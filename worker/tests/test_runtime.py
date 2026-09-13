import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest

from worker import runtime
from worker.capture import Frame


STREAM_IDS = ["stream"] + ["stream-" + str(index) * 32 for index in range(1, 5)]


def config(**changes):
    return replace(runtime.Config("stream", True, "session-one", 1, "rtsp://reader:secret@media/live", 2, 0.85), **changes)


def config_values(**changes):
    return asdict(config(**changes))


def make_registry(count=4):
    registry = runtime.Registry()
    registry.update_configs([config(stream_id=stream_id) for stream_id in STREAM_IDS[:count]])
    return registry


def frame(state, *, age=0, data=b""):
    return Frame(data, state.generation, "2026-09-13T12:00:00+00:00", time.monotonic() - age)


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for runtime")


def mock_engine(monkeypatch, engine):
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(
        uint8="uint8", frombuffer=lambda data, **_: SimpleNamespace(reshape=lambda *_: data)))
    monkeypatch.setitem(sys.modules, "worker.inference", SimpleNamespace(FaceEngine=engine))


@pytest.fixture
def worker_run(monkeypatch):
    handlers, results, threads = {}, [], []
    monkeypatch.setenv("INTERNAL_TOKEN", "x" * 32)
    monkeypatch.setenv("INFERENCE_DEVICE", "cpu")
    monkeypatch.setattr(runtime.signal, "signal", lambda number, handler: handlers.update({number: handler}))
    monkeypatch.setattr(runtime, "poll_config", lambda backend, registry, stop: stop.wait())

    def start(registry, engine, decoder, backend=None):
        monkeypatch.setattr(runtime, "Registry", lambda: registry)
        monkeypatch.setattr(runtime, "Backend", lambda *_: backend or Backend())
        monkeypatch.setattr(runtime, "Decoder", decoder)
        mock_engine(monkeypatch, engine)
        thread = threading.Thread(target=lambda: results.append(runtime.run()), daemon=True)
        threads.append(thread)
        thread.start()
        wait_until(lambda: signal.SIGTERM in handlers)
        return thread, lambda: handlers[signal.SIGTERM](), results

    yield start
    if signal.SIGTERM in handlers:
        handlers[signal.SIGTERM]()
    for thread in threads:
        thread.join(timeout=4)
        assert not thread.is_alive(), "Runtime did not shut down"


class Backend:
    def __init__(self, code=None):
        self.calls = []
        self.code = code

    def request(self, path, payload=None):
        self.calls.append((path, payload))
        if self.code:
            raise urllib.error.HTTPError("https://secret", self.code, "secret", {}, None)
        return {"accepted": 1}

    def close(self):
        pass


@pytest.fixture
def fake_curl(monkeypatch):
    """Real pipe/process lifecycle, without requiring curl or network access."""
    launched = []
    real_popen = subprocess.Popen

    def install(scripts):
        def popen(command, **kwargs):
            assert command[0] == "curl"
            script = scripts[len(launched)] if isinstance(scripts, list) else scripts
            process = real_popen([sys.executable, "-c", script], **kwargs)
            launched.append((command, kwargs, process))
            return process

        monkeypatch.setattr(runtime.subprocess, "Popen", popen)
        return launched

    yield install
    for _, _, process in launched:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"session_id": "new-session"}, {"session_id": None},
    {"catalog_version": 2}, {"detection_threshold": 0.9}, {"rtsp_url": "rtsp://different/live"},
])
def test_config_change_prevents_stale_submission(changes):
    state, backend = runtime.State("stream"), Backend()
    old = config()
    state.update_config(old)
    captured = frame(state)
    state.update_config(config(**changes))
    assert not runtime.submit_faces(backend, state, old, captured, [{"test": True}])
    assert backend.calls == []


def test_disabled_then_reenabled_still_invalidates_old_work():
    state = runtime.State("stream")
    state.update_config(config())
    generation = state.generation
    state.update_config(config(enabled=False))
    state.update_config(config())
    assert not state.current(generation)


def test_identical_poll_keeps_generation_and_expired_config_fails_closed():
    state = runtime.State("stream")
    state.update_config(config())
    generation = state.generation
    state.update_config(config())
    assert state.current(generation)
    state.config_at -= runtime.CONFIG_TTL + 1
    assert not state.current(generation)


def test_disk_pause_is_stable_and_resumes_after_config_change():
    values = {"stream_id": "stream", "enabled": False, "session_id": "s", "catalog_version": 1, "analysis_fps": 2,
              "detection_threshold": 0.85, "rtsp_url": "rtsp://media/live", "pause_reason": "Low disk space"}
    paused = runtime.Config.parse(values)
    state = runtime.State("stream")
    state.update_config(paused)
    generation = state.generation
    state.update_config(runtime.Config.parse(values))
    assert state.generation == generation and not state.snapshot()[0].active
    state.update_config(runtime.Config.parse({**values, "enabled": True, "pause_reason": None}))
    assert state.generation > generation and state.snapshot()[0].active


def test_conflict_invalidates_and_never_retries_batch():
    state, backend = runtime.State("stream"), Backend(409)
    current = config()
    state.update_config(current)
    captured = frame(state)
    assert not runtime.submit_faces(backend, state, current, captured, [{}])
    assert state.snapshot()[0] is None
    assert not runtime.submit_faces(backend, state, current, captured, [{}])
    assert len(backend.calls) == 1


def test_old_conflict_cannot_invalidate_new_config():
    state = runtime.State("stream")
    state.update_config(config())
    old = state.generation
    state.update_config(config(catalog_version=2))
    state.invalidate(old)
    assert state.snapshot()[0].catalog_version == 2


def test_payload_contract_and_stale_frames():
    state, backend = runtime.State("stream"), Backend()
    current = config()
    state.update_config(current)
    assert runtime.submit_faces(backend, state, current, frame(state), [{}] * 25)
    path, payload = backend.calls[0]
    assert path == "/internal/observations"
    assert payload["stream_id"] == "stream"
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
    {"stream_id": "rtsp://secret"}, {"stream_id": ""}, {"stream_id": None},
    {"stream_id": "stream-" + "A" * 32},
])
def test_invalid_config(changes):
    values = {"stream_id": "stream", "enabled": True, "session_id": "s", "catalog_version": 1, "analysis_fps": 2,
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


def test_http_credentials_and_json_only_on_stdin_no_redirect_proxy_or_curlrc(fake_curl, capsys):
    launched = fake_curl("import json,sys; print(json.dumps({'config': sys.stdin.read()})); print('200', end='')")
    token = 'secret-"\\' + "x" * 32
    payload = {"faces": [{"thumbnail": 'private\\image"\r\nurl = "https://attacker"', "name": "\u00e9"}]}
    backend = runtime.Backend("http://backend:8000/private-origin", token)
    response = backend.request("/internal/observations", payload)
    options = [(name, json.loads(value)) for name, value in
               (line.split(" = ", 1) for line in response["config"].splitlines())]
    assert options == [("url", "http://backend:8000/private-origin/internal/observations"),
                       ("header", f"Authorization: Bearer {token}"),
                       ("header", "Content-Type: application/json"),
                       ("data-binary", json.dumps(payload, allow_nan=False))]
    command, kwargs, process = launched[0]
    assert command[:2] == ["curl", "--disable"]
    for flag, value in [("--config", "-"), ("--proxy", ""), ("--noproxy", "*"),
                        ("--max-redirs", "0"), ("--retry", "0"), ("--proto", "=http,https"),
                        ("--max-time", "1.5"), ("--write-out", "\\n%{http_code}")]:
        assert command[command.index(flag) + 1] == value
    assert "--no-location" in command and "--globoff" in command
    assert "--location" not in command
    assert not any("secret" in arg or "private" in arg or "Authorization" in arg for arg in command)
    assert kwargs == {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                      "stderr": subprocess.DEVNULL, "bufsize": 0}
    assert process.returncode == 0 and not backend._processes
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("script,payload", [
    ("import time; time.sleep(30)", {"faces": ["x" * 200000]}),
    ("import sys,time; sys.stdin.read(); time.sleep(30)", {}),
    ("import os,sys,time; sys.stdin.read();\nwhile True: os.write(1,b' '); time.sleep(.02)", {}),
    ("import os,sys,time; sys.stdin.read(); os.close(1); time.sleep(30)", {}),
])
def test_http_deadline_kills_and_reaps_during_send_headers_trickle_and_exit(monkeypatch, fake_curl, script, payload):
    monkeypatch.setattr(runtime, "HTTP_TIMEOUT", 0.2)
    launched = fake_curl(script)
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="Backend request timed out"):
        backend.request("/internal/observations", payload)
    assert 0.15 <= time.monotonic() - started < 1
    assert len(launched) == 1
    process = launched[0][2]
    assert process.returncode == -signal.SIGKILL
    assert process.stdin.closed and process.stdout.closed
    assert backend._processes == set()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 409, 503])
def test_http_status_and_redirect_errors_preserve_code_without_payload(fake_curl, status):
    launched = fake_curl(f"import sys; sys.stdin.read(); sys.stdout.write('secret body and URL\\n{status}')")
    backend = runtime.Backend("http://backend:8000/private", "x" * 32)
    with pytest.raises(urllib.error.HTTPError) as caught:
        backend.request("/internal/observations", {})
    assert caught.value.code == status
    assert caught.value.read() == b""
    assert "secret" not in str(caught.value) and "private" not in str(caught.value)
    assert len(launched) == 1 and launched[0][2].returncode == 0
    assert not backend._processes


@pytest.mark.parametrize("payload,status,expected", [
    (b"", 204, None), (b'{"accepted": 1}', 200, {"accepted": 1}),
    (b'"text\\n409"', 200, "text\n409"),
    (b'"' + b"x" * (runtime.MAX_RESPONSE_BYTES - 2) + b'"', 200, "x" * (runtime.MAX_RESPONSE_BYTES - 2)),
], ids=["empty", "json", "status-in-body", "exact-cap"])
def test_http_get_success_empty_and_exact_response_cap(fake_curl, payload, status, expected):
    script = ("import json,sys; config=sys.stdin.read(); assert 'data-binary =' not in config; "
              f"sys.stdout.buffer.write({payload!r} + b'\\n{status}')")
    fake_curl(script)
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    assert backend.request("/internal/worker/configs") == expected


@pytest.mark.parametrize("extra", [1, 1000000])
def test_http_response_overflow_kills_without_unbounded_buffering(fake_curl, extra):
    script = ("import sys,time; sys.stdin.read(); "
              f"sys.stdout.buffer.write(b'x'*{runtime.MAX_RESPONSE_BYTES + extra} + b'\\n200'); "
              "sys.stdout.buffer.flush(); time.sleep(30)")
    launched = fake_curl(script)
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    started = time.monotonic()
    with pytest.raises(ValueError, match="Backend response too large"):
        backend.request("/internal/worker/configs")
    assert time.monotonic() - started < 1
    assert launched[0][2].returncode == -signal.SIGKILL
    assert not backend._processes


@pytest.mark.parametrize("code,error", [(6, ConnectionError), (7, ConnectionError), (28, TimeoutError)])
def test_curl_transport_exit_codes_are_sanitized(fake_curl, code, error):
    fake_curl(f"import sys; sys.stdin.read(); sys.stderr.write('secret DNS/URL'); sys.exit({code})")
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    with pytest.raises(error) as caught:
        backend.request("/internal/observations", {})
    assert "secret" not in str(caught.value)
    assert not backend._processes


@pytest.mark.parametrize("output", [b"", b"{}", b"{}\nabc", b"{}x200", b"{}\n000", b"invalid json\n200"])
def test_invalid_transport_responses_fail_closed(fake_curl, output):
    fake_curl(f"import sys; sys.stdin.read(); sys.stdout.buffer.write({output!r})")
    with pytest.raises(ValueError):
        runtime.Backend("http://backend:8000", "x" * 32).request("/internal/worker/configs")


def test_http_request_size_cap_and_invalid_json_before_spawn(fake_curl):
    launched = fake_curl("import sys; sys.stdin.read(); sys.stdout.write('\\n204')")
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    with pytest.raises(ValueError, match="Backend request too large"):
        backend.request("/internal/observations", "x" * (runtime.MAX_REQUEST_BYTES - 1))
    with pytest.raises(ValueError):
        backend.request("/internal/observations", {"faces": [float("nan")]})
    assert launched == []
    assert backend.request("/internal/observations", "x" * (runtime.MAX_REQUEST_BYTES - 2)) is None
    assert len(launched) == 1 and launched[0][2].returncode == 0


def test_transport_close_kills_parallel_children_and_prevents_new_requests(monkeypatch, fake_curl):
    monkeypatch.setattr(runtime, "HTTP_TIMEOUT", 30)
    launched = fake_curl("import time; time.sleep(30)")
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    failures = []

    def request():
        try:
            backend.request("/internal/observations", {"faces": ["x" * 200000]})
        except Exception as error:
            failures.append(error)

    threads = [threading.Thread(target=request) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        wait_until(lambda: len(launched) == 3)
        started = time.monotonic()
        backend.close()
        for thread in threads:
            thread.join(timeout=1)
        assert time.monotonic() - started < 1
        assert all(not thread.is_alive() for thread in threads)
        assert all(process.returncode == -signal.SIGKILL for _, _, process in launched)
        assert len(failures) == 3 and all(isinstance(error, ConnectionError) for error in failures)
        assert not backend._processes
        with pytest.raises(ConnectionError):
            backend.request("/internal/worker/configs")
        assert len(launched) == 3
        backend.close()
    finally:
        backend.close()
        for thread in threads:
            thread.join(timeout=1)


def test_transport_process_limit_is_bounded_and_recovers_after_reaping(monkeypatch, fake_curl):
    monkeypatch.setattr(runtime, "HTTP_TIMEOUT", 30)
    capacity = runtime.MAX_STREAMS + 3
    launched = fake_curl(["import time; time.sleep(30)"] * capacity +
                         ["import sys; sys.stdin.read(); sys.stdout.write('{}\\n200')"])
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    failures = []

    def request():
        try:
            backend.request("/internal/worker/configs")
        except ConnectionError as error:
            failures.append(error)

    threads = [threading.Thread(target=request) for _ in range(capacity)]
    for thread in threads:
        thread.start()
    try:
        wait_until(lambda: len(launched) == capacity)
        with pytest.raises(ConnectionError, match="Backend transport unavailable"):
            backend.request("/internal/worker/configs")
        assert len(launched) == capacity
        launched[0][2].kill()
        wait_until(lambda: len(failures) == 1)
        assert backend.request("/internal/worker/configs") == {}
        assert len(launched) == capacity + 1
    finally:
        backend.close()
        for thread in threads:
            thread.join(timeout=1)
    assert all(not thread.is_alive() for thread in threads)
    assert all(process.poll() is not None for _, _, process in launched)


def test_run_shutdown_reaps_transport_even_while_model_initialization_hangs(monkeypatch, fake_curl, worker_run):
    registry, release = make_registry(), threading.Event()
    monkeypatch.setattr(runtime, "HTTP_TIMEOUT", 30)
    launched = fake_curl("import sys,time; sys.stdin.read(); time.sleep(30)")
    backend = runtime.Backend("http://backend:8000", "x" * 32)

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *_):
            release.wait(5)

    def no_decoder(*_):
        raise AssertionError("Capture started before initialization completed")

    thread, shutdown, results = worker_run(registry, Engine, no_decoder, backend)
    try:
        wait_until(lambda: len(launched) == 1)
        started = time.monotonic()
        shutdown()
        thread.join(timeout=1.5)
        assert not thread.is_alive() and results == [0]
        assert time.monotonic() - started < 1.5
        assert launched[0][2].returncode == -signal.SIGKILL
        assert not backend._processes
        assert len(launched) == 1
    finally:
        release.set()


def test_worker_images_include_curl_without_changing_gpu_ffmpeg_probe():
    root = Path(__file__).resolve().parents[2]
    for filename in ("Dockerfile", "Dockerfile.gpu"):
        dockerfile = (root / "worker" / filename).read_text()
        packages = dockerfile.split("apt-get install -y --no-install-recommends", 1)[1].split("&&", 1)[0].split()
        assert "curl" in packages and "ffmpeg" in packages
    gpu = (root / "worker/Dockerfile.gpu").read_text()
    assert "assert rtsp_timeout_option() == '-stimeout'" in gpu


def test_heartbeat_and_config_continue_during_blocked_native_initialization(monkeypatch):
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    registry = runtime.Registry()

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
                self.beats.append((time.monotonic(), payload))
            else:
                assert path == "/internal/worker/configs"
                self.polls += 1
                return {"streams": [config_values(stream_id=stream_id, enabled=False, session_id=None)
                                    for stream_id in STREAM_IDS[:4]]}

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "worker.inference", SimpleNamespace(FaceEngine=SlowEngine))
    backend = ResponsiveBackend()
    threads = [threading.Thread(target=runtime.inference_loop,
                                args=(backend, registry, stop, "/models", "cpu")),
               threading.Thread(target=runtime.heartbeat_loop, args=(backend, registry, stop)),
               threading.Thread(target=runtime.poll_config, args=(backend, registry, stop))]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(1)
        time.sleep(2.15)
        assert not registry.ready
        assert {beat["stream_id"] for _, beat in backend.beats} == set(STREAM_IDS[:4])
        assert all(beat["state"] == "starting" and beat["provider"] == "unavailable"
                   for _, beat in backend.beats)
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
    registry = make_registry()
    runtime.inference_loop(Backend(), registry, threading.Event(), "/models", "cuda")
    beats = registry.heartbeats()
    assert len(beats) == 4
    for beat in beats:
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


@pytest.mark.parametrize("snapshot_before_refresh", [False, True])
def test_expired_config_cannot_revive_old_generation(snapshot_before_refresh):
    registry = make_registry()
    target, other = registry.snapshot()[:2]
    old, other_generation = target.generation, other.generation
    target.slot.put(frame(target))
    target.config_at -= runtime.CONFIG_TTL + 1
    if snapshot_before_refresh:
        assert target.snapshot()[0] is None
    target.update_config(config())
    assert not target.current(old)
    assert target.slot.take() is None
    assert other.current(other_generation)


def test_reorder_and_names_preserve_states_generations_and_frames():
    registry = make_registry()
    states = registry.snapshot()
    generations = [state.generation for state in states]
    for state in states:
        state.slot.put(frame(state))
    registry.update_configs([runtime.Config.parse({**config_values(stream_id=state.stream_id), "name": "Renamed"})
                             for state in reversed(states)])
    assert registry.snapshot() == states
    assert [state.generation for state in states] == generations
    assert all(state.slot.take().generation == generation for state, generation in zip(states, generations))


def test_removed_stream_readded_cannot_revive_old_state_or_frames():
    registry, backend = make_registry(), Backend()
    states = registry.snapshot()
    removed, survivor = states[:2]
    old_config, old_frame = removed.config, frame(removed)
    removed.slot.put(old_frame)
    survivor_generation = survivor.generation
    registry.update_configs([state.config for state in states[1:]])
    assert removed.snapshot()[0] is None and removed.slot.take() is None
    assert survivor.current(survivor_generation)
    assert old_config.stream_id not in [beat["stream_id"] for beat in registry.heartbeats()]
    removed.update_config(old_config)
    assert not runtime.submit_faces(backend, removed, old_config, old_frame, [{}])
    registry.update_configs([old_config] + [state.config for state in states[1:]])
    new = registry.streams[old_config.stream_id]
    assert new is not removed
    assert not new.current(old_frame.generation)
    assert not runtime.submit_faces(backend, new, old_config, old_frame, [{}])
    assert backend.calls == []


@pytest.mark.parametrize("changes", [{"catalog_version": 2}, {"session_id": "new"}, {"enabled": False}])
def test_config_changes_and_rollbacks_are_stream_local(changes):
    registry, backend = make_registry(), Backend()
    states = registry.snapshot()
    configs = [state.config for state in states]
    old_frames = [frame(state) for state in states]
    registry.update_configs([replace(configs[0], **changes)] + configs[1:])
    registry.update_configs(configs)
    assert not runtime.submit_faces(backend, states[0], configs[0], old_frames[0], [{}])
    for state, current, captured in zip(states[1:], configs[1:], old_frames[1:]):
        assert runtime.submit_faces(backend, state, current, captured, [{}])
    assert [payload["stream_id"] for _, payload in backend.calls] == STREAM_IDS[1:4]


def test_cannot_submit_another_streams_config_or_frame():
    registry, backend = make_registry(), Backend()
    first, second = registry.snapshot()[:2]
    assert not runtime.submit_faces(backend, first, second.config, frame(first), [{}])
    assert not runtime.submit_faces(backend, second, second.config, frame(first), [{}])
    with pytest.raises(ValueError):
        first.update_config(second.config)
    assert backend.calls == []


@pytest.mark.parametrize("response", [
    {"streams": [config_values(stream_id=stream_id) for stream_id in STREAM_IDS]},
    {"streams": [config_values(), config_values()]},
    {"streams": [config_values(), {"stream_id": STREAM_IDS[1]}]},
    {"streams": None}, {}, TimeoutError("rtsp://secret and sensitive payload"),
])
def test_bad_poll_fails_closed_all_without_partial_update_or_legacy_fallback(response):
    registry, stop = make_registry(), threading.Event()
    states = registry.snapshot()
    generations = [state.generation for state in states]
    calls = []

    def request(path):
        calls.append(path)
        stop.set()
        if isinstance(response, Exception):
            raise response
        return response

    runtime.poll_config(SimpleNamespace(request=request), registry, stop)
    assert calls == ["/internal/worker/configs"]
    assert registry.snapshot() == states
    assert all(state.snapshot()[0] is None for state in states)
    registry.update_configs([config(stream_id=stream_id) for stream_id in STREAM_IDS[:4]])
    assert all(not state.current(old) for state, old in zip(states, generations))
    assert "secret" not in json.dumps(registry.heartbeats())


def test_successful_poll_accepts_four_including_offline_and_disabled():
    registry, stop = runtime.Registry(), threading.Event()
    values = [config_values(stream_id=STREAM_IDS[index], enabled=index != 1,
                            session_id=None if index == 2 else "session-one") for index in range(4)]

    def request(path):
        assert path == "/internal/worker/configs"
        stop.set()
        return {"streams": values}

    runtime.poll_config(SimpleNamespace(request=request), registry, stop)
    assert [state.config.active for state in registry.snapshot()] == [True, False, False, True]


def test_heartbeats_are_scoped_and_one_rejection_does_not_affect_others():
    registry, stop = make_registry(), threading.Event()
    registry.provider = "CPUExecutionProvider"
    generations = [state.generation for state in registry.snapshot()]
    calls = []

    def request(path, payload):
        assert path == "/internal/worker/heartbeat"
        calls.append(payload)
        if len(calls) == 4:
            stop.set()
        if len(calls) == 1:
            raise urllib.error.HTTPError("https://secret", 409, "secret", {}, None)

    runtime.heartbeat_loop(SimpleNamespace(request=request), registry, stop)
    assert [beat["stream_id"] for beat in calls] == STREAM_IDS[:4]
    assert all(set(beat) == {"stream_id", "state", "provider", "error"} for beat in calls)
    assert all(beat["provider"] == "CPUExecutionProvider" for beat in calls)
    assert [state.generation for state in registry.snapshot()] == generations


def test_four_streams_share_one_engine_and_fair_sequential_inference(monkeypatch):
    registry, backend, stop = make_registry(), Backend(), threading.Event()
    states = registry.snapshot()
    calls, initializations = [], []
    analyzing = threading.Lock()

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *args):
            initializations.append(args)

        def analyze(self, image, threshold, is_current):
            assert analyzing.acquire(blocking=False), "Concurrent mutable detector use"
            try:
                calls.append(image.decode())
                assert is_current()
                for state in states:
                    state.slot.put(frame(state, data=state.stream_id.encode()))
                registry.update_configs([state.config for state in reversed(states)])
                if len(calls) == 12:
                    stop.set()
                return [{"source": image.decode()}]
            finally:
                analyzing.release()

    mock_engine(monkeypatch, Engine)
    for state in states:
        state.slot.put(frame(state, data=state.stream_id.encode()))
    runtime.inference_loop(backend, registry, stop, "/models", "cpu")
    assert initializations == [("/models", "cpu")]
    assert calls == STREAM_IDS[:4] * 3
    assert len(backend.calls) == 11
    assert all(payload["stream_id"] == payload["faces"][0]["source"] for _, payload in backend.calls)
    assert all(beat["provider"] == Engine.provider for beat in registry.heartbeats())


def test_stalled_transport_is_killed_and_other_streams_deliver_in_order(monkeypatch, fake_curl):
    registry, stop = make_registry(), threading.Event()
    states, analyzed = registry.snapshot(), []
    monkeypatch.setattr(runtime, "HTTP_TIMEOUT", 0.2)
    backend = runtime.Backend("http://backend:8000", "x" * 32)
    launched = fake_curl([
        "import sys,time; sys.stdin.read(); time.sleep(30)",
        *[("import json,sys; lines=sys.stdin.read().splitlines(); "
           "body=json.loads(lines[-1].split(' = ',1)[1]); "
           f"assert json.loads(body)['stream_id'] == {stream_id!r}; "
           "sys.stdout.write('{}\\n200')") for stream_id in STREAM_IDS[1:4]],
    ])

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *_):
            pass

        def analyze(self, image, threshold, is_current):
            analyzed.append(image)
            for state in states:
                # Overwriting repeatedly must not create a backlog.
                for index in range(100):
                    state.slot.put(frame(state, data=f"{state.stream_id}:{index}".encode()))
            if len(analyzed) > 1:
                assert launched[-1][2].poll() is not None, "Previous transport not reaped"
            if len(analyzed) == 5:
                stop.set()
            return [{}]

    mock_engine(monkeypatch, Engine)
    for state in states:
        state.slot.put(frame(state, data=f"{state.stream_id}:99".encode()))
    started = time.monotonic()
    runtime.inference_loop(backend, registry, stop, "/models", "cpu")
    assert time.monotonic() - started < 1.5
    assert len(launched) == 4
    assert launched[0][2].returncode == -signal.SIGKILL
    assert all(process.returncode == 0 for _, _, process in launched[1:])
    assert analyzed[:4] == [f"{stream_id}:99".encode() for stream_id in STREAM_IDS[:4]]
    assert states[0].delivery_error_at is not None
    assert all(state.delivery_error_at is None for state in states[1:])
    assert all(state.slot.take() is not None and state.slot.take() is None for state in states)
    assert not backend._processes


@pytest.mark.parametrize("failure", [TimeoutError("secret"), urllib.error.HTTPError("https://secret", 409, "secret", {}, None)])
def test_late_submission_failure_does_not_invalidate_new_or_other_streams(failure):
    registry = make_registry()
    states = registry.snapshot()
    old_config, old_frame = states[0].config, frame(states[0])
    generations = [state.generation for state in states[1:]]

    def request(path, payload):
        assert payload["stream_id"] == states[0].stream_id
        registry.update_configs([replace(old_config, catalog_version=2)] + [state.config for state in states[1:]])
        raise failure

    assert not runtime.submit_faces(SimpleNamespace(request=request), states[0], old_config, old_frame, [{}])
    assert states[0].config.catalog_version == 2
    assert all(state.current(generation) for state, generation in zip(states[1:], generations))


def test_submission_timeout_invalidates_only_source_and_never_retries():
    registry, calls = make_registry(), []
    states = registry.snapshot()
    current, captured = states[0].config, frame(states[0])
    generations = [state.generation for state in states[1:]]

    def request(path, payload):
        calls.append(payload)
        raise TimeoutError("secret URL and payload")

    backend = SimpleNamespace(request=request)
    assert not runtime.submit_faces(backend, states[0], current, captured, [{}])
    assert states[0].snapshot()[0] is None
    assert not runtime.submit_faces(backend, states[0], current, captured, [{}])
    assert len(calls) == 1
    assert all(state.current(old) for state, old in zip(states[1:], generations))


def test_delivery_error_persists_across_status_and_refresh_until_own_fresh_success():
    registry = make_registry()
    target, other = registry.snapshot()[:2]
    current, captured = target.config, frame(target)
    started = time.monotonic()
    assert not runtime.submit_faces(Backend(409), target, current, captured, [{}])
    failed_at = target.delivery_error_at
    assert failed_at >= started
    target.update_config(current)
    target.status("analyzing")
    beat = target.heartbeat("CPUExecutionProvider", None)
    assert beat["state"] == "error" and beat["error"] == "Observation delivery failed"
    assert runtime.submit_faces(Backend(), other, other.config, frame(other), [{}])
    assert target.delivery_error_at == failed_at and other.delivery_error_at is None
    target.delivery_result(captured.generation, True)
    target.delivery_result(captured.generation, False)
    assert target.delivery_error_at == failed_at
    assert not runtime.submit_faces(Backend(), target, current, frame(target), [])
    assert target.delivery_error_at == failed_at
    assert target.heartbeat("CPUExecutionProvider", "Face inference failed")["error"] == "Face inference failed"
    assert runtime.submit_faces(Backend(), target, current, frame(target), [{}])
    assert target.delivery_error_at is None
    assert target.heartbeat("CPUExecutionProvider", None)["state"] == "analyzing"


@pytest.mark.parametrize("success", [True, False])
def test_late_delivery_result_cannot_modify_new_generation_or_removed_state(success):
    registry = make_registry()
    target = registry.snapshot()[0]
    old = target.generation
    target.update_config(replace(target.config, catalog_version=2))
    target.delivery_result(target.generation, False)
    failed_at = target.delivery_error_at
    target.delivery_result(old, success)
    assert target.delivery_error_at == failed_at
    retired_generation = target.generation
    registry.update_configs([state.config for state in registry.snapshot()[1:]])
    target.delivery_result(retired_generation, success)
    assert target.delivery_error_at == failed_at


@pytest.mark.parametrize("change", ["expire", "remove", "session", "disable"])
def test_inflight_analysis_is_cancelled_only_for_target_stream(monkeypatch, change):
    registry, backend, stop = make_registry(), Backend(), threading.Event()
    states = registry.snapshot()
    target = states[0]
    generations = [state.generation for state in states[1:]]
    analyzed = []

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *_):
            pass

        def analyze(self, image, threshold, is_current):
            analyzed.append(image)
            if image == b"target":
                if change == "expire":
                    target.config_at -= runtime.CONFIG_TTL + 1
                elif change == "remove":
                    registry.update_configs([state.config for state in states[1:]])
                else:
                    target.update_config(replace(target.config, **(
                        {"session_id": "new"} if change == "session" else {"enabled": False})))
                assert not is_current()
                assert all(state.current(old) for state, old in zip(states[1:], generations))
                return [{}]
            stop.set()
            return []

    mock_engine(monkeypatch, Engine)
    target.slot.put(frame(target, data=b"target"))
    states[1].slot.put(frame(states[1], data=b"other"))
    runtime.inference_loop(backend, registry, stop, "/models", "cpu")
    assert analyzed == [b"target", b"other"]
    assert backend.calls == []


def test_stale_frames_skipped_before_and_after_inference(monkeypatch):
    registry, backend, stop = make_registry(), Backend(), threading.Event()
    states = registry.snapshot()
    stale = frame(states[1], data=b"ages-during-inference")
    calls = []

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *_):
            pass

        def analyze(self, image, threshold, is_current):
            calls.append(image)
            if len(calls) == 1:
                monkeypatch.setattr(runtime, "MAX_FRAME_AGE", -1)
                assert not is_current()
                return [{}]
            raise AssertionError("Stale frame reached inference")

    mock_engine(monkeypatch, Engine)
    states[0].slot.put(frame(states[0], age=4))
    states[1].slot.put(stale)
    thread = threading.Thread(target=runtime.inference_loop, args=(backend, registry, stop, "/models", "cpu"))
    thread.start()
    try:
        wait_until(lambda: calls)
        time.sleep(0.1)
        assert backend.calls == []
        assert calls == [b"ages-during-inference"]
    finally:
        stop.set()
        thread.join(timeout=1)


def test_inference_fatal_is_shared_and_sanitized(monkeypatch):
    registry = make_registry()

    class Engine:
        provider = "CUDAExecutionProvider"

        def __init__(self, *_):
            pass

        def analyze(self, *_):
            raise RuntimeError("CUDA failure rtsp://reader:secret@host and private face payload")

    mock_engine(monkeypatch, Engine)
    state = registry.snapshot()[2]
    state.slot.put(frame(state))
    runtime.inference_loop(Backend(), registry, threading.Event(), "/models", "cuda")
    assert all(beat == {"stream_id": stream_id, "state": "error", "provider": "CUDAExecutionProvider",
                        "error": "Face inference failed"}
               for stream_id, beat in zip(STREAM_IDS[:4], registry.heartbeats()))


def test_decoder_lifecycle_isolated_reorder_stable_and_never_overflows(worker_run):
    registry = make_registry()
    configs = [state.config for state in registry.snapshot()]
    registry.update_configs([configs[0], replace(configs[1], enabled=False, pause_reason="rtsp://secret"),
                             replace(configs[2], session_id=None), configs[3]])
    made, live_counts = [], []

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *_):
            pass

        def analyze(self, *_):
            return []

    class Decoder(threading.Thread):
        def __init__(self, url, fps, generation, slot):
            super().__init__(daemon=True)
            self.url, self.fps, self.generation, self.slot = url, fps, generation, slot
            self.stop_event, self.finish = threading.Event(), threading.Event()
            self.finish.set()
            self.last_frame, self.failed = 1, False
            made.append(self)

        def run(self):
            live_counts.append(sum(decoder.is_alive() for decoder in made))
            self.stop_event.wait()
            self.finish.wait(5)

    thread, shutdown, results = worker_run(registry, Engine, Decoder)
    try:
        wait_until(lambda: len(made) == 2 and registry.snapshot()[2].state == "offline")
        assert made[0].slot is registry.snapshot()[0].slot
        assert made[1].slot is registry.snapshot()[3].slot
        assert registry.snapshot()[1].state == "paused"
        assert registry.snapshot()[1].error == "Analysis paused"
        assert "secret" not in json.dumps(registry.heartbeats())
        registry.update_configs(configs)
        wait_until(lambda: len(made) == 4)
        delivering = registry.streams[configs[0].stream_id]
        delivering.delivery_result(delivering.generation, False)
        delivering.status("connecting")
        wait_until(lambda: delivering.state == "analyzing")
        assert registry.heartbeats()[0]["error"] == "Observation delivery failed"
        assert registry.heartbeats()[0]["state"] == "error"
        delivering.delivery_result(delivering.generation, True)
        original = list(made)
        registry.update_configs(list(reversed(configs)))
        time.sleep(0.2)
        assert made == original
        assert len({id(decoder.slot) for decoder in made}) == 4
        made[1].failed = True
        wait_until(lambda: registry.streams[configs[3].stream_id].state == "error")
        assert registry.streams[configs[0].stream_id].state == "analyzing"
        assert registry.streams[configs[3].stream_id].error == "Stream unavailable or timed out"

        target = registry.streams[configs[0].stream_id]
        old_generation = target.generation
        made[0].finish.clear()
        registry.update_configs([replace(configs[0], catalog_version=2)] + configs[1:])
        wait_until(lambda: made[0].stop_event.is_set())
        assert not target.current(old_generation)
        assert all(not decoder.stop_event.is_set() for decoder in made[1:])
        registry.update_configs(configs[1:] + [config(stream_id=STREAM_IDS[4])])
        time.sleep(0.2)
        assert len(made) == 4, "Replacement decoder started before retiring decoder exited"
        assert target.retired
        made[0].finish.set()
        wait_until(lambda: len(made) == 5)
        assert made[-1].slot is registry.streams[STREAM_IDS[4]].slot
        assert all(not decoder.stop_event.is_set() for decoder in made[1:])
        assert max(live_counts) <= 4
        registry.invalidate()
        wait_until(lambda: all(decoder.stop_event.is_set() for decoder in made))
        wait_until(lambda: all(state.state == "error" for state in registry.snapshot()))
        assert all(state.error == "Worker configuration unavailable" for state in registry.snapshot())
    finally:
        for decoder in made:
            decoder.finish.set()
        shutdown()
        thread.join(timeout=2)
    assert results == [0]
    assert all(not decoder.is_alive() for decoder in made)


def test_shutdown_reaps_four_children_in_parallel_even_with_hung_inference(monkeypatch, worker_run):
    from worker import capture

    registry, release, entered = make_registry(), threading.Event(), threading.Event()
    processes, decoders = [], []
    real_popen, real_decoder = capture.subprocess.Popen, capture.Decoder
    script = ("import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
              f"os.write(1,b'x'*{capture.FRAME_BYTES}); time.sleep(30)")
    monkeypatch.setattr(capture, "ffmpeg_command", lambda *_: [sys.executable, "-c", script])

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def decoder(*args):
        instance = real_decoder(*args)
        decoders.append(instance)
        return instance

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *_):
            pass

        def analyze(self, *_):
            assert threading.current_thread().daemon
            entered.set()
            release.wait(10)
            return []

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    thread, shutdown, results = worker_run(registry, Engine, decoder)
    try:
        wait_until(lambda: len(decoders) == 4 and all(item.last_frame > 0 for item in decoders))
        assert entered.wait(1)
        assert len(processes) == 4 and all(process.poll() is None for process in processes)
        started = time.monotonic()
        shutdown()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert time.monotonic() - started < 2.8
        assert results == [0]
        assert all(process.poll() is not None for process in processes)
        assert all(not item.is_alive() for item in decoders)
        assert all(state.slot.take() is None for state in registry.snapshot())
    finally:
        release.set()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


@pytest.mark.parametrize("stage", ["initialization", "inference"])
def test_run_reports_shared_fatal_to_all_streams_and_exits_nonzero(worker_run, capsys, stage):
    registry, backend = make_registry(), Backend()

    class Engine:
        provider = "CUDAExecutionProvider"

        def __init__(self, *_):
            if stage == "initialization":
                raise RuntimeError("secret model URL")

        def analyze(self, *_):
            raise RuntimeError("secret face payload")

    class Decoder(threading.Thread):
        def __init__(self, url, fps, generation, slot):
            super().__init__(daemon=True)
            self.generation = generation
            self.stop_event = threading.Event()
            self.last_frame, self.failed = 0, False

        def run(self):
            self.stop_event.wait()

    registry.snapshot()[0].slot.put(frame(registry.snapshot()[0]))
    thread, _, results = worker_run(registry, Engine, Decoder, backend)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert results == [1]
    beats = [payload for path, payload in backend.calls if path.endswith("heartbeat") and payload["state"] == "error"]
    assert {beat["stream_id"] for beat in beats} == set(STREAM_IDS[:4])
    assert all(beat["provider"] == ("unavailable" if stage == "initialization" else "CUDAExecutionProvider")
               for beat in beats)
    assert "secret" not in json.dumps(backend.calls)
    assert "secret" not in capsys.readouterr().out


def test_config_and_heartbeats_continue_during_slow_inference(monkeypatch):
    registry, stop = make_registry(), threading.Event()
    entered, release = threading.Event(), threading.Event()
    polls, beats = [], []

    class Engine:
        provider = "CPUExecutionProvider"

        def __init__(self, *_):
            pass

        def analyze(self, *_):
            entered.set()
            release.wait(5)
            return []

    def request(path, payload=None):
        if path.endswith("configs"):
            polls.append(time.monotonic())
            return {"streams": [config_values(stream_id=stream_id) for stream_id in STREAM_IDS[:4]]}
        beats.append((time.monotonic(), payload))

    mock_engine(monkeypatch, Engine)
    registry.snapshot()[0].slot.put(frame(registry.snapshot()[0]))
    backend = SimpleNamespace(request=request)
    threads = [threading.Thread(target=runtime.inference_loop, args=(backend, registry, stop, "/models", "cpu")),
               threading.Thread(target=runtime.poll_config, args=(backend, registry, stop)),
               threading.Thread(target=runtime.heartbeat_loop, args=(backend, registry, stop))]
    try:
        threads[0].start()
        assert entered.wait(1)
        for thread in threads[1:]:
            thread.start()
        wait_until(lambda: len(beats) >= 8)
        assert len(polls) >= 2
        for stream_id in STREAM_IDS[:4]:
            stream_beats = [(at, beat) for at, beat in beats if beat["stream_id"] == stream_id]
            assert len(stream_beats) >= 2
            assert stream_beats[1][0] - stream_beats[0][0] < 3
            assert all(beat["provider"] == Engine.provider for _, beat in stream_beats)
    finally:
        stop.set()
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=2)


def test_final_fatal_heartbeats_attempt_all_streams_despite_stuck_network(monkeypatch, worker_run):
    registry, release = make_registry(), threading.Event()
    calls = []
    monkeypatch.setattr(runtime, "HTTP_TIMEOUT", 0.05)

    class Engine:
        def __init__(self, *_):
            raise RuntimeError("secret CUDA initialization failure")

    def request(path, payload):
        if payload["state"] == "error":
            calls.append(payload)
        release.wait(5)

    def no_decoder(*_):
        raise AssertionError("Decoder started after failed initialization")

    started = time.monotonic()
    thread, _, results = worker_run(registry, Engine, no_decoder, SimpleNamespace(request=request, close=lambda: None))
    try:
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert time.monotonic() - started < 1
        assert results == [1]
        assert {beat["stream_id"] for beat in calls} == set(STREAM_IDS[:4])
    finally:
        release.set()
