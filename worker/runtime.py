"""Control, heartbeat and inference never share a blocking network/inference loop."""

import itertools
import json
import math
import os
import re
import selectors
import signal
import subprocess
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from .capture import HEIGHT, WIDTH, Decoder, LatestFrame

CONFIG_TTL = 5.0
MAX_FRAME_AGE = 3.0
HTTP_TIMEOUT = 1.5
HTTP_CLEANUP_TIMEOUT = 0.5
MAX_RESPONSE_BYTES = 65536
MAX_REQUEST_BYTES = 3 * 1024 * 1024
MAX_STREAMS = 4


@dataclass(frozen=True)
class Config:
    stream_id: str
    enabled: bool
    session_id: str | None
    catalog_version: int
    rtsp_url: str = field(repr=False)
    analysis_fps: float
    detection_threshold: float
    pause_reason: str | None = None

    @classmethod
    def parse(cls, data):
        stream_id = data["stream_id"]
        if not isinstance(stream_id, str) or not re.fullmatch(r"stream(?:-[a-f0-9]{32})?", stream_id):
            raise ValueError("Invalid stream ID")
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
        return cls(stream_id, enabled, session, version, url, max(0.2, min(fps, 10.0)), threshold, reason)

    @property
    def active(self):
        return self.enabled and self.session_id is not None

    def stream_url(self, token):
        parsed = urlsplit(self.rtsp_url)
        host = parsed.netloc.rsplit("@", 1)[-1]
        return parsed._replace(netloc=f"reader:{quote(token, safe='')}@{host}").geturl()


class Backend:
    def __init__(self, base_url, token):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Invalid BACKEND_URL")
        if not 32 <= len(token) <= 4096 or any(not 32 <= ord(char) < 127 for char in token):
            raise ValueError("INTERNAL_TOKEN must contain at least 32 characters")
        self._base_url, self._token = base_url.rstrip("/"), token
        self._lock, self._processes, self._closed = threading.Lock(), set(), False

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload, allow_nan=False)
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            raise ValueError("Backend request too large")
        options = [("url", self._base_url + path), ("header", f"Authorization: Bearer {self._token}"),
                   ("header", "Content-Type: application/json")]
        if body is not None:
            options.append(("data-binary", body))
        lines = []
        for name, value in options:
            # curl config quoting is not JSON quoting. Escape backslashes first so
            # JSON escapes and user strings cannot introduce config directives.
            value = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
            lines.append(f'{name} = "{value}"\n')
        pending = memoryview("".join(lines).encode())
        deadline = time.monotonic() + HTTP_TIMEOUT
        with self._lock:
            self._processes = {child for child in self._processes if child.poll() is None}
            # Normal operation has config, heartbeat and one observation request;
            # reserve at most four more for final fatal heartbeats, never a queue.
            if self._closed or len(self._processes) >= MAX_STREAMS + 3:
                raise ConnectionError("Backend transport unavailable")
            process = subprocess.Popen(
                ["curl", "--disable", "--silent", "--globoff", "--proxy", "", "--noproxy", "*",
                 "--proto", "=http,https", "--no-location", "--max-redirs", "0", "--retry", "0",
                 "--max-time", str(HTTP_TIMEOUT), "--write-out", "\\n%{http_code}", "--config", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            self._processes.add(process)
        try:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Backend request timed out")
                    for key, _ in selector.select(remaining):
                        if key.fileobj is process.stdin:
                            written = os.write(key.fd, pending[:65536])
                            pending = pending[written:]
                            if not pending:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                        else:
                            # Four trailer bytes (newline + status), plus one byte
                            # to detect overflow. Never buffer an unbounded body.
                            chunk = os.read(key.fd, MAX_RESPONSE_BYTES + 5 - len(output))
                            if not chunk:
                                selector.unregister(process.stdout)
                            output.extend(chunk)
                            if len(output) > MAX_RESPONSE_BYTES + 4:
                                raise ValueError("Backend response too large")
            code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            if code == 28 or time.monotonic() >= deadline:
                raise TimeoutError("Backend request timed out")
            if code != 0:
                raise ConnectionError("Backend request failed")
            if len(output) < 4 or output[-4] != 10 or not output[-3:].isdigit():
                raise ValueError("Invalid backend response")
            content, status = output[:-4], int(output[-3:])
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("Backend response too large")
            if not 100 <= status <= 599:
                raise ValueError("Invalid backend response")
            if not 200 <= status < 300:
                raise urllib.error.HTTPError("", status, "Backend request rejected", {}, None)
            return json.loads(content) if content else None
        except subprocess.TimeoutExpired:
            raise TimeoutError("Backend request timed out") from None
        except ConnectionError:
            raise ConnectionError("Backend request failed") from None
        finally:
            process.kill()
            try:
                process.wait(timeout=HTTP_CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise TimeoutError("Backend transport cleanup timed out") from None
            finally:
                process.stdin.close()
                process.stdout.close()
                with self._lock:
                    # Retain an unreaped child in the bounded registry rather than
                    # allowing repeated failures to accumulate orphan processes.
                    if process.returncode is not None:
                        self._processes.discard(process)

    def close(self):
        with self._lock:
            self._closed = True
            processes = list(self._processes)
        for process in processes:
            process.kill()
        deadline = time.monotonic() + HTTP_CLEANUP_TIMEOUT
        for process in processes:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass


class State:
    # Unique across removed/re-added State objects as well as config rollbacks.
    generations = itertools.count(1)

    def __init__(self, stream_id):
        self.stream_id = stream_id
        self.lock = threading.Lock()
        self.slot = LatestFrame()
        self.config = None
        self.generation = 0
        self.config_at = 0.0
        self.retired = False
        self.state, self.error = "starting", None
        self.delivery_error_at = None

    def update_config(self, config):
        with self.lock:
            if self.retired:
                return
            if config is not None and config.stream_id != self.stream_id:
                raise ValueError("Mismatched stream config")
            if config != self.config or time.monotonic() - self.config_at > CONFIG_TTL:
                self.generation = next(self.generations)
                self.slot.clear()
            self.config, self.config_at = config, time.monotonic()

    def snapshot(self):
        with self.lock:
            if time.monotonic() - self.config_at > CONFIG_TTL:
                if self.config is not None:
                    self.config = None
                    self.generation = next(self.generations)
                    self.slot.clear()
            return self.config, self.generation

    def current(self, generation):
        config, actual = self.snapshot()
        return config is not None and config.active and actual == generation

    def invalidate(self, generation):
        with self.lock:
            if self.generation == generation:
                self.config = None
                self.generation = next(self.generations)
                self.slot.clear()

    def retire(self):
        with self.lock:
            self.retired = True
            self.config = None
            self.generation = next(self.generations)
            self.slot.clear()

    def status(self, state, error=None):
        with self.lock:
            self.state, self.error = state, error

    def heartbeat(self, provider, fatal):
        with self.lock:
            delivery_error = "Observation delivery failed" if self.delivery_error_at is not None else None
            return {"stream_id": self.stream_id, "state": "error" if fatal or delivery_error else self.state,
                    "provider": provider, "error": fatal or delivery_error or self.error}

    def delivery_result(self, generation, success):
        with self.lock:
            if not self.retired and self.generation == generation:
                self.delivery_error_at = None if success else time.monotonic()


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.streams = {}
        self.provider = "unavailable"
        self.ready = False
        self.fatal = None

    def update_configs(self, configs):
        if len(configs) > MAX_STREAMS or len({config.stream_id for config in configs}) != len(configs):
            raise ValueError("Invalid stream registry")
        with self.lock:
            incoming = {config.stream_id for config in configs}
            for stream_id in list(self.streams):
                if stream_id not in incoming:
                    self.streams.pop(stream_id).retire()
            for config in configs:
                if config.stream_id not in self.streams:
                    self.streams[config.stream_id] = State(config.stream_id)
                self.streams[config.stream_id].update_config(config)

    def invalidate(self):
        with self.lock:
            for state in self.streams.values():
                state.update_config(None)

    def snapshot(self):
        with self.lock:
            return list(self.streams.values())

    def heartbeats(self):
        with self.lock:
            return [state.heartbeat(self.provider, self.fatal) for state in self.streams.values()]


def poll_config(backend, registry, stop):
    while not stop.is_set():
        try:
            data = backend.request("/internal/worker/configs")
            if not isinstance(data["streams"], list):
                raise ValueError("Invalid stream registry")
            registry.update_configs([Config.parse(item) for item in data["streams"]])
        except Exception:
            registry.invalidate()
        stop.wait(1.0)


def heartbeat_loop(backend, registry, stop):
    while not stop.is_set():
        for beat in registry.heartbeats():
            if stop.is_set():
                break
            try:
                backend.request("/internal/worker/heartbeat", beat)
            except Exception:
                pass
        stop.wait(2.0)


def submit_faces(backend, state, config, frame, faces):
    if (not faces or config.stream_id != state.stream_id or not state.current(frame.generation)
            or time.monotonic() - frame.received_at > MAX_FRAME_AGE):
        return False
    try:
        backend.request("/internal/observations", {
            "stream_id": config.stream_id,
            "session_id": config.session_id, "catalog_version": config.catalog_version,
             "captured_at": frame.captured_at, "faces": faces[:20]})
        state.delivery_result(frame.generation, True)
        return True
    except Exception:
        # A conflict or network failure invalidates only this batch's stream and
        # generation. Never replay observations, including after a later refresh.
        state.delivery_result(frame.generation, False)
        state.invalidate(frame.generation)
        return False


def inference_loop(backend, registry, stop, model_dir, device):
    try:
        # Native initialization/inference can be slow; heartbeat and shutdown remain independent.
        import numpy as np

        from .inference import FaceEngine
        engine = FaceEngine(model_dir, device)
        with registry.lock:
            registry.provider, registry.ready = engine.provider, True
    except Exception:
        with registry.lock:
            registry.fatal = "Model or requested inference provider initialization failed"
        return
    previous = None
    while not stop.is_set():
        states = registry.snapshot()
        # Registry insertion order survives list reordering. Resume after the last
        # serviced stream, skipping empty slots without favoring a busy decoder.
        if previous in states:
            start = states.index(previous) + 1
            states = states[start:] + states[:start]
        frame = None
        for state in states:
            frame = state.slot.take()
            if frame is not None:
                previous = state
                break
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
                                   lambda: (not stop.is_set() and state.current(generation)
                                            and time.monotonic() - frame.received_at <= MAX_FRAME_AGE))
        except Exception:
            with registry.lock:
                registry.fatal = "Face inference failed"
            return
        if stop.is_set():
            return
        # The transport kills/reaps at its wall-clock deadline, so one synchronous
        # request cannot indefinitely suppress the next stream's turn. No retries.
        submit_faces(backend, state, config, frame, faces)


def run():
    stop, registry = threading.Event(), Registry()
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
        threading.Thread(target=poll_config, args=(backend, registry, stop), daemon=True, name="config"),
        threading.Thread(target=heartbeat_loop, args=(backend, registry, stop), daemon=True, name="heartbeat"),
        threading.Thread(target=inference_loop,
                         args=(backend, registry, stop, os.environ.get("MODEL_DIR", "/models"), device),
                         daemon=True, name="inference"),
    ]
    for thread in threads:
        thread.start()
    decoders = {}
    result = 0
    try:
        while not stop.is_set():
            with registry.lock:
                ready, fatal = registry.ready, registry.fatal
            if fatal:
                print(fatal, flush=True)
                result = 1
                break
            for state, decoder in list(decoders.items()):
                config, generation = state.snapshot()
                if not ready or config is None or not config.active or decoder.generation != generation:
                    decoder.stop_event.set()
                if not decoder.is_alive():
                    decoder.join(timeout=0)
                    del decoders[state]
                    state.slot.clear()
            for state in registry.snapshot():
                config, generation = state.snapshot()
                active = ready and config is not None and config.active
                decoder = decoders.get(state)
                if active and decoder is None and len(decoders) < MAX_STREAMS:
                    decoder = Decoder(config.stream_url(token), config.analysis_fps, generation, state.slot)
                    decoders[state] = decoder
                    decoder.start()
                if not ready:
                    state.status("starting")
                elif config is None:
                    state.status("error", "Worker configuration unavailable")
                elif not config.enabled:
                    reason = ("Low disk space" if config.pause_reason == "Low disk space"
                              else "Analysis paused" if config.pause_reason else None)
                    state.status("paused" if config.pause_reason else "disabled", reason)
                elif not config.active:
                    state.status("offline")
                elif decoder is None or decoder.stop_event.is_set():
                    state.status("connecting")
                elif decoder.failed:
                    state.status("error", "Stream unavailable or timed out")
                elif decoder.last_frame == 0:
                    state.status("connecting")
                else:
                    state.status("analyzing")
            stop.wait(0.1)
    finally:
        stop.set()
        with registry.lock:
            for state in registry.streams.values():
                state.retire()
        # Signal all decoders before waiting: their subprocess cleanup runs in
        # parallel, with one shared deadline rather than four serial close waits.
        for decoder in decoders.values():
            decoder.stop_event.set()
        deadline = time.monotonic() + 3.0
        for state, decoder in decoders.items():
            decoder.join(timeout=max(0.0, deadline - time.monotonic()))
            state.slot.clear()
        if result:
            def report_fatal(beat):
                try:
                    backend.request("/internal/worker/heartbeat", beat)
                except Exception:
                    pass

            reporters = [threading.Thread(target=report_fatal, args=(beat,), daemon=True, name="fatal-heartbeat")
                         for beat in registry.heartbeats()]
            for reporter in reporters:
                reporter.start()
            deadline = time.monotonic() + HTTP_TIMEOUT
            for reporter in reporters:
                reporter.join(timeout=max(0.0, deadline - time.monotonic()))
        backend.close()
        for thread in threads:
            thread.join(timeout=0.2)
    return result
