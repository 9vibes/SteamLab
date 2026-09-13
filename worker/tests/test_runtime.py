import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

from worker import runtime
from worker.capture import Frame, LatestFrame


def config(**changes):
    return replace(runtime.Config(True, "session-one", 1, "rtsp://reader:secret@media/live", 2, 0.85), **changes)


def frame(state, *, age=0):
    return Frame(b"", state.generation, "2026-09-13T12:00:00+00:00", time.monotonic() - age)


class Backend:
    def __init__(self, code=None):
        self.calls = []
        self.code = code

    def request(self, path, payload=None):
        self.calls.append((path, payload))
        if self.code:
            raise urllib.error.HTTPError("https://secret", self.code, "secret", {}, None)
        return {"accepted": 1}


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"session_id": "new-session"}, {"session_id": None},
    {"catalog_version": 2}, {"detection_threshold": 0.9}, {"rtsp_url": "rtsp://different/live"},
])
def test_config_change_prevents_stale_submission(changes):
    state, backend = runtime.State(), Backend()
    old = config()
    state.update_config(old)
    captured = frame(state)
    state.update_config(config(**changes))
    assert not runtime.submit_faces(backend, state, old, captured, [{"test": True}])
    assert backend.calls == []


def test_disabled_then_reenabled_still_invalidates_old_work():
    state = runtime.State()
    state.update_config(config())
    generation = state.generation
    state.update_config(config(enabled=False))
    state.update_config(config())
    assert not state.current(generation)


def test_identical_poll_keeps_generation_and_expired_config_fails_closed():
    state = runtime.State()
    state.update_config(config())
    generation = state.generation
    state.update_config(config())
    assert state.current(generation)
    state.config_at -= runtime.CONFIG_TTL + 1
    assert not state.current(generation)


def test_disk_pause_is_stable_and_resumes_after_config_change():
    values = {"enabled": False, "session_id": "s", "catalog_version": 1, "analysis_fps": 2,
              "detection_threshold": 0.85, "rtsp_url": "rtsp://media/live", "pause_reason": "Low disk space"}
    paused = runtime.Config.parse(values)
    state = runtime.State()
    state.update_config(paused)
    generation = state.generation
    state.update_config(runtime.Config.parse(values))
    assert state.generation == generation and not state.snapshot()[0].active
    state.update_config(runtime.Config.parse({**values, "enabled": True, "pause_reason": None}))
    assert state.generation > generation and state.snapshot()[0].active


def test_conflict_invalidates_and_never_retries_batch():
    state, backend = runtime.State(), Backend(409)
    current = config()
    state.update_config(current)
    captured = frame(state)
    assert not runtime.submit_faces(backend, state, current, captured, [{}])
    assert state.snapshot()[0] is None
    assert not runtime.submit_faces(backend, state, current, captured, [{}])
    assert len(backend.calls) == 1


def test_old_conflict_cannot_invalidate_new_config():
    state = runtime.State()
    state.update_config(config())
    old = state.generation
    state.update_config(config(catalog_version=2))
    state.invalidate(old)
    assert state.snapshot()[0].catalog_version == 2


def test_payload_contract_and_stale_frames():
    state, backend = runtime.State(), Backend()
    current = config()
    state.update_config(current)
    assert runtime.submit_faces(backend, state, current, frame(state), [{}] * 25)
    path, payload = backend.calls[0]
    assert path == "/internal/observations"
    assert payload["session_id"] == "session-one"
    assert payload["catalog_version"] == 1
    assert payload["captured_at"].endswith("+00:00")
    assert len(payload["faces"]) == 20
    assert not runtime.submit_faces(backend, state, current, frame(state, age=4), [{}])
    assert not runtime.submit_faces(backend, state, current, frame(state), [])
    assert len(backend.calls) == 1


@pytest.mark.parametrize("changes", [
    {"enabled": 1}, {"session_id": 123}, {"catalog_version": True}, {"catalog_version": -1},
    {"analysis_fps": float("nan")}, {"analysis_fps": 0}, {"detection_threshold": float("inf")},
    {"detection_threshold": 1.1}, {"rtsp_url": "file:///secret"},
])
def test_invalid_config(changes):
    values = {"enabled": True, "session_id": "s", "catalog_version": 1, "analysis_fps": 2,
              "detection_threshold": 0.85, "rtsp_url": "rtsp://media/live"}
    values.update(changes)
    with pytest.raises(ValueError):
        runtime.Config.parse(values)


def test_config_repr_does_not_expose_stream_credentials():
    assert "secret" not in repr(config())


@pytest.mark.parametrize("url", ["rtsp://media:8554/live/stream", "rtsp://reader:old@media:8554/live/stream",
                                  "rtsp://[::1]:8554/live/stream"])
def test_rtsp_reader_credentials_are_safely_encoded(url):
    token = "@:/?# percent %" + "x" * 32
    parsed = urlsplit(config(rtsp_url=url).stream_url(token))
    assert parsed.username == "reader"
    assert unquote(parsed.password) == token
    assert parsed.hostname == urlsplit(url).hostname
    assert parsed.port == 8554 and parsed.path == "/live/stream"


def test_http_token_timeout_and_no_redirect(monkeypatch):
    calls = []

    def open_response(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(b'{"accepted": 1}')

    monkeypatch.setattr(runtime.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_response))
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    assert backend.request("/internal/observations", {"faces": []}) == {"accepted": 1}
    request, timeout = calls[0]
    assert request.get_header("Authorization") == "Bearer " + "x" * 32
    assert timeout == 1.5
    assert json.loads(request.data) == {"faces": []}
    assert runtime.NoRedirect().redirect_request(request, None, 302, "", {}, "https://attacker") is None


def test_heartbeat_and_config_continue_during_blocked_native_initialization(monkeypatch):
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    state = runtime.State()

    class SlowEngine:
        def __init__(self, *_):
            entered.set()
            release.wait(5)
            self.provider = "CPUExecutionProvider"

    class ResponsiveBackend:
        def __init__(self):
            self.beats, self.polls = [], 0

        def request(self, path, payload=None):
            if path.endswith("heartbeat"):
                self.beats.append(time.monotonic())
            else:
                self.polls += 1
                return {"enabled": False, "session_id": None, "catalog_version": 1,
                        "rtsp_url": "", "analysis_fps": 2, "detection_threshold": 0.85}

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "worker.inference", SimpleNamespace(FaceEngine=SlowEngine))
    backend = ResponsiveBackend()
    threads = [threading.Thread(target=runtime.inference_loop,
                                args=(backend, state, LatestFrame(), stop, "/models", "cpu")),
               threading.Thread(target=runtime.heartbeat_loop, args=(backend, state, stop)),
               threading.Thread(target=runtime.poll_config, args=(backend, state, stop))]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(1)
        time.sleep(2.15)
        assert not state.ready
        assert len(backend.beats) >= 2
        assert backend.beats[1] - backend.beats[0] < 5
        assert backend.polls >= 2
    finally:
        stop.set()
        release.set()
        for thread in threads:
            thread.join(timeout=2)


def test_provider_failure_heartbeat_is_generic(monkeypatch):
    def fail(*_):
        raise RuntimeError("rtsp://reader:secret@host and sensitive payload")

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "worker.inference", SimpleNamespace(FaceEngine=fail))
    state = runtime.State()
    runtime.inference_loop(Backend(), state, LatestFrame(), threading.Event(), "/models", "cuda")
    beat = state.heartbeat()
    assert beat["state"] == "error"
    assert beat["provider"] == "unavailable"
    assert "secret" not in json.dumps(beat)
    assert "provider" in beat["error"]


def test_sigterm_exits_even_if_native_initialization_never_returns():
    script = """
import os, sys, threading, types
from worker.runtime import run
class Engine:
    def __init__(self, *_):
        print('initializing', flush=True)
        threading.Event().wait()
sys.modules['numpy'] = types.SimpleNamespace()
sys.modules['worker.inference'] = types.SimpleNamespace(FaceEngine=Engine)
os._exit(run())
"""
    environment = dict(os.environ, INTERNAL_TOKEN="x" * 32, BACKEND_URL="http://127.0.0.1:1",
                       INFERENCE_DEVICE="cpu", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen([sys.executable, "-c", script], env=environment,
                               cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "initializing"
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=4)
        assert process.returncode == 0
        assert stderr == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
