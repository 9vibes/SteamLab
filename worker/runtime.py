"""Control, heartbeat and inference never share a blocking network/inference loop."""

import json
import math
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from .capture import HEIGHT, WIDTH, Decoder, LatestFrame

CONFIG_TTL = 5.0
MAX_FRAME_AGE = 3.0
HTTP_TIMEOUT = 1.5


@dataclass(frozen=True)
class Config:
    enabled: bool
    session_id: str | None
    catalog_version: int
    rtsp_url: str = field(repr=False)
    analysis_fps: float
    detection_threshold: float
    pause_reason: str | None = None

    @classmethod
    def parse(cls, data):
        enabled, session = data["enabled"], data["session_id"]
        version, url = data["catalog_version"], data["rtsp_url"]
        fps, threshold = float(data["analysis_fps"]), float(data["detection_threshold"])
        reason = data.get("pause_reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 256):
            raise ValueError("Invalid pause reason")
        if type(enabled) is not bool or (session is not None and (not isinstance(session, str) or not session)):
            raise ValueError("Invalid worker config")
        if type(version) is not int or version < 0 or not isinstance(url, str):
            raise ValueError("Invalid worker config")
        if enabled and session is not None and (urlsplit(url).scheme not in ("rtsp", "rtsps") or not urlsplit(url).hostname):
            raise ValueError("Invalid stream config")
        if not math.isfinite(fps) or fps <= 0 or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Invalid analysis config")
        return cls(enabled, session, version, url, max(0.2, min(fps, 10.0)), threshold, reason)

    @property
    def active(self):
        return self.enabled and self.session_id is not None

    def stream_url(self, token):
        parsed = urlsplit(self.rtsp_url)
        host = parsed.netloc.rsplit("@", 1)[-1]
        return parsed._replace(netloc=f"reader:{quote(token, safe='')}@{host}").geturl()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Backend:
    def __init__(self, base_url, token):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Invalid BACKEND_URL")
        if len(token) < 32 or "\n" in token or "\r" in token:
            raise ValueError("INTERNAL_TOKEN must contain at least 32 characters")
        self._base_url, self._token = base_url.rstrip("/"), token

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        req = urllib.request.Request(self._base_url + path, data=body,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Content-Type": "application/json"})
        # Do not leak the bearer token through redirects or ambient HTTP proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=HTTP_TIMEOUT) as response:
            content = response.read(65537)
            if len(content) > 65536:
                raise ValueError("Backend response too large")
            return json.loads(content) if content else None


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.config = None
        self.generation = 0
        self.config_at = 0.0
        self.provider = "unavailable"
        self.ready = False
        self.fatal = None
        self.state, self.error = "starting", None

    def update_config(self, config):
        with self.lock:
            if config != self.config:
                self.generation += 1
            self.config, self.config_at = config, time.monotonic()

    def snapshot(self):
        with self.lock:
            if time.monotonic() - self.config_at > CONFIG_TTL:
                return None, self.generation
            return self.config, self.generation

    def current(self, generation):
        config, actual = self.snapshot()
        return config is not None and config.active and actual == generation

    def invalidate(self, generation):
        with self.lock:
            if self.generation == generation:
                self.config = None
                self.generation += 1

    def status(self, state, error=None):
        with self.lock:
            self.state, self.error = state, error

    def heartbeat(self):
        with self.lock:
            return {"state": "error" if self.fatal else self.state,
                    "provider": self.provider, "error": self.fatal or self.error}


def poll_config(backend, state, stop):
    while not stop.is_set():
        try:
            state.update_config(Config.parse(backend.request("/internal/worker/config")))
        except Exception:
            state.update_config(None)
        stop.wait(1.0)


def heartbeat_loop(backend, state, stop):
    while not stop.is_set():
        try:
            backend.request("/internal/worker/heartbeat", state.heartbeat())
        except Exception:
            pass
        stop.wait(2.0)


def submit_faces(backend, state, config, frame, faces):
    if not faces or not state.current(frame.generation) or time.monotonic() - frame.received_at > MAX_FRAME_AGE:
        return False
    try:
        backend.request("/internal/observations", {
            "session_id": config.session_id, "catalog_version": config.catalog_version,
            "captured_at": frame.captured_at, "faces": faces[:20]})
        return True
    except urllib.error.HTTPError as error:
        if error.code == 409:
            state.invalidate(frame.generation)
            return False
        raise


def inference_loop(backend, state, slot, stop, model_dir, device):
    try:
        # Native initialization/inference can be slow; heartbeat and shutdown remain independent.
        import numpy as np

        from .inference import FaceEngine
        engine = FaceEngine(model_dir, device)
        with state.lock:
            state.provider, state.ready = engine.provider, True
    except Exception:
        with state.lock:
            state.fatal = "Model or requested inference provider initialization failed"
        return
    while not stop.is_set():
        frame = slot.take()
        if frame is None:
            stop.wait(0.05)
            continue
        config, generation = state.snapshot()
        if config is None or not config.active or generation != frame.generation:
            continue
        if time.monotonic() - frame.received_at > MAX_FRAME_AGE:
            continue
        try:
            image = np.frombuffer(frame.data, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
            faces = engine.analyze(image, config.detection_threshold,
                                   lambda generation=generation: not stop.is_set() and state.current(generation))
        except Exception:
            with state.lock:
                state.fatal = "Face inference failed"
            return
        if stop.is_set():
            return
        try:
            submit_faces(backend, state, config, frame, faces)
        except Exception:
            # Drop rather than replay observations after a network failure.
            state.invalidate(generation)


def run():
    stop, state, slot = threading.Event(), State(), LatestFrame()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        device = os.environ.get("INFERENCE_DEVICE", "cpu").lower()
        if device not in ("cpu", "cuda"):
            raise ValueError("Invalid device")
        token = os.environ.get("INTERNAL_TOKEN", "")
        backend = Backend(os.environ.get("BACKEND_URL", "http://backend:8000"), token)
    except Exception:
        print("Worker configuration invalid", flush=True)
        return 1
    threads = [
        threading.Thread(target=poll_config, args=(backend, state, stop), daemon=True, name="config"),
        threading.Thread(target=heartbeat_loop, args=(backend, state, stop), daemon=True, name="heartbeat"),
        threading.Thread(target=inference_loop,
                         args=(backend, state, slot, stop, os.environ.get("MODEL_DIR", "/models"), device),
                         daemon=True, name="inference"),
    ]
    for thread in threads:
        thread.start()
    decoder = None
    result = 0
    try:
        while not stop.is_set():
            config, generation = state.snapshot()
            with state.lock:
                ready, fatal = state.ready, state.fatal
            if fatal:
                print(fatal, flush=True)
                try:
                    backend.request("/internal/worker/heartbeat", state.heartbeat())
                except Exception:
                    pass
                result = 1
                break
            active = ready and config is not None and config.active
            if decoder is not None and (not active or decoder.generation != generation):
                decoder.close()
                decoder = None
                slot.clear()
            if active and decoder is None:
                decoder = Decoder(config.stream_url(token), config.analysis_fps, generation, slot)
                decoder.start()
            if not ready:
                state.status("starting")
            elif config is None:
                state.status("error", "Worker configuration unavailable")
            elif not config.enabled:
                state.status("paused" if config.pause_reason else "disabled", config.pause_reason)
            elif not config.active:
                state.status("offline")
            elif decoder.failed:
                state.status("error", "Stream unavailable or timed out")
            elif decoder.last_frame == 0:
                state.status("connecting")
            else:
                state.status("analyzing")
            stop.wait(0.1)
    finally:
        stop.set()
        if decoder is not None:
            decoder.close()
        slot.clear()
        for thread in threads:
            thread.join(timeout=0.2)
    return result
