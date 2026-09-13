import base64
import io
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import COOKIE, create_app
from backend.config import Config
from backend.media import Bitrate
from backend.store import Store, utcnow


@pytest.fixture
def client(tmp_path):
    config = Config(admin_password="a-long-test-password", internal_token="t" * 32, data_dir=tmp_path)
    app = create_app(config, monitoring=False)
    with TestClient(app) as client:
        yield client


def login(client):
    response = client.post("/api/auth/login", json={"password": "a-long-test-password"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def online(client):
    media = client.app.state.media
    media.read_path = AsyncMock(return_value={"ready": True, "source": {"id": "publisher-1"},
                                              "readyTime": "2026-09-13T12:00:00Z", "tracks": ["H264", "MPEG4Audio"],
                                              "bytesReceived": 100})
    client.portal.call(media.refresh)
    return media


def batch(client, captured=None, vectors=None):
    cfg = client.app.state.config
    headers = {"Authorization": f"Bearer {cfg.internal_token}"}
    config = client.get("/internal/worker/config", headers=headers).json()
    image = io.BytesIO()
    Image.new("RGB", (112, 112), "red").save(image, format="JPEG")
    return {"session_id": config["session_id"], "catalog_version": config["catalog_version"],
            "captured_at": captured or utcnow(), "faces": [
                {"embedding": vector, "thumbnail": base64.b64encode(image.getvalue()).decode(),
                 "confidence": 0.98, "quality": 0.8} for vector in (vectors or [[1.0] + [0.0] * 127])]}, headers


@pytest.mark.parametrize("path", ["/api/auth/me", "/api/status", "/api/settings", "/api/faces", "/api/sessions",
                                  "/api/recordings", "/api/live/index.m3u8", "/api/faces/1/thumbnail",
                                  f"/api/recordings/{uuid.uuid4()}/file", "/internal/worker/config"])
def test_private_resources(client, path):
    assert client.get(path).status_code == 401


def test_login_csrf_logout_and_cookie(client):
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    response = login(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/auth/me").json()["csrf_token"] == response.json()["csrf_token"]
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/analysis", json={"enabled": True}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    old_cookie = client.cookies.get(COOKIE)
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.set(COOKIE, old_cookie)
    assert client.get("/api/status").status_code == 401


def test_login_rate_limit_and_cross_site(client):
    assert client.post("/api/auth/login", headers={"Sec-Fetch-Site": "cross-site"},
                       json={"password": "a-long-test-password"}).status_code == 403
    for _ in range(10):
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 429


def test_expired_session(client):
    login(client)
    for session in client.app.state.sessions.values():
        session["expires"] = time.monotonic() - 1
    assert client.get("/api/status").status_code == 401


def test_initial_status_and_credentials(client):
    assert client.app.title == "KUNAS/Labs"
    login(client)
    state = client.get("/api/status").json()
    assert not state["online"] and not state["analysis"]["enabled"]
    assert state["recording"] is None and state["face_count"] == 0
    config = client.get("/api/settings").json()
    assert config["rtmp_url"] == "rtmp://localhost:1935/live"
    assert config["stream_key"].startswith("stream?user=publisher&pass=")
    assert client.app.state.config.internal_token not in config["stream_key"]


@pytest.mark.parametrize("action,protocol,user,password,path,expected", [
    ("publish", "rtmp", "publisher", "key", "live/stream", 204),
    ("publish", "rtmp", "publisher", "wrong", "live/stream", 401),
    ("publish", "rtsp", "publisher", "key", "live/stream", 401),
    ("publish", "rtmp", "reader", "internal", "live/stream", 401),
    ("read", "hls", "reader", "internal", "live/stream", 204),
    ("read", "rtsp", "reader", "internal", "live/stream", 204),
    ("read", "rtmp", "", "", "live/stream", 401),
    ("read", "hls", "publisher", "key", "live/stream", 401),
    ("publish", "rtmp", "publisher", "key", "elsewhere", 401),
    ("api", "", "reader", "internal", "", 401),
])
def test_media_authorization(client, action, protocol, user, password, path, expected):
    if password == "key":
        password = client.app.state.store.get("stream_key")
    elif password == "internal":
        password = client.app.state.config.internal_token
    response = client.post("/internal/media/auth", json=dict(action=action, protocol=protocol,
                           user=user, password=password, path=path, query="ignored", id="ignored"))
    assert response.status_code == expected


def test_hls_authorization_accepts_mediamtx_null_connection_id(client):
    response = client.post("/internal/media/auth", json={
        "id": None, "action": "read", "protocol": "hls", "path": "live/stream",
        "user": "reader", "password": client.app.state.config.internal_token,
    })
    assert response.status_code == 204
    assert not client.app.state.admissions


def test_key_rotation(client):
    login(client)
    original = client.get("/api/settings").json()["stream_key"]
    media = online(client)
    assert client.post("/api/stream/key").status_code == 409
    media.read_path = AsyncMock(side_effect=httpx.ConnectError("offline"))
    assert client.post("/api/stream/key").status_code == 409
    media.read_path = AsyncMock(return_value={"ready": False})
    media.client.get = AsyncMock(return_value=httpx.Response(200, json={"items": [], "pageCount": 0},
                                 request=httpx.Request("GET", "http://media/v3/rtmpconns/list")))
    response = client.post("/api/stream/key")
    assert response.status_code == 200 and response.json()["stream_key"] != original


def test_bitrate_reset_and_reconnect():
    meter = Bitrate()
    assert meter.update("one", 0, 1) == 0
    assert meter.update("one", 500_000, 2) == 4
    assert meter.update("one", 100, 3) == 0
    assert meter.update(None, 0, 4) == 0
    assert meter.update("two", 500_000, 5) == 0
    assert meter.update("two", 1_000_000, 6) == 4
    for i in range(100):
        meter.update(None, 0, 7 + i)
    assert len(meter.history) == 60


def test_faces_group_sightings_filter_delete(client):
    login(client)
    media = online(client)
    client.post("/api/analysis", json={"enabled": True})
    earlier = (datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat()
    data, headers = batch(client, earlier)
    assert client.post("/internal/observations", json=data, headers=headers).json() == {"accepted": 1}
    face = client.get("/api/faces").json()["items"][0]
    assert face["match_similarity"] is None and face["sightings"] == 1
    data, headers = batch(client)
    assert client.post("/internal/observations", json=data, headers=headers).status_code == 200
    faces = client.get("/api/faces", params={"session_id": media.session_id}).json()
    assert faces["total"] == 1 and faces["items"][0]["sightings"] == 2
    assert faces["items"][0]["match_similarity"] == 1
    data, headers = batch(client)
    client.post("/internal/observations", json=data, headers=headers)
    assert client.get("/api/faces").json()["items"][0]["sightings"] == 2
    assert client.get("/api/faces?session_id=other").json()["total"] == 0
    assert client.get(face["thumbnail_url"]).headers["content-type"] == "image/jpeg"
    assert client.delete(f"/api/faces/{face['id']}").status_code == 204
    assert client.get(face["thumbnail_url"]).status_code == 404
    assert client.post("/internal/observations", json=data, headers=headers).status_code == 409
    assert client.app.state.store.db.execute("SELECT COUNT(*) FROM face_sessions").fetchone()[0] == 0


def test_face_validation_and_stale_batches(client):
    login(client)
    online(client)
    data, headers = batch(client)
    assert client.post("/internal/observations", json=data, headers=headers).status_code == 409
    client.post("/api/analysis", json={"enabled": True})
    data, headers = batch(client)
    data["faces"][0]["embedding"] = [0.0] * 128
    assert client.post("/internal/observations", json=data, headers=headers).status_code == 422
    data, headers = batch(client)
    data["faces"][0]["thumbnail"] = base64.b64encode(b"not jpeg").decode()
    assert client.post("/internal/observations", json=data, headers=headers).status_code == 422
    data, headers = batch(client, (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat())
    assert client.post("/internal/observations", json=data, headers=headers).status_code == 409
    assert client.post("/api/analysis", json={"enabled": "false"}).status_code == 422


def test_same_frame_assignments_and_retention(client):
    login(client)
    online(client)
    client.post("/api/analysis", json={"enabled": True})
    data, headers = batch(client, vectors=[[1.0] + [0.0] * 127] * 2)
    assert client.post("/internal/observations", json=data, headers=headers).json() == {"accepted": 2}
    assert client.get("/api/faces").json()["total"] == 2
    store = client.app.state.store
    with store.db:
        store.db.execute("UPDATE faces SET last_seen='2020-01-01T00:00:00+00:00'")
    store.prune()
    assert client.get("/api/faces").json()["total"] == 0


def test_face_cap_and_clear(client):
    login(client)
    online(client)
    client.post("/api/analysis", json={"enabled": True})
    store = client.app.state.store
    object.__setattr__(store.config, "max_faces", 1)
    data, headers = batch(client, vectors=[[1.0] + [0.0] * 127, [0.0, 1.0] + [0.0] * 126])
    assert client.post("/internal/observations", json=data, headers=headers).json()["accepted"] == 1
    assert client.delete("/api/faces").status_code == 204
    assert client.get("/api/faces").json()["total"] == 0


def test_worker_heartbeat(client):
    login(client)
    client.post("/api/analysis", json={"enabled": True})
    headers = {"Authorization": f"Bearer {client.app.state.config.internal_token}"}
    assert client.post("/internal/worker/heartbeat", json={"state": "analyzing", "provider": "CPUExecutionProvider"}, headers=headers).status_code == 204
    status = client.get("/api/status").json()["analysis"]
    assert status["provider"] == "CPUExecutionProvider" and status["last_seen"]
    client.app.state.media.heartbeat_at = time.monotonic() - 20
    assert client.get("/api/status").json()["analysis"]["state"] == "unavailable"


def test_recording_guards(client, monkeypatch):
    login(client)
    media = client.app.state.media
    media.read_path = AsyncMock(return_value={"ready": False})
    assert client.post("/api/recordings/start").status_code == 409
    online(client)
    monkeypatch.setattr(media, "disk_free", lambda: 0)
    assert client.post("/api/recordings/start").status_code == 409
    assert client.post("/api/recordings/stop").status_code == 204


class Recorder:
    returncode = None
    stdin = None

    def send_signal(self, value):
        self.returncode = 255

    async def wait(self):
        return self.returncode


def test_recording_lifecycle_disconnect_range_delete(client, monkeypatch):
    login(client)
    media = online(client)

    async def spawn(*args, **kwargs):
        assert "-c" in args and "copy" in args and "-n" in args
        from pathlib import Path
        Path(args[-1]).write_bytes(b"simulated-fragments" * 256)
        return Recorder()

    monkeypatch.setattr("backend.media.asyncio.create_subprocess_exec", spawn)
    response = client.post("/api/recordings/start")
    assert response.status_code == 200
    key = response.json()["id"]
    assert client.post("/api/recordings/start").status_code == 409
    assert client.delete(f"/api/recordings/{key}").status_code == 409
    assert client.get(f"/api/recordings/{key}/file").status_code == 409
    assert client.post("/api/recordings/stop").status_code == 204
    recording = client.get("/api/recordings").json()["items"][0]
    assert recording["status"] == "ready"
    response = client.get(recording["playback_url"], headers={"Range": "bytes=0-99"})
    assert response.status_code == 206 and len(response.content) == 100
    disposition = client.get(recording["download_url"]).headers["content-disposition"]
    assert disposition == f'attachment; filename="kunas-labs-{key}.mp4"'
    assert client.delete(f"/api/recordings/{key}").status_code == 204
    assert client.get(recording["playback_url"]).status_code == 404
    client.post("/api/recordings/start")
    media.read_path = AsyncMock(return_value={"ready": False})
    client.portal.call(media.refresh)
    recording = client.get("/api/recordings").json()["items"][0]
    assert recording["status"] == "interrupted" and media.recording is None
    online(client)
    assert media.recording is None


def test_restart_preserves_key_and_marks_interrupted(tmp_path):
    config = Config(admin_password="a" * 16, internal_token="b" * 32, data_dir=tmp_path)
    store = Store(config)
    key = store.get("stream_key")
    with store.db:
        store.db.execute("INSERT INTO recordings (id, started_at, status) VALUES ('test', ?, 'recording')", (utcnow(),))
        store.set("analysis_enabled", 1)
    store.db.close()
    store = Store(config)
    assert store.get("stream_key") == key and store.get("analysis_enabled") == "0"
    assert store.db.execute("SELECT status FROM recordings").fetchone()[0] == "interrupted"
    store.db.close()


def test_hls_proxy_credentials_queries_and_paths(client):
    login(client)
    seen = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"#EXTM3U\nsegment.mp4\n"

    async def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"Content-Type": "application/vnd.apple.mpegurl"}, stream=Chunks())

    media = client.app.state.media
    client.portal.call(media.client.aclose)
    media.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = client.get("/api/live/index.m3u8?_HLS_msn=5&pass=attacker")
    assert response.status_code == 200 and response.content.startswith(b"#EXTM3U")
    assert str(seen[0].url) == "http://mediamtx:8888/live/stream/index.m3u8?_HLS_msn=5"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/live/evil.html").status_code == 404
    assert client.get("/api/live/http:%2F%2Fevil").status_code == 404


def test_body_limit_and_config_validation(client):
    assert client.post("/api/auth/login", content=b"x" * (3 * 1024 * 1024 + 1)).status_code == 413
    with pytest.raises(ValueError):
        Config(admin_password="short", internal_token="b" * 32)
    with pytest.raises(ValueError):
        Config(admin_password="a" * 16, internal_token="b" * 32, public_host="host:1935")


def test_rotation_rejects_publisher_without_tracks(client):
    login(client)
    media = client.app.state.media
    media.read_path = AsyncMock(return_value={"ready": False, "source": {"id": "pending"}})
    old_key = client.app.state.store.get("stream_key")
    assert client.post("/api/stream/key").status_code == 409
    assert client.app.state.store.get("stream_key") == old_key


def test_rotation_closes_previously_admitted_connections(client):
    login(client)
    media = client.app.state.media
    media.read_path = AsyncMock(return_value={"ready": False})
    media.client.get = AsyncMock(return_value=httpx.Response(200, json={"items": [{"id": "pending"}], "pageCount": 1},
                                 request=httpx.Request("GET", "http://media/v3/rtmpconns/list")))
    media.client.post = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("POST", "http://media/kick")))
    old_key = client.app.state.store.get("stream_key")
    assert client.post("/internal/media/auth", json={"id": "pending", "action": "publish", "protocol": "rtmp",
                       "user": "publisher", "password": old_key, "path": "live/stream"}).status_code == 204
    assert client.post("/api/stream/key").status_code == 200
    assert client.app.state.store.get("stream_key") != old_key
    assert media.client.post.call_args.args[0].endswith("/v3/rtmpconns/kick/pending")
    assert client.post("/internal/media/auth", json={"action": "publish", "protocol": "rtmp",
                       "user": "publisher", "password": old_key, "path": "live/stream"}).status_code == 401


def test_publisher_auth_rejects_during_rotation_without_waiting(client):
    import asyncio

    async def scenario():
        media, store = client.app.state.media, client.app.state.store
        old_key = store.get("stream_key")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as request:
            async with client.app.state.ingest_lock:
                client.app.state.admission_blocks.add("stream")
                pending = asyncio.create_task(request.post("/internal/media/auth", json={
                    "action": "publish", "protocol": "rtmp", "user": "publisher",
                    "password": old_key, "path": "live/stream"}))
                assert (await asyncio.wait_for(pending, timeout=0.5)).status_code == 401
                with store.db:
                    store.set("stream_key", "replacement-key")
                client.app.state.admission_blocks.remove("stream")
            assert (await request.post("/internal/media/auth", json={
                "action": "publish", "protocol": "rtmp", "user": "publisher",
                "password": old_key, "path": "live/stream"})).status_code == 401

    client.portal.call(scenario)


def test_session_invalidated_before_recorder_cleanup(client, monkeypatch):
    login(client)
    media = online(client)
    client.post("/api/analysis", json={"enabled": True})
    data, headers = batch(client)
    media.recording = {"id": "pending", "started_at": utcnow()}

    async def cleanup(*args, **kwargs):
        assert not media.online and media.session_id is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as request:
            assert (await request.get("/internal/worker/config", headers=headers)).json()["session_id"] is None
            assert (await request.post("/internal/observations", json=data, headers=headers)).status_code == 409
        media.recording = None

    monkeypatch.setattr(media, "stop_locked", cleanup)
    media.read_path = AsyncMock(return_value={"ready": False})
    client.portal.call(media.refresh)


def test_disk_pause_reaches_worker_and_dashboard(client, monkeypatch):
    login(client)
    media = online(client)
    client.post("/api/analysis", json={"enabled": True})
    headers = {"Authorization": f"Bearer {client.app.state.config.internal_token}"}
    monkeypatch.setattr(media, "disk_free", lambda: 0)
    worker = client.get("/internal/worker/config", headers=headers).json()
    assert worker["enabled"] is False and worker["pause_reason"] == "Low disk space"
    assert client.get("/api/status").json()["analysis"]["state"] == "paused"
    monkeypatch.setattr(media, "disk_free", lambda: 10 * 1024 ** 3)
    worker = client.get("/internal/worker/config", headers=headers).json()
    assert worker["enabled"] is True and worker["pause_reason"] is None


def test_all_recordings_remain_manageable(client):
    login(client)
    store = client.app.state.store
    with store.db:
        store.db.executemany("INSERT INTO recordings (id, started_at, ended_at, status) VALUES (?, ?, ?, 'ready')",
                             [(str(uuid.uuid4()), utcnow(), utcnow()) for _ in range(501)])
    assert len(client.get("/api/recordings").json()["items"]) == 501


def test_umbrel_data_init_preserves_files_and_limits_ownership_changes(tmp_path, monkeypatch):
    from backend.init_data import initialize

    root = tmp_path / "data"
    root.mkdir()
    marker = root / "existing-recording.mp4"
    marker.write_bytes(b"preserve")
    changed = []
    monkeypatch.setattr("backend.init_data.os.chown", lambda path, uid, gid: changed.append((path, uid, gid)))
    initialize(root)
    assert changed == [(root, 65532, 65532), (root / "recordings", 65532, 65532)]
    assert marker.read_bytes() == b"preserve"
    assert root.stat().st_mode & 0o777 == 0o700
    initialize(root)
    assert marker.read_bytes() == b"preserve"
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        initialize(link)
