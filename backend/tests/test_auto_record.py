"""Automatic-recording contract, with no MediaMTX, viewer, worker, or FFmpeg."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.media as media_module
from backend.app import create_app
from backend.config import Config
from backend.media import Media
from backend.store import Store, utcnow


class Recorder:
    def __init__(self):
        self.returncode = None
        self.stdin = None
        self.signals = []
        self.waits = 0

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = 255

    async def wait(self):
        self.waits += 1
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture(autouse=True)
def recorders(monkeypatch):
    state = SimpleNamespace(calls=[], processes=[], error=None, entered=None, release=None)

    async def spawn(*args, **kwargs):
        state.calls.append((args, kwargs))
        assert args[0] == "ffmpeg"
        assert args[args.index("-c") + 1] == "copy"
        assert args[args.index("-rtsp_transport") + 1] == "tcp"
        assert "-n" in args and "-y" not in args
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        if state.entered is not None:
            state.entered.set()
            await state.release.wait()
        if state.error is not None:
            raise state.error
        # Finalization must see a real, nonempty fragment file, not a mocked stat.
        Path(args[-1]).write_bytes(b"fake-mp4-fragments" * 256)
        process = Recorder()
        state.processes.append(process)
        return process

    monkeypatch.setattr(media_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(Media, "disk_free", lambda self: 10 * 1024 ** 3)
    monkeypatch.setattr(Media, "read_path", AsyncMock(return_value={"ready": False}))
    return state


@pytest.fixture
def config(tmp_path):
    # Deliberately omit auto_record: these tests exercise the default-on policy.
    return Config(admin_password="a-long-test-password", internal_token="t" * 32, data_dir=tmp_path)


@pytest.fixture
def client(config, recorders):
    with TestClient(create_app(config, monitoring=False)) as client:
        yield client


@pytest.fixture
def media(config, recorders):
    store = Store(config)
    controller = Media(config, store)
    try:
        yield controller
    finally:
        asyncio.run(controller.close())
        store.db.close()


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0, free=10 * 1024 ** 3)
    # Replace only Media's module reference, never time.monotonic on global time.
    monkeypatch.setattr(media_module, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(Media, "disk_free", lambda self: state.free)
    return state


def ready(media, *, publisher="publisher-1", ready_time="2026-09-13T12:00:00Z", tracks=None):
    path = {"ready": True, "source": {"id": publisher} if publisher else None,
            "readyTime": ready_time, "tracks": ["H264", "MPEG4Audio"] if tracks is None else tracks,
            "bytesReceived": 100}
    media.read_path = AsyncMock(return_value=path)
    return path


def login(client):
    response = client.post("/api/auth/login", json={"password": client.app.state.config.admin_password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def four_streams(client):
    login(client)
    for index in range(1, 4):
        response = client.post("/api/streams", json={"name": f"Camera {index}"})
        assert response.status_code == 201, response.text
    medias = list(client.app.state.medias.values())
    assert len(medias) == 4
    for index, media in enumerate(medias):
        ready(media, publisher=f"publisher-{index}")
        client.portal.call(media.refresh)
    return medias


def status(client, media):
    response = client.get("/api/status", params={"stream_id": media.stream_id})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("value,expected", [(None, True), ("true", True), ("false", False),
                                           (" TRUE ", True), ("False", False)])
def test_auto_record_environment_policy(monkeypatch, value, expected):
    for name in ("AUTO_RECORD", "PUBLIC_HOST", "DATA_DIR", "RTMP_PORT", "COOKIE_SECURE", "MIN_FREE_GB",
                 "FACE_RETENTION_DAYS", "MAX_FACES", "MATCH_THRESHOLD", "DETECTION_THRESHOLD", "ANALYSIS_FPS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("INTERNAL_TOKEN", "t" * 32)
    if value is not None:
        monkeypatch.setenv("AUTO_RECORD", value)
    assert Config.from_env().auto_record is expected


@pytest.mark.parametrize("value", ["", "1", "0", "yes", "no", "invalid"])
def test_invalid_auto_record_environment_rejected(monkeypatch, value):
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-test-password")
    monkeypatch.setenv("INTERNAL_TOKEN", "t" * 32)
    monkeypatch.setenv("AUTO_RECORD", value)
    with pytest.raises(ValueError, match="AUTO_RECORD must be true or false"):
        Config.from_env()


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None])
def test_config_rejects_non_boolean_auto_record(tmp_path, value):
    with pytest.raises(ValueError, match="AUTO_RECORD must be true or false"):
        Config(admin_password="a-long-test-password", internal_token="t" * 32,
               data_dir=tmp_path, auto_record=value)


def test_first_ready_monitor_tick_records_without_viewer_or_start_api(media, recorders):
    async def scenario():
        assert media.config.auto_record is True
        await media.refresh()
        assert media.recording_state == "waiting" and not recorders.calls
        ready(media)
        task = asyncio.create_task(media.monitor())
        try:
            async def started():
                while media.recording is None:
                    await asyncio.sleep(0)

            await asyncio.wait_for(started(), timeout=2)
            assert media.recording_state == "recording"
            assert media.store.get("analysis_enabled") == "0"
            row, = media.recordings()["items"]
            assert row["id"] == media.recording["id"]
            assert row["session_id"] == media.session_id
            assert row["status"] == "recording" and row["size_bytes"] > 1024
            assert len(recorders.calls) == 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_four_independent_automatic_recorders_and_stop_latch(client, recorders):
    medias = four_streams(client)
    recordings = {media.stream_id: dict(media.recording) for media in medias}
    assert len({item["id"] for item in recordings.values()}) == 4
    assert len({id(media.process) for media in medias}) == 4
    assert len({media.session_id for media in medias}) == 4
    for media, (args, _) in zip(medias, recorders.calls):
        reader = urlsplit(args[args.index("-i") + 1])
        assert reader.path == f"/live/{media.stream_id}"
        assert reader.username == "reader"
        assert unquote(reader.password) == media.config.internal_token
        assert media.store.get("stream_key", media.stream_id) not in reader.geturl()
        state = status(client, media)
        assert state["auto_record"] is True
        assert state["recording_state"] == "recording" and state["recording_error"] is None
        assert state["recording"] == recordings[media.stream_id]
        assert state["analysis"]["enabled"] is False
        settings = client.get("/api/settings", params={"stream_id": media.stream_id}).json()
        assert settings["auto_record"] is True and settings["analysis_enabled"] is False
        row, = media.recordings()["items"]
        assert row["stream_id"] == media.stream_id and row["session_id"] == media.session_id

    stopped = medias[1]
    for _ in range(2):
        assert client.post("/api/recordings/stop", params={"stream_id": stopped.stream_id}).status_code == 204
    for _ in range(8):
        for media in medias:
            client.portal.call(media.refresh)
            if media is stopped:
                assert status(client, media)["recording_state"] == "stopped"
                assert media.recording is None
            else:
                assert media.recording == recordings[media.stream_id]
                assert media.process.returncode is None
    assert len(recorders.calls) == 4
    assert recorders.processes[1].waits == 1
    row, = stopped.recordings()["items"]
    assert row["status"] == "ready" and row["playback_url"] and row["ended_at"]
    response = client.post("/api/recordings/start", params={"stream_id": stopped.stream_id})
    assert response.status_code == 200 and response.json()["id"] != row["id"]
    assert stopped.stopped_source is None and len(recorders.calls) == 5


@pytest.mark.parametrize("auto_first", [True, False])
def test_poll_and_concurrent_manual_start_return_one_active_recording(client, recorders, auto_first):
    login(client)
    media = client.app.state.media
    ready(media)

    async def scenario():
        recorders.entered, recorders.release = asyncio.Event(), asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://testserver", cookies=client.cookies,
                                     headers={"X-CSRF-Token": client.headers["X-CSRF-Token"]}) as request:
            first = asyncio.create_task(media.refresh() if auto_first else request.post("/api/recordings/start"))
            tasks = [first]
            try:
                await asyncio.wait_for(recorders.entered.wait(), timeout=2)
                tasks.extend(asyncio.create_task(request.post("/api/recordings/start")) for _ in range(6))
                tasks.extend(asyncio.create_task(media.refresh()) for _ in range(6))
                await asyncio.sleep(0)
                assert len(recorders.calls) == 1
            finally:
                recorders.release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            responses = [result for result in results if isinstance(result, httpx.Response)]
            assert len(responses) == (6 if auto_first else 7)
            for response in responses:
                assert response.status_code == 200, response.text
                assert response.json() == media.recording

    client.portal.call(scenario)
    for _ in range(5):
        client.portal.call(media.refresh)
        response = client.post("/api/recordings/start")
        assert response.status_code == 200 and response.json() == media.recording
    assert len(recorders.calls) == len(media.recordings()["items"]) == 1


@pytest.mark.parametrize("change", ["publisher", "ready_time"])
def test_stop_survives_outages_and_same_fingerprint_offline_recovery(client, recorders, change):
    medias = four_streams(client)
    media = medias[0]
    original = dict(media.recording)
    fingerprint = media.source
    assert client.post("/api/recordings/stop").status_code == 204
    request = httpx.Request("GET", media.config.media_api)
    outages = [httpx.ConnectError("temporary outage"),
               httpx.HTTPStatusError("temporary HTTP 503", request=request,
                                     response=httpx.Response(503, request=request)),
               {"ready": False}]
    for outage in outages:
        media.read_path = AsyncMock(side_effect=outage) if isinstance(outage, Exception) else AsyncMock(return_value=outage)
        for _ in range(3):
            client.portal.call(media.refresh)
            assert media.recording is None and media.recording_state == "stopped"
            assert media.stopped_source == media.last_source == fingerprint
        ready(media, publisher="publisher-0")
        for _ in range(3):
            for controller in medias:
                client.portal.call(controller.refresh)
            assert media.online and media.recording is None
            assert all(controller.recording and controller.process.returncode is None for controller in medias[1:])
        assert len(recorders.calls) == 4
    ready(media, **({"publisher": "new-publisher"} if change == "publisher" else
                   {"publisher": "publisher-0", "ready_time": "2026-09-13T12:01:00Z"}))
    client.portal.call(media.refresh)
    assert media.recording["id"] != original["id"]
    assert media.stopped_source is media.failed_source is media.recording_error is None
    assert media.last_source != fingerprint and len(recorders.calls) == 5
    assert len(media.recordings()["items"]) == 2


@pytest.mark.parametrize("change", ["publisher", "ready_time", "no_publisher_id"])
def test_genuine_reconnect_finalizes_old_file_and_starts_new(media, recorders, change):
    async def scenario():
        ready(media, publisher=None if change == "no_publisher_id" else "publisher-1")
        await media.refresh()
        old = dict(media.recording)
        old_session = media.session_id
        old_target = media.recordings_dir / f"{old['id']}.mp4"
        content = old_target.read_bytes()
        media.read_path = AsyncMock(return_value={"ready": False})
        await media.refresh()
        assert media.recording is None and media.recording_state == "waiting"
        ready(media, publisher="publisher-2" if change == "publisher" else
              (None if change == "no_publisher_id" else "publisher-1"),
              ready_time="2026-09-13T12:00:00Z" if change == "publisher" else "2026-09-13T12:01:00Z")
        await media.refresh()
        assert media.recording["id"] != old["id"] and media.session_id != old_session
        rows = {row["id"]: row for row in media.recordings()["items"]}
        assert len(rows) == len(recorders.calls) == 2
        assert rows[old["id"]]["status"] == "interrupted"
        assert rows[old["id"]]["ended_at"] and rows[old["id"]]["playback_url"]
        assert old_target.read_bytes() == content

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["codec", "spawn", "exit"])
@pytest.mark.parametrize("recovery", ["manual", "publisher", "ready_time"])
def test_failures_latch_sanitized_errors_without_tick_retries(client, recorders, failure, recovery):
    login(client)
    media = client.app.state.media
    secret = media.reader_url + "?pass=" + media.store.get("stream_key")
    ready(media, tracks=["H265", "MPEG4Audio"] if failure == "codec" else None)
    if failure == "spawn":
        recorders.error = OSError("cannot execute " + secret)
    client.portal.call(media.refresh)
    if failure == "exit":
        media.process.returncode = 1
        client.portal.call(media.refresh)
    expected = {"codec": "Recording requires H.264 video; set your encoder to H.264/AAC",
                "spawn": "FFmpeg could not start", "exit": "Recorder exited unexpectedly"}[failure]
    fingerprint = media.last_source
    assert media.recording is None and media.process is None
    assert media.failed_source == fingerprint
    state = status(client, media)
    assert state["recording_state"] == "error"
    assert state["recording_error"] == media.recording_error == expected
    before = media.recordings()["items"]
    assert len(before) == (0 if failure == "codec" else 1)
    if before:
        assert before[0]["status"] == ("error" if failure == "spawn" else "interrupted")
        assert before[0]["ended_at"] and before[0]["error"] == expected
    calls = len(recorders.calls)
    # Even if the underlying problem is repaired, the same publisher must not retry.
    recorders.error = None
    ready(media)
    for _ in range(8):
        client.portal.call(media.refresh)
        assert media.recording_state == "error" and media.recording is None
    for path in (httpx.ConnectError(secret), {"ready": False}):
        media.read_path = AsyncMock(side_effect=path) if isinstance(path, Exception) else AsyncMock(return_value=path)
        client.portal.call(media.refresh)
        ready(media)
        client.portal.call(media.refresh)
        assert media.failed_source == fingerprint and media.recording_state == "error"
    assert len(recorders.calls) == calls and media.recordings()["items"] == before
    visible = str(status(client, media)) + str(media.recordings())
    for private in (secret, media.config.internal_token, media.store.get("stream_key"), "rtsp://", "reader:"):
        assert private not in visible
    if recovery == "manual":
        response = client.post("/api/recordings/start")
        assert response.status_code == 200, response.text
        assert response.json() == media.recording
        assert media.last_source == fingerprint
    else:
        ready(media, **({"publisher": "publisher-2"} if recovery == "publisher" else
                       {"ready_time": "2026-09-13T12:01:00Z"}))
        client.portal.call(media.refresh)
        assert media.last_source != fingerprint
    assert media.recording_state == "recording"
    assert media.failed_source is media.stopped_source is media.recording_error is None
    assert status(client, media)["recording_error"] is None
    assert len(recorders.calls) == calls + 1
    assert len(media.recordings()["items"]) == len(before) + 1
    assert all(row["id"] != media.recording["id"] for row in before)


def test_manual_opt_out_never_auto_starts_but_explicit_start_works(config, recorders):
    manual_config = Config(admin_password=config.admin_password, internal_token=config.internal_token,
                           data_dir=config.data_dir, auto_record=False)
    with TestClient(create_app(manual_config, monitoring=False)) as client:
        login(client)
        media = client.app.state.media
        for publisher in ("publisher-1", "publisher-2"):
            ready(media, publisher=publisher)
            for _ in range(5):
                client.portal.call(media.refresh)
            state = status(client, media)
            assert state["auto_record"] is False and state["recording_state"] == "manual"
            assert state["recording"] is state["recording_error"] is None
        assert client.get("/api/settings").json()["auto_record"] is False
        assert not recorders.calls and media.recordings()["items"] == []
        response = client.post("/api/recordings/start")
        assert response.status_code == 200 and status(client, media)["recording_state"] == "recording"
        assert client.post("/api/recordings/stop").status_code == 204
        ready(media, publisher="publisher-3")
        client.portal.call(media.refresh)
        assert media.recording is None and len(recorders.calls) == 1


def test_restart_records_already_active_feed_and_preserves_interrupted_metadata(config, recorders):
    store = Store(config)
    session_id, recording_id = "old-session", "11111111-1111-4111-8111-111111111111"
    started = "2026-09-13T12:00:00+00:00"
    target = config.data_dir / "recordings"
    target.mkdir()
    target = target / f"{recording_id}.mp4"
    content = b"existing-crash-fragments" * 256
    target.write_bytes(content)
    stat = target.stat()
    key = store.get("stream_key")
    with store.db:
        store.db.execute("INSERT INTO sessions (id, started_at) VALUES (?, ?)", (session_id, started))
        store.db.execute("INSERT INTO recordings (id, session_id, started_at, status) VALUES (?, ?, ?, 'recording')",
                         (recording_id, session_id, started))
        store.set("analysis_enabled", 1)
    store.db.close()
    with TestClient(create_app(config, monitoring=False)) as client:
        media = client.app.state.media
        ready(media)
        # No browser login or Start request precedes the restart monitor tick.
        client.portal.call(media.refresh)
        assert media.recording["id"] != recording_id and len(recorders.calls) == 1
        assert media.store.get("stream_key") == key and media.store.get("analysis_enabled") == "0"
        rows = {row["id"]: row for row in media.recordings()["items"]}
        assert len(rows) == 2
        old = rows[recording_id]
        assert old["session_id"] == session_id and old["started_at"] == started
        assert old["stream_id"] == "stream" and old["status"] == "interrupted" and old["ended_at"]
        assert old["error"] == "Backend restarted before recording was finalized"
        assert old["playback_url"] and target.read_bytes() == content
        assert (target.stat().st_ino, target.stat().st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns)
        assert media.store.db.execute("SELECT ended_at FROM sessions WHERE id=?", (session_id,)).fetchone()[0]


@pytest.mark.parametrize("reserve_gb,headroom", [(0.05, 16 * 1024 ** 2),
                                                (2, int(2 * 1024 ** 3) // 10), (10, 256 * 1024 ** 2)])
def test_disk_resume_threshold_includes_bounded_headroom(config, reserve_gb, headroom):
    cfg = Config(admin_password=config.admin_password, internal_token=config.internal_token,
                 data_dir=config.data_dir, min_free_gb=reserve_gb)
    store = Store(cfg)
    media = Media(cfg, store)
    try:
        assert media.disk_resume_bytes == media.min_free_bytes + headroom
        assert media_module.DISK_RECOVERY_SECONDS == 5
    finally:
        asyncio.run(media.close())
        store.db.close()


@pytest.mark.parametrize("initially_active", [False, True])
def test_low_disk_requires_headroom_and_five_continuous_seconds(media, clock, recorders, initially_active):
    async def scenario():
        ready(media)
        if initially_active:
            await media.refresh()
            assert media.recording_state == "recording"
        clock.free = media.min_free_bytes - 1
        await media.refresh()
        assert media.recording is None and media.recording_state == "disk_paused"
        assert media.failed_source is None and media.recording_error is None
        assert len(recorders.calls) == int(initially_active)
        if initially_active:
            row, = media.recordings()["items"]
            assert row["status"] == "interrupted" and "disk space" in row["error"]
            assert row["playback_url"] and recorders.processes[0].returncode == 255
        for free in (media.min_free_bytes, media.disk_resume_bytes - 1):
            clock.free = free
            clock.now += 100
            await media.refresh()
            assert media.disk_recovered_at is None and media.recording is None
        clock.free = media.disk_resume_bytes + 1
        clock.now = 300
        await media.refresh()
        assert media.disk_recovered_at == 300 and media.recording is None
        clock.now = 304.999
        await media.refresh()
        assert media.recording is None
        # Dropping below headroom (not below reserve) invalidates the stable interval.
        clock.free = media.disk_resume_bytes - 1
        await media.refresh()
        assert media.disk_recovered_at is None
        clock.free = media.disk_resume_bytes + 1
        clock.now = 305
        await media.refresh()
        for now in (306, 307, 308, 309, 309.999):
            clock.now = now
            await media.refresh()
            assert media.recording is None and media.recording_state == "disk_paused"
        clock.now = 310
        await media.refresh()
        assert media.recording_state == "recording" and not media.disk_paused
        assert media.disk_recovered_at is None and media.recording_error is None
        assert len(recorders.calls) == int(initially_active) + 1
        assert len(media.recordings()["items"]) == int(initially_active) + 1

    asyncio.run(scenario())


def test_media_api_outage_resets_disk_recovery_interval(media, clock, recorders):
    async def scenario():
        ready(media)
        clock.free = media.min_free_bytes - 1
        await media.refresh()
        clock.free = media.disk_resume_bytes + 1
        clock.now = 10
        await media.refresh()
        clock.now = 14
        await media.refresh()
        assert media.disk_recovered_at == 10 and media.recording is None
        media.read_path = AsyncMock(side_effect=httpx.ConnectError("temporary outage"))
        await media.refresh()
        assert not media.available and media.disk_recovered_at is None
        clock.now = 100
        ready(media)
        await media.refresh()
        assert media.disk_recovered_at == 100 and media.recording is None
        clock.now = 104.999
        await media.refresh()
        assert media.recording is None
        clock.now = 105
        await media.refresh()
        assert media.recording_state == "recording" and len(recorders.calls) == 1

    asyncio.run(scenario())


def test_reconnecting_publisher_cannot_bypass_disk_recovery(media, clock, recorders):
    async def scenario():
        ready(media)
        await media.refresh()
        clock.free = media.min_free_bytes - 1
        await media.refresh()
        assert media.disk_paused and media.recording is None
        clock.free = media.min_free_bytes + 1
        for index in range(3):
            ready(media, publisher=f"flapping-{index}")
            clock.now += 20
            await media.refresh()
            assert media.disk_paused and media.recording is None
        assert len(recorders.calls) == 1
        clock.free = media.disk_resume_bytes + 1
        await media.refresh()
        clock.now += 4
        ready(media, publisher="new-publisher")
        await media.refresh()
        clock.now += 4.9
        await media.refresh()
        assert media.recording is None
        clock.now += 0.1
        await media.refresh()
        assert media.recording and len(recorders.calls) == 2

    asyncio.run(scenario())


def test_stop_during_media_api_outage_stays_suppressed_on_recovery(client, recorders):
    media = client.app.state.media
    login(client)
    assert not status(client, media)["can_stop_recording"]
    ready(media)
    client.portal.call(media.refresh)
    assert status(client, media)["can_stop_recording"]
    media.read_path = AsyncMock(side_effect=httpx.ConnectError("temporary outage"))
    client.portal.call(media.refresh)
    report = status(client, media)
    assert not report["media_available"] and report["can_stop_recording"]
    assert report["recording"] is None
    assert client.post("/api/recordings/stop").status_code == 204
    assert not status(client, media)["can_stop_recording"]
    ready(media)
    for _ in range(3):
        client.portal.call(media.refresh)
    assert media.recording_state == "stopped" and len(recorders.calls) == 1
    ready(media, publisher="different")
    client.portal.call(media.refresh)
    assert media.recording and len(recorders.calls) == 2


@pytest.mark.parametrize("initially_active", [False, True])
def test_stop_while_disk_paused_prevents_recovery_until_manual_start(media, clock, recorders, initially_active):
    async def scenario():
        ready(media)
        if initially_active:
            await media.refresh()
        clock.free = media.min_free_bytes - 1
        await media.refresh()
        assert media.recording_state == "disk_paused"
        await media.stop()
        await media.stop()
        assert media.recording_state == "stopped"
        clock.free = media.disk_resume_bytes + 1
        for now in (10, 15, 20, 100):
            clock.now = now
            await media.refresh()
            assert media.recording is None and media.recording_state == "stopped"
        assert len(recorders.calls) == int(initially_active)
        result = await media.start()
        assert result == media.recording and media.recording_state == "recording"
        assert media.stopped_source is None and len(recorders.calls) == int(initially_active) + 1

    asyncio.run(scenario())


def test_manual_start_can_resume_above_reserve_without_automatic_delay(media, clock, recorders):
    async def scenario():
        ready(media)
        clock.free = media.min_free_bytes - 1
        await media.refresh()
        with pytest.raises(HTTPException) as error:
            await media.start()
        assert error.value.status_code == 409 and not recorders.calls
        clock.free = media.min_free_bytes + 1
        await media.refresh()
        assert media.recording_state == "disk_paused" and not recorders.calls
        assert await media.start() == media.recording
        assert media.recording_state == "recording" and len(recorders.calls) == 1

    asyncio.run(scenario())


def test_shared_low_disk_stops_all_four_recorders_and_rejects_all_stream_frames(client, monkeypatch, recorders):
    medias = four_streams(client)
    headers = {"Authorization": f"Bearer {client.app.state.config.internal_token}"}
    frames = {}
    for media in medias:
        response = client.post("/api/analysis", params={"stream_id": media.stream_id}, json={"enabled": True})
        assert response.status_code == 200
        frames[media.stream_id] = {"stream_id": media.stream_id, "session_id": media.session_id,
                                   "catalog_version": int(media.store.get("catalog_version", media.stream_id)),
                                   "captured_at": utcnow(), "faces": []}
        assert client.post("/internal/observations", json=frames[media.stream_id], headers=headers).status_code == 200
    monkeypatch.setattr(Media, "disk_free", lambda self: self.min_free_bytes - 1)
    for media in medias:
        client.portal.call(media.refresh)
        state = status(client, media)
        assert state["recording"] is None and state["recording_state"] == "disk_paused"
        assert state["analysis"]["enabled"] is True and state["analysis"]["state"] == "paused"
        assert state["recording_error"] is None
        assert "Low disk" in state["warning"]
        row, = media.recordings()["items"]
        assert row["status"] == "interrupted" and row["size_bytes"] > 1024
        response = client.post("/internal/observations", json=frames[media.stream_id], headers=headers)
        assert response.status_code == 409 and "insufficient disk space" in response.json()["detail"]
    configs = client.get("/internal/worker/configs", headers=headers).json()["streams"]
    assert len(configs) == 4
    assert all(item["enabled"] is False and item["pause_reason"] == "Low disk space" for item in configs)
    assert len(recorders.calls) == 4 and all(process.returncode == 255 for process in recorders.processes)


def test_archived_status_keeps_policy_and_preserves_automatic_recording_history(client, recorders):
    login(client)
    response = client.post("/api/streams", json={"name": "Archive camera"})
    assert response.status_code == 201
    stream_id = response.json()["id"]
    media = client.app.state.medias[stream_id]
    ready(media)
    client.portal.call(media.refresh)
    recording_id = media.recording["id"]
    target = media.recordings_dir / f"{recording_id}.mp4"
    content = target.read_bytes()
    media.read_path = AsyncMock(return_value={"ready": False})
    client.portal.call(media.refresh)
    before = media.recordings()["items"]
    client.app.state.media.client.get = AsyncMock(return_value=httpx.Response(
        200, json={"items": [], "pageCount": 1}, request=httpx.Request("GET", "http://media/v3/rtmpconns/list")))
    assert client.delete(f"/api/streams/{stream_id}").status_code == 204
    assert media.recording_state == "archived" and stream_id not in client.app.state.medias
    state = status(client, media)
    assert state["auto_record"] is True and state["recording_state"] == "archived"
    assert state["recording_error"] is state["recording"] is None
    settings = client.get("/api/settings", params={"stream_id": stream_id}).json()
    assert settings["auto_record"] is True and settings["archived"] is True and settings["stream_key"] == ""
    assert client.get("/api/recordings", params={"stream_id": stream_id}).json()["items"] == before
    assert target.read_bytes() == content and client.get(before[0]["playback_url"]).content == content
    ready(media, publisher="new-publisher")
    client.portal.call(media.refresh)
    assert len(recorders.calls) == 1
    assert client.post("/api/recordings/start", params={"stream_id": stream_id}).status_code == 409


@pytest.mark.parametrize("manual", [False, True])
def test_recording_insert_failure_never_launches_untracked_ffmpeg(media, recorders, manual):
    async def scenario():
        ready(media)
        media.store.db.executescript("""
            CREATE TRIGGER reject_recording BEFORE INSERT ON recordings
            BEGIN SELECT RAISE(ABORT, 'private database write failure'); END;
        """)
        if manual:
            with pytest.raises(sqlite3.IntegrityError, match="private database write failure"):
                await media.start()
        else:
            await media.refresh()
        assert not recorders.calls and not recorders.processes
        assert media.recording is media.process is None
        assert media.recordings()["items"] == []
        assert list(media.recordings_dir.iterdir()) == []
        assert media.recording_state == "error" and media.failed_source == media.last_source
        assert media.recording_error and "private database" not in media.recording_error
        for _ in range(8):
            await media.refresh()
        assert not recorders.calls and media.recordings()["items"] == []
        media.store.db.execute("DROP TRIGGER reject_recording")
        await media.refresh()
        assert not recorders.calls
        assert await media.start() == media.recording
        assert len(recorders.calls) == 1 and len(media.recordings()["items"]) == 1

    asyncio.run(scenario())
