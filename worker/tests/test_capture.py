import sys
import time

from worker import capture


def wait_until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for decoder")


def test_latest_slot_drops_old_frames():
    slot = capture.LatestFrame()
    for number in range(1000):
        slot.put(number)
    assert slot.take() == 999
    assert slot.take() is None
    slot.put(1)
    slot.clear()
    assert slot.take() is None


def test_ffmpeg_44_uses_client_socket_timeout(monkeypatch):
    # FFmpeg 4.4 -timeout implies listen mode; -stimeout is the client option.
    help_text = "  -timeout <int> wait for incoming connections\n  -stimeout <int> socket TCP I/O operations\n"
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **kw: type("Result", (), {"stdout": help_text})())
    if hasattr(capture, "rtsp_timeout_option"):
        capture.rtsp_timeout_option.cache_clear()
    try:
        command = capture.ffmpeg_command("rtsp://reader:secret@media/live", 2)
        assert "-stimeout" in command
        assert "-timeout" not in command
        assert command[command.index("-stimeout") + 1] == "10000000"
    finally:
        if hasattr(capture, "rtsp_timeout_option"):
            capture.rtsp_timeout_option.cache_clear()


def test_ffmpeg_modern_socket_timeout_probe_is_cached(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command == ["ffmpeg", "-hide_banner", "-h", "demuxer=rtsp"]
        assert kwargs["timeout"] == 5 and kwargs["check"] is True
        return type("Result", (), {"stdout": "  -timeout <int64> socket TCP I/O operations\n"})()

    monkeypatch.setattr(capture.subprocess, "run", run)
    capture.rtsp_timeout_option.cache_clear()
    try:
        assert capture.rtsp_timeout_option() == "-timeout"
        assert capture.rtsp_timeout_option() == "-timeout"
        assert len(calls) == 1
    finally:
        capture.rtsp_timeout_option.cache_clear()


def test_ffmpeg_bounded_output_and_no_shell():
    command = capture.ffmpeg_command("rtsp://reader:secret@media/live", 2)
    assert command[command.index("-loglevel") + 1] == "quiet"
    assert command[command.index(capture.rtsp_timeout_option()) + 1] == "10000000"
    assert "fps=2,scale=640:360" in command[command.index("-vf") + 1]
    assert command[-3:] == ["-f", "rawvideo", "pipe:1"]


def test_exact_partial_frame_read_and_utc_timestamp(monkeypatch):
    monkeypatch.setattr(capture, "FRAME_BYTES", 12)
    script = ("import os,time; os.write(1,b'abc'); time.sleep(.05); "
              "os.write(1,b'defghijklmnopqrstuvwx'); time.sleep(30)")
    monkeypatch.setattr(capture, "ffmpeg_command", lambda *_: [sys.executable, "-c", script])
    frames = []
    decoder = capture.Decoder("rtsp://secret", 2, 7, type("Slot", (), {"put": lambda _, f: frames.append(f)})())
    decoder.start()
    try:
        wait_until(lambda: len(frames) == 2)
        assert [f.data for f in frames] == [b"abcdefghijkl", b"mnopqrstuvwx"]
        assert all(f.generation == 7 and f.captured_at.endswith("+00:00") for f in frames)
        assert frames[1].received_at >= frames[0].received_at
    finally:
        decoder.close()
    assert not decoder.is_alive()


def test_hang_restarts_and_shutdown_reaps_processes(monkeypatch):
    monkeypatch.setattr(capture, "FRAME_TIMEOUT", 0.1)
    monkeypatch.setattr(capture, "ffmpeg_command", lambda *_: [sys.executable, "-c", "import time; time.sleep(30)"])
    original = capture.subprocess.Popen
    processes = []

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    decoder = capture.Decoder("rtsp://secret", 2, 0, capture.LatestFrame())
    decoder.start()
    try:
        wait_until(lambda: len(processes) >= 2)
        assert decoder.failed
    finally:
        started = time.monotonic()
        decoder.close()
    assert time.monotonic() - started < 3
    assert not decoder.is_alive()
    assert all(process.poll() is not None for process in processes)


def test_partial_frame_eof_is_never_published(monkeypatch):
    monkeypatch.setattr(capture, "FRAME_BYTES", 12)
    monkeypatch.setattr(capture, "ffmpeg_command", lambda *_: [sys.executable, "-c", "import os; os.write(1,b'abc')"])
    slot = capture.LatestFrame()
    decoder = capture.Decoder("rtsp://secret", 2, 0, slot)
    decoder.start()
    try:
        wait_until(lambda: decoder.failed)
        assert slot.take() is None
    finally:
        decoder.close()
