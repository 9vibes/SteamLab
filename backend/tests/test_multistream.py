"""Four-stream contract regressions, without a worker, FFmpeg, or MediaMTX server."""

import asyncio
import base64
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import COOKIE, create_app
from backend.config import Config
from backend.media import Media
from backend.store import Store, utcnow
from backend.tests.test_app import Recorder, batch, client, login


@pytest.fixture(autouse=True)
def ample_disk(monkeypatch):
    monkeypatch.setattr(Media, "disk_free", lambda self: 10 * 1024 ** 3)


def add_stream(client, name="Camera B"):
    response = client.post("/api/streams", json={"name": name})
    assert response.status_code == 201, response.text
    item = response.json()
    assert re.fullmatch(r"stream-[a-f0-9]{32}", item["id"])
    return item["id"]


def worker_configs(client):
    headers = {"Authorization": f"Bearer {client.app.state.config.internal_token}"}
    response = client.get("/internal/worker/configs", headers=headers)
    assert response.status_code == 200, response.text
    return {item["stream_id"]: item for item in response.json()["streams"]}


def set_path(client, stream_id, *, ready=True, source="publisher-1"):
    media = client.app.state.medias[stream_id]
    media.read_path = AsyncMock(return_value={
        "ready": ready, "source": {"id": source} if source else None,
        "readyTime": "2026-09-13T12:00:00Z", "tracks": ["H264", "MPEG4Audio"],
        "bytesReceived": 100,
    })
    client.portal.call(media.refresh)
    return media


def connection_list(client, items=()):
    client.app.state.media.client.get = AsyncMock(return_value=httpx.Response(
        200, json={"items": list(items), "pageCount": 1},
        request=httpx.Request("GET", "http://mediamtx:9997/v3/rtmpconns/list"),
    ))


def scoped_batch(client, stream_id, *, captured=None, vectors=None):
    data, headers = batch(client, captured=captured, vectors=vectors)
    cfg = worker_configs(client)[stream_id]
    data.update(stream_id=stream_id, session_id=cfg["session_id"],
                catalog_version=cfg["catalog_version"])
    return data, headers


def observe(client, stream_id, **kwargs):
    data, headers = scoped_batch(client, stream_id, **kwargs)
    response = client.post("/internal/observations", json=data, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["accepted"]


def enable(client, stream_id, enabled=True):
    response = client.post("/api/analysis", params={"stream_id": stream_id}, json={"enabled": enabled})
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": enabled}


def publish(client, stream_id, **overrides):
    body = {"action": "publish", "protocol": "rtmp", "user": "publisher",
            "password": client.app.state.store.get("stream_key", stream_id),
            "path": f"live/{stream_id}"}
    body.update(overrides)
    return client.post("/internal/media/auth", json=body)


def test_genuine_legacy_schema_migrates_twice_without_rewriting_data(tmp_path):
    config = Config(admin_password="a-long-test-password", internal_token="t" * 32, data_dir=tmp_path)
    session_id, open_session = str(uuid.uuid4()), str(uuid.uuid4())
    recording_id, running_id = str(uuid.uuid4()), str(uuid.uuid4())
    first, last = "2026-09-12T10:00:00+00:00", "2026-09-12T10:01:00+00:00"
    image = io.BytesIO()
    Image.new("RGB", (112, 112), "red").save(image, format="JPEG")
    thumbnail = image.getvalue()
    embedding = json.dumps([1.0] + [0.0] * 127, separators=(",", ":"))
    secret = "existing-publishing-secret-never-replace"
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()
    files = {}
    for key in (recording_id, running_id):
        path = recordings_dir / f"{key}.mp4"
        path.write_bytes((key.encode() + b"legacy-fragments") * 100)
        files[path] = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)

    # Deliberately do not seed with Store: this is the shipped pre-stream schema.
    db = sqlite3.connect(tmp_path / "steamlab.sqlite3")
    db.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT);
        CREATE TABLE faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT, embedding TEXT NOT NULL,
            thumbnail BLOB NOT NULL, quality REAL NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            sightings INTEGER NOT NULL, detection_confidence REAL NOT NULL, match_similarity REAL);
        CREATE TABLE face_sessions (
            face_id INTEGER REFERENCES faces(id) ON DELETE CASCADE,
            session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, sightings INTEGER NOT NULL,
            PRIMARY KEY(face_id, session_id));
        CREATE TABLE recordings (
            id TEXT PRIMARY KEY, session_id TEXT REFERENCES sessions(id),
            started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, error TEXT);
        CREATE INDEX face_last_seen ON faces(last_seen);
        CREATE INDEX face_session_id ON face_sessions(session_id);
    """)
    with db:
        db.executemany("INSERT INTO settings VALUES (?, ?)", [
            ("stream_key", secret), ("catalog_version", "42"), ("analysis_enabled", "1"),
            ("custom-setting", "unchanged-value"),
        ])
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?)",
                       [(session_id, first, last), (open_session, first, None)])
        db.execute("INSERT INTO faces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (73, embedding, thumbnail, 0.73, first, last, 8, 0.97, 0.91))
        db.execute("INSERT INTO face_sessions VALUES (?, ?, ?, ?, ?)", (73, session_id, first, last, 5))
        db.executemany("INSERT INTO recordings VALUES (?, ?, ?, ?, ?, ?)", [
            (recording_id, None, first, last, "ready", "preserved note"),
            (running_id, open_session, first, None, "recording", None),
        ])
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"settings", "sessions", "faces", "face_sessions", "recordings", "sqlite_sequence"}
    for table in tables:
        assert "stream_id" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    db.close()

    snapshot = None
    for _ in range(2):
        store = Store(config)
        try:
            assert len(store.streams()) == 1
            assert store.stream("stream")["name"] == "Stream 1"
            assert store.stream("stream")["archived_at"] is None
            assert store.get("stream_key") == secret
            assert store.get("catalog_version") == "42"
            assert store.get("analysis_enabled") == "0"
            assert store.get("custom-setting") == "unchanged-value"
            assert not store.db.execute("SELECT key FROM settings WHERE key LIKE 'stream:%'").fetchall()
            face = dict(store.db.execute("SELECT * FROM faces").fetchone())
            assert face == dict(id=73, embedding=embedding, thumbnail=thumbnail, quality=0.73,
                                first_seen=first, last_seen=last, sightings=8,
                                detection_confidence=0.97, match_similarity=0.91, stream_id="stream")
            assert tuple(store.db.execute("SELECT * FROM face_sessions").fetchone()) == (73, session_id, first, last, 5)
            sessions = {row["id"]: dict(row) for row in store.db.execute("SELECT * FROM sessions")}
            assert set(sessions) == {session_id, open_session}
            assert sessions[session_id] == dict(id=session_id, started_at=first, ended_at=last, stream_id="stream")
            assert sessions[open_session]["ended_at"] is not None
            assert all(row["stream_id"] == "stream" for row in sessions.values())
            records = {row["id"]: dict(row) for row in store.db.execute("SELECT * FROM recordings")}
            assert set(records) == {recording_id, running_id}
            assert records[recording_id] == dict(id=recording_id, session_id=None, started_at=first,
                                                ended_at=last, status="ready", error="preserved note", stream_id="stream")
            assert records[running_id]["session_id"] == open_session
            assert records[running_id]["stream_id"] == "stream"
            assert records[running_id]["started_at"] == first
            assert records[running_id]["status"] == "interrupted"
            assert records[running_id]["ended_at"] is not None
            assert records[running_id]["error"] == "Backend restarted before recording was finalized"
            assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
            current = {table: [tuple(row) for row in store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                       for table in tables | {"streams"}}
            if snapshot is not None:
                assert current == snapshot
            snapshot = current
            for path, original in files.items():
                assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == original
            assert set(recordings_dir.iterdir()) == set(files)
        finally:
            store.db.close()


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/streams", None),
    ("POST", "/api/streams", {"name": "Private"}),
    ("PATCH", "/api/streams/stream", {"name": "Private"}),
    ("DELETE", "/api/streams/stream", None),
    ("GET", "/api/streams/stream/live/index.m3u8", None),
    ("GET", "/internal/worker/configs", None),
])
def test_registry_requires_auth_and_mutations_require_csrf(client, method, path, body):
    assert client.request(method, path, json=body).status_code == 401
    login(client)
    if path.startswith("/internal/"):
        assert client.request(method, path, json=body).status_code == 401
    elif method != "GET":
        token = client.headers.pop("X-CSRF-Token")
        assert client.request(method, path, json=body).status_code == 403
        assert client.request(method, path, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        client.headers["X-CSRF-Token"] = token
        assert client.request(method, path, json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.app.state.store.streams()[0]["name"] == "Stream 1"
    assert len(client.app.state.store.streams()) == 1


def test_registry_default_counts_toward_four_and_names_are_trimmed(client):
    login(client)
    initial = client.get("/api/streams").json()
    assert initial["max_streams"] == 4 and initial["active_count"] == 1
    assert len(initial["items"]) == 1
    default = initial["items"][0]
    assert default == {"id": "stream", "name": "Stream 1", "created_at": default["created_at"],
                       "archived_at": None, "is_default": True, "online": False,
                       "media_available": False, "recording": None, "bitrate_mbps": 0,
                       "analysis_enabled": False}
    datetime.fromisoformat(default["created_at"])
    ids = ["stream"] + [add_stream(client, name) for name in ("  Camera B  ", "C", "D" * 64)]
    assert client.app.state.store.stream(ids[1])["name"] == "Camera B"
    assert client.post("/api/streams", json={"name": "Fifth"}).status_code == 409
    renamed = client.patch("/api/streams/stream", json={"name": "  Main stage  "})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Main stage"
    assert renamed.json()["created_at"] == default["created_at"]
    assert client.patch(f"/api/streams/{ids[1]}", json={"name": " B renamed "}).json()["name"] == "B renamed"
    listing = client.get("/api/streams")
    assert listing.json()["active_count"] == 4
    assert {item["id"] for item in listing.json()["items"]} == set(ids)
    keys = [client.app.state.store.get("stream_key", key) for key in ids]
    assert len(set(keys)) == 4
    for key in keys:
        assert key not in listing.text
    assert "stream_key" not in listing.text
    settings = dict(client.app.state.store.db.execute("SELECT key, value FROM settings"))
    assert settings["stream_key"] == keys[0]
    for stream_id, key in zip(ids[1:], keys[1:]):
        assert settings[f"stream:{stream_id}:stream_key"] == key
        assert settings[f"stream:{stream_id}:analysis_enabled"] == "0"
        assert settings[f"stream:{stream_id}:catalog_version"] == "0"


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "A\nB", "A\rB", "A\tB", "A\x00B", "A\x7fB", "A\x85B"])
def test_invalid_names_do_not_create_or_rename(client, name):
    login(client)
    assert client.post("/api/streams", json={"name": name}).status_code == 422
    assert client.patch("/api/streams/stream", json={"name": name}).status_code == 422
    assert len(client.app.state.store.streams()) == 1
    assert client.app.state.store.stream("stream")["name"] == "Stream 1"


def test_five_simultaneous_creations_cannot_overbook_registry(client):
    login(client)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://testserver", cookies=client.cookies,
                                     headers={"X-CSRF-Token": client.headers["X-CSRF-Token"]}) as request:
            gate = asyncio.Event()

            async def create(index):
                await gate.wait()
                return await request.post("/api/streams", json={"name": f"Concurrent {index}"})

            tasks = [asyncio.create_task(create(index)) for index in range(5)]
            gate.set()
            return await asyncio.gather(*tasks)

    responses = client.portal.call(scenario)
    assert sorted(response.status_code for response in responses) == [201, 201, 201, 409, 409]
    ids = {response.json()["id"] for response in responses if response.status_code == 201}
    assert len(ids) == 3
    assert client.get("/api/streams").json()["active_count"] == 4
    assert set(client.app.state.medias) == ids | {"stream"}
    assert set(worker_configs(client)) == ids | {"stream"}
    assert len(client.app.state.store.streams()) == 4


def test_status_settings_worker_heartbeat_and_session_scopes(client):
    login(client)
    other = add_stream(client)
    a = set_path(client, "stream", source="publisher-A")
    b = set_path(client, other, source="publisher-B")
    enable(client, other)
    configs = worker_configs(client)
    assert set(configs) == {"stream", other}
    assert not configs["stream"]["enabled"] and configs[other]["enabled"]
    assert configs["stream"]["session_id"] == a.session_id
    assert configs[other]["session_id"] == b.session_id != a.session_id
    headers = {"Authorization": f"Bearer {client.app.state.config.internal_token}"}
    assert client.get("/internal/worker/config", headers=headers).json() == configs["stream"]
    assert client.post("/internal/worker/heartbeat", headers=headers, json={
        "stream_id": other, "state": "analyzing", "provider": "B-provider", "error": "B-only",
    }).status_code == 204
    for stream_id, name, media in (("stream", "Stream 1", a), (other, "Camera B", b)):
        params = {"stream_id": stream_id}
        status = client.get("/api/status", params=params).json()
        settings = client.get("/api/settings", params=params).json()
        assert status["stream_id"] == settings["stream_id"] == stream_id
        assert status["stream_name"] == settings["stream_name"] == name
        assert status["archived"] is settings["archived"] is False
        assert status["online"] and status["media_available"]
        assert status["session_id"] == media.session_id
        assert status["analysis"]["enabled"] is settings["analysis_enabled"] is (stream_id == other)
        assert status["analysis"]["provider"] == ("B-provider" if stream_id == other else None)
        assert status["analysis"]["error"] == ("B-only" if stream_id == other else None)
        assert settings["stream_key"] == f"{stream_id}?user=publisher&pass={client.app.state.store.get('stream_key', stream_id)}"
        assert settings["max_faces"] == client.app.state.config.max_faces
        assert configs[stream_id]["rtsp_url"] == f"rtsp://mediamtx:8554/live/{stream_id}"
        assert "@" not in configs[stream_id]["rtsp_url"]
        sessions = client.get("/api/sessions", params=params).json()["items"]
        assert len(sessions) == 1 and sessions[0]["id"] == media.session_id
        assert sessions[0]["stream_id"] == stream_id and sessions[0]["ended_at"] is None
        assert client.get("/api/faces", params=params).json() == {"items": [], "total": 0}
        assert client.get("/api/recordings", params=params).json() == {"items": []}
    assert client.get("/api/settings").json() == client.get("/api/settings?stream_id=stream").json()
    assert client.get("/api/status").json()["stream_id"] == "stream"
    assert client.get("/api/sessions").json()["items"][0]["id"] == a.session_id
    assert client.post("/internal/worker/heartbeat", headers=headers, json={
        "state": "idle", "provider": "legacy-provider",
    }).status_code == 204
    assert b.heartbeat["provider"] == "B-provider"
    assert a.heartbeat["provider"] == "legacy-provider"


def test_identical_embeddings_group_within_stream_and_recur_across_sessions(client):
    login(client)
    other = add_stream(client)
    for stream_id in ("stream", other):
        set_path(client, stream_id)
        enable(client, stream_id)
    earlier = (datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat()
    for stream_id in ("stream", other):
        assert observe(client, stream_id, captured=earlier) == 1
    face_a = client.get("/api/faces").json()["items"][0]
    face_b = client.get("/api/faces", params={"stream_id": other}).json()["items"][0]
    assert face_a["id"] != face_b["id"]
    assert face_a["stream_id"] == "stream" and face_b["stream_id"] == other
    assert face_a["match_similarity"] is face_b["match_similarity"] is None
    data, headers = scoped_batch(client, "stream")
    assert client.post("/internal/observations", headers=headers, json=data).json() == {"accepted": 1}
    assert client.post("/internal/observations", headers=headers, json=data).json() == {"accepted": 0}
    repeated = client.get("/api/faces").json()["items"][0]
    assert repeated["id"] == face_a["id"] and repeated["sightings"] == 2
    assert repeated["match_similarity"] == pytest.approx(1.0)
    assert client.get("/api/faces", params={"stream_id": other}).json()["items"] == [face_b]
    old_session = client.app.state.media.session_id
    b_session = client.app.state.medias[other].session_id
    assert client.get("/api/faces", params={"session_id": b_session}).json()["total"] == 0
    assert client.get("/api/faces", params={"stream_id": other, "session_id": old_session}).json()["total"] == 0
    set_path(client, "stream", ready=False, source=None)
    set_path(client, "stream", source="reconnected-A")
    new_session = client.app.state.media.session_id
    assert new_session != old_session
    assert observe(client, "stream") == 1
    recurrence = client.get("/api/faces").json()
    assert recurrence["total"] == 1
    assert recurrence["items"][0]["id"] == face_a["id"]
    assert recurrence["items"][0]["sightings"] == 3
    for session_id, sightings in ((old_session, 2), (new_session, 1)):
        scoped = client.get("/api/faces", params={"session_id": session_id}).json()
        assert scoped["total"] == 1 and scoped["items"][0]["sightings"] == sightings
    sessions = client.get("/api/sessions").json()["items"]
    assert {item["id"] for item in sessions} == {old_session, new_session}
    assert next(item for item in sessions if item["id"] == old_session)["ended_at"] is not None
    assert client.get("/api/status").json()["face_count"] == 1
    assert client.get("/api/status", params={"stream_id": other}).json()["face_count"] == 1


def test_toggle_clear_and_single_delete_invalidate_only_owner(client):
    login(client)
    other = add_stream(client)
    for stream_id in ("stream", other):
        set_path(client, stream_id)
        enable(client, stream_id)
        assert observe(client, stream_id) == 1
    stale_a, headers = scoped_batch(client, "stream")
    valid_b, _ = scoped_batch(client, other)
    before = worker_configs(client)
    enable(client, "stream", False)
    after = worker_configs(client)
    assert after["stream"]["catalog_version"] == before["stream"]["catalog_version"] + 1
    assert after[other] == before[other]
    assert client.post("/internal/observations", json=stale_a, headers=headers).status_code == 409
    assert client.post("/internal/observations", json=valid_b, headers=headers).status_code == 200
    enable(client, "stream")
    stale_a, _ = scoped_batch(client, "stream")
    before = worker_configs(client)
    b_faces = client.get("/api/faces", params={"stream_id": other}).json()
    assert client.delete("/api/faces").status_code == 204
    assert client.get("/api/faces").json() == {"items": [], "total": 0}
    assert client.get("/api/faces", params={"stream_id": other}).json() == b_faces
    after = worker_configs(client)
    assert after["stream"]["catalog_version"] == before["stream"]["catalog_version"] + 1
    assert after[other] == before[other]
    assert client.post("/internal/observations", json=stale_a, headers=headers).status_code == 409
    face = b_faces["items"][0]
    assert parse_qs(urlsplit(face["thumbnail_url"]).query) == {"stream_id": [other]}
    assert client.get(face["thumbnail_url"]).headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/faces/{face['id']}/thumbnail").status_code == 200
    assert client.get(f"/api/faces/{face['id']}/thumbnail?stream_id=stream").status_code == 404
    assert client.delete(f"/api/faces/{face['id']}?stream_id=stream").status_code == 404
    assert worker_configs(client) == after
    assert client.delete(f"/api/faces/{face['id']}", params={"stream_id": other}).status_code == 204
    final = worker_configs(client)
    assert final["stream"] == after["stream"]
    assert final[other]["catalog_version"] == after[other]["catalog_version"] + 1
    assert client.get(face["thumbnail_url"]).status_code == 404
    assert client.app.state.store.db.execute("SELECT COUNT(*) FROM face_sessions").fetchone()[0] == 0


def test_global_face_capacity_counts_archived_groups_and_allows_existing_matches(client):
    login(client)
    other = add_stream(client)
    object.__setattr__(client.app.state.config, "max_faces", 2)
    earlier = (datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat()
    for stream_id in ("stream", other):
        set_path(client, stream_id)
        enable(client, stream_id)
        assert observe(client, stream_id, captured=earlier) == 1
    different = [[0.0, 1.0] + [0.0] * 126]
    assert observe(client, "stream", vectors=different) == 0
    assert observe(client, other, vectors=different) == 0
    assert observe(client, "stream") == 1
    set_path(client, other, ready=False, source=None)
    connection_list(client)
    assert client.delete(f"/api/streams/{other}").status_code == 204
    assert client.get("/api/faces", params={"stream_id": other}).json()["total"] == 1
    third = add_stream(client, "Replacement")
    set_path(client, third)
    enable(client, third)
    assert observe(client, third) == 0
    assert observe(client, "stream", vectors=different) == 0
    assert client.app.state.store.db.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2
    assert client.delete("/api/faces", params={"stream_id": other}).status_code == 204
    assert observe(client, third) == 1
    assert observe(client, "stream", vectors=different) == 0
    assert client.app.state.store.db.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 2


@pytest.mark.parametrize("count", [2, 4])
def test_recorders_use_real_scoped_reader_paths_and_stop_independently(client, monkeypatch, count):
    login(client)
    ids = ["stream"] + [add_stream(client, f"Camera {index}") for index in range(1, count)]
    paths, requests, processes = {}, [], {}

    async def handler(request):
        requests.append(request)
        stream_id = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path == f"/v3/paths/get/live/{stream_id}"
        assert stream_id in paths
        return httpx.Response(200, json=paths[stream_id])

    async def spawn(*args, **kwargs):
        assert args[0] == "ffmpeg" and args[args.index("-c") + 1] == "copy" and "-n" in args
        assert args[args.index("-rtsp_transport") + 1] == "tcp"
        url = urlsplit(args[args.index("-i") + 1])
        stream_id = url.path.rsplit("/", 1)[-1]
        assert url.scheme == "rtsp" and url.hostname == "mediamtx" and url.port == 8554
        assert url.path == f"/live/{stream_id}"
        assert url.username == "reader" and unquote(url.password) == client.app.state.config.internal_token
        assert all(client.app.state.store.get("stream_key", key) not in args[args.index("-i") + 1] for key in ids)
        target = Path(args[-1])
        assert target.parent == client.app.state.config.data_dir / "recordings"
        assert str(uuid.UUID(target.stem)) == target.stem
        target.write_bytes((stream_id.encode() + b"-fragments") * 256)
        assert stream_id not in processes
        processes[stream_id] = Recorder()
        return processes[stream_id]

    monkeypatch.setattr("backend.media.asyncio.create_subprocess_exec", spawn)
    for stream_id in ids:
        paths[stream_id] = {"ready": True, "source": {"id": f"publisher-{stream_id}"},
                            "readyTime": utcnow(), "tracks": ["H264", "MPEG4Audio"], "bytesReceived": 100}
        media = client.app.state.medias[stream_id]
        client.portal.call(media.client.aclose)
        media.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def start_all():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
                                     cookies=client.cookies, headers={"X-CSRF-Token": client.headers["X-CSRF-Token"]}) as request:
            return await asyncio.gather(*(request.post("/api/recordings/start", params={"stream_id": key}) for key in ids))

    responses = client.portal.call(start_all)
    assert [response.status_code for response in responses] == [200] * count
    recordings = {key: response.json()["id"] for key, response in zip(ids, responses)}
    assert len(set(recordings.values())) == count and len({id(process) for process in processes.values()}) == count
    assert {request.url.path for request in requests} == {f"/v3/paths/get/live/{key}" for key in ids}
    sessions = {key: client.app.state.medias[key].session_id for key in ids}
    assert len(set(sessions.values())) == count
    for key in ids:
        status = client.get("/api/status", params={"stream_id": key}).json()
        assert status["recording"]["id"] == recordings[key]
        listing = client.get("/api/recordings", params={"stream_id": key}).json()["items"]
        assert len(listing) == 1 and listing[0]["id"] == recordings[key]
        assert listing[0]["stream_id"] == key and listing[0]["session_id"] == sessions[key]
        assert listing[0]["status"] == "recording"
        assert client.post("/api/recordings/start", params={"stream_id": key}).status_code == 409
        assert client.delete(f"/api/recordings/{recordings[key]}").status_code == 409
        assert client.get(f"/api/recordings/{recordings[key]}/file").status_code == 409
    assert {item["id"]: item["recording"]["id"] for item in client.get("/api/streams").json()["items"]} == recordings
    assert client.post("/api/recordings/stop").status_code == 204
    assert processes["stream"].returncode == 255
    for key in ids[1:]:
        assert processes[key].returncode is None
        assert client.get("/api/status", params={"stream_id": key}).json()["recording"]["id"] == recordings[key]
    disconnected = ids[1]
    paths[disconnected] = {"ready": False}
    client.portal.call(client.app.state.medias[disconnected].refresh)
    assert processes[disconnected].returncode == 255
    assert client.get("/api/status", params={"stream_id": disconnected}).json()["session_id"] is None
    assert client.app.state.media.session_id == sessions["stream"]
    for key in ids[2:]:
        assert processes[key].returncode is None
        assert client.app.state.medias[key].session_id == sessions[key]
        assert client.post("/api/recordings/stop", params={"stream_id": key}).status_code == 204
    for key in ids:
        record = client.get("/api/recordings", params={"stream_id": key}).json()["items"][0]
        assert record["status"] == ("interrupted" if key == disconnected else "ready")
        assert record["ended_at"] is not None
        assert parse_qs(urlsplit(record["playback_url"]).query) == {"stream_id": [key]}
        wrong = ids[1] if key == "stream" else "stream"
        assert client.get(f"/api/recordings/{record['id']}/file", params={"stream_id": wrong}).status_code == 404
        assert client.delete(f"/api/recordings/{record['id']}", params={"stream_id": wrong}).status_code == 404
        content = (client.app.state.media.recordings_dir / f"{record['id']}.mp4").read_bytes()
        response = client.get(record["playback_url"], headers={"Range": "bytes=7-37"})
        assert response.status_code == 206 and response.content == content[7:38]
        assert response.headers["content-range"] == f"bytes 7-37/{len(content)}"
        assert client.get(f"/api/recordings/{record['id']}/file").content == content
        download = client.get(record["download_url"])
        assert download.headers["content-disposition"] == f'attachment; filename="kunas-labs-{record["id"]}.mp4"'
    assert client.delete(f"/api/recordings/{recordings['stream']}").status_code == 204
    assert client.get("/api/recordings").json()["items"] == []
    assert all(len(client.get("/api/recordings", params={"stream_id": key}).json()["items"]) == 1 for key in ids[1:])


@pytest.mark.parametrize("a_is_default", [True, False])
def test_rotation_kicks_only_a_including_pretrack_admissions_while_b_stays_live(client, a_is_default):
    login(client)
    other = add_stream(client)
    a_id, b_id = ("stream", other) if a_is_default else (other, "stream")
    a = set_path(client, a_id, ready=False, source=None)
    b = set_path(client, b_id, source="B-live")
    b_state = (b.source, b.session_id, b.online)
    keys = {key: client.app.state.store.get("stream_key", key) for key in (a_id, b_id)}
    for key, connection in ((a_id, "pending-A"), (b_id, "pending-B")):
        assert publish(client, key, id=connection).status_code == 204
    connection_list(client, [{"id": "path-A", "path": f"live/{a_id}"}, {"id": "pending-A"},
                             {"id": "live-B", "path": f"live/{b_id}"}, {"id": "pending-B"}, {"id": "unrelated"}])
    a.client.post = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("POST", "http://mediamtx/kick")))
    if a is not b:
        b.client.post = AsyncMock(side_effect=AssertionError("Rotation must not kick B"))
    response = client.post("/api/stream/key", params={"stream_id": a_id})
    assert response.status_code == 200, response.text
    assert client.app.state.store.get("stream_key", a_id) != keys[a_id]
    assert client.app.state.store.get("stream_key", b_id) == keys[b_id]
    assert {call.args[0] for call in a.client.post.call_args_list} == {
        "http://mediamtx:9997/v3/rtmpconns/kick/path-A", "http://mediamtx:9997/v3/rtmpconns/kick/pending-A"}
    assert a.client.post.await_count == 2 and b.client.post.await_count == 0
    assert (b.source, b.session_id, b.online) == b_state
    assert client.app.state.admissions["pending-B"] == b_id
    assert publish(client, a_id, password=keys[a_id]).status_code == 401
    assert publish(client, a_id).status_code == 204
    assert publish(client, b_id, password=keys[b_id]).status_code == 204
    assert client.post("/api/stream/key", params={"stream_id": b_id}).status_code == 409
    assert client.app.state.store.get("stream_key", b_id) == keys[b_id]


@pytest.mark.parametrize("overrides", [
    {"password": "wrong"}, {"protocol": "rtsp"}, {"protocol": "hls"}, {"protocol": "webrtc"},
    {"user": "reader"}, {"action": "api"}, {"path": "live/stream-not-registered"},
    {"path": "live/stream-" + "a" * 32}, {"path": "elsewhere/stream"},
    {"path": "live/stream/"}, {"path": "/live/stream"}, {"path": "live/../stream"},
])
def test_scoped_publisher_auth_rejects_wrong_credentials_protocol_and_path(client, overrides):
    login(client)
    other = add_stream(client)
    assert publish(client, other, id="valid").status_code == 204
    assert publish(client, other, id="rejected", **overrides).status_code == 401
    assert "rejected" not in client.app.state.admissions
    assert publish(client, other, path="live/stream").status_code == 401
    assert publish(client, "stream", path=f"live/{other}").status_code == 401


@pytest.mark.parametrize("protocol", ["rtmp", "rtsp", "hls"])
def test_reader_auth_is_internal_and_requires_registered_path(client, protocol):
    login(client)
    other = add_stream(client)
    for stream_id in ("stream", other):
        body = dict(action="read", protocol=protocol, user="reader", path=f"live/{stream_id}",
                    password=client.app.state.config.internal_token)
        assert client.post("/internal/media/auth", json=body).status_code == 204
        assert client.post("/internal/media/auth", json={**body, "password": client.app.state.store.get("stream_key", stream_id)}).status_code == 401
        assert client.post("/internal/media/auth", json={**body, "path": "live/stream-" + "0" * 32}).status_code == 401


@pytest.mark.parametrize("invalid", ["stream", "session", "version", "old-session", "old-version", "unknown", "old-time"])
def test_observations_reject_cross_stream_and_stale_generations(client, invalid):
    login(client)
    other = add_stream(client)
    for key in ("stream", other):
        set_path(client, key)
        enable(client, key)
    enable(client, other)  # Distinct generations make accidental default lookup visible.
    data, headers = scoped_batch(client, other)
    if invalid == "stream":
        data["stream_id"] = "stream"
    elif invalid == "session":
        data["session_id"] = client.app.state.media.session_id
    elif invalid == "version":
        data["catalog_version"] = worker_configs(client)["stream"]["catalog_version"]
    elif invalid == "old-session":
        set_path(client, other, source="replacement")
    elif invalid == "old-version":
        enable(client, other, False)
        enable(client, other)
    elif invalid == "unknown":
        data["stream_id"] = "stream-" + "f" * 32
    else:
        data["captured_at"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    assert client.post("/internal/observations", json=data, headers=headers).status_code == 409
    assert client.app.state.store.db.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    assert observe(client, other) == 1
    assert client.get("/api/faces").json()["total"] == 0


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/status", None), ("GET", "/api/settings", None),
    ("GET", "/api/sessions", None), ("GET", "/api/faces", None),
    ("GET", "/api/recordings", None), ("DELETE", "/api/faces", None),
    ("POST", "/api/stream/key", None), ("POST", "/api/analysis", {"enabled": True}),
    ("POST", "/api/recordings/start", None), ("POST", "/api/recordings/stop", None),
])
def test_unknown_scope_never_falls_back_to_default(client, method, path, body):
    login(client)
    assert client.request(method, path, params={"stream_id": "stream-" + "f" * 32}, json=body).status_code == 404
    assert client.request(method, path, params={"stream_id": ""}, json=body).status_code == 422
    assert client.request(method, path, params={"stream_id": "x" * 65}, json=body).status_code == 422
    assert client.app.state.store.get("analysis_enabled") == "0"
    assert client.app.state.store.get("catalog_version") == "0"


def test_scoped_hls_relative_playlists_segments_ranges_and_secret_filtering(client):
    login(client)
    other = add_stream(client)
    seen = []
    token = client.app.state.config.internal_token
    keys = [client.app.state.store.get("stream_key", key) for key in ("stream", other)]

    class Chunks(httpx.AsyncByteStream):
        def __init__(self, content):
            self.content = content

        async def __aiter__(self):
            yield self.content

    async def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Basic " + base64.b64encode(f"reader:{token}".encode()).decode()
        assert all(secret not in str(request.url) for secret in [token, *keys])
        assert COOKIE + "=" not in request.headers.get("cookie", "")
        assert client.cookies.get(COOKIE) not in request.headers.get("cookie", "")
        assert "x-csrf-token" not in request.headers
        file = request.url.path.rsplit("/", 1)[-1]
        content = {"index.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nvideo.m3u8\n",
                   "video.m3u8": b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\nsegment.m4s\n',
                   "init.mp4": b"initialization", "segment.m4s": b"0123456789"}[file]
        headers = {"content-type": "application/vnd.apple.mpegurl" if file.endswith("m3u8") else "video/mp4",
                   "set-cookie": "upstream-secret=not-for-browser", "authorization": "never-forward"}
        code = 200
        if "range" in request.headers:
            assert request.headers["range"] == "bytes=2-5"
            assert request.headers["if-range"] == '"segment-generation"'
            code, content = 206, content[2:6]
            headers.update({"content-range": "bytes 2-5/10", "accept-ranges": "bytes", "content-length": "4"})
        return httpx.Response(code, headers=headers, stream=Chunks(content))

    for media in client.app.state.medias.values():
        client.portal.call(media.client.aclose)
        media.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    for stream_id in (other, "stream"):
        base = f"/api/streams/{stream_id}/live/index.m3u8"
        response = client.get(base, params={"_HLS_msn": "5", "_HLS_part": "2", "_HLS_skip": "YES",
                                           "pass": keys[0], "user": "publisher", "stream_id": "stream"})
        assert response.status_code == 200
        assert seen[-1].url.path == f"/live/{stream_id}/index.m3u8"
        assert dict(seen[-1].url.params) == {"_HLS_msn": "5", "_HLS_part": "2", "_HLS_skip": "YES"}
        relative = next(line for line in response.text.splitlines() if not line.startswith("#"))
        playlist = client.get(urljoin(base, relative))
        assert playlist.status_code == 200 and seen[-1].url.path == f"/live/{stream_id}/video.m3u8"
        assert client.get(urljoin(base, "init.mp4")).content == b"initialization"
        segment = next(line for line in playlist.text.splitlines() if not line.startswith("#"))
        ranged = client.get(urljoin(base, segment), headers={"Range": "bytes=2-5", "If-Range": '"segment-generation"'})
        assert ranged.status_code == 206 and ranged.content == b"2345"
        assert ranged.headers["content-range"] == "bytes 2-5/10"
        assert ranged.headers["accept-ranges"] == "bytes"
        assert seen[-1].url.path == f"/live/{stream_id}/segment.m4s"
        for result in (response, playlist, ranged):
            assert result.headers["cache-control"] == "no-store"
            assert "set-cookie" not in result.headers and "authorization" not in result.headers
            assert all(secret not in result.text + str(result.headers) for secret in [token, *keys])
    assert client.get("/api/live/index.m3u8").status_code == 200
    assert seen[-1].url.path == "/live/stream/index.m3u8"
    count = len(seen)
    for file in ("evil.html", "http:%2F%2Fevil", "%2e%2e%2Findex.m3u8", "nested/index.m3u8"):
        assert client.get(f"/api/streams/{other}/live/{file}").status_code == 404
    assert client.get("/api/streams/stream-" + "f" * 32 + "/live/index.m3u8").status_code == 404
    assert len(seen) == count


@pytest.mark.parametrize("guard", ["default", "online", "source", "path-pending", "admitted-pending", "recording", "path-unavailable", "connections-unavailable"])
def test_archive_refuses_unsafe_states_without_changing_registry(client, guard):
    login(client)
    other = add_stream(client)
    media = set_path(client, other, ready=False, source=None)
    connection_list(client)
    target = other
    if guard == "default":
        target = "stream"
    elif guard == "online":
        media = set_path(client, other)
    elif guard == "source":
        media.read_path = AsyncMock(return_value={"ready": False, "source": {"id": "pre-track"}})
    elif guard == "path-pending":
        connection_list(client, [{"id": "pending", "path": f"live/{other}"}])
    elif guard == "admitted-pending":
        assert publish(client, other, id="pending").status_code == 204
        connection_list(client, [{"id": "pending"}])
    elif guard == "recording":
        media.recording = {"id": str(uuid.uuid4()), "started_at": utcnow()}
    elif guard == "path-unavailable":
        media.read_path = AsyncMock(side_effect=httpx.ConnectError("unavailable"))
    else:
        client.app.state.media.client.get = AsyncMock(side_effect=httpx.ConnectError("unavailable"))
    before = client.app.state.store.streams()
    settings = list(client.app.state.store.db.execute("SELECT * FROM settings ORDER BY key"))
    response = client.delete(f"/api/streams/{target}")
    assert response.status_code == 409, response.text
    assert client.app.state.store.streams() == before
    assert list(client.app.state.store.db.execute("SELECT * FROM settings ORDER BY key")) == settings
    assert other in client.app.state.medias and not media.archived
    if guard == "recording":
        assert media.recording is not None
        media.recording = None


def test_archive_preserves_manageable_history_frees_slot_and_denies_ingest(client):
    login(client)
    ids = ["stream"] + [add_stream(client, f"Camera {index}") for index in range(1, 4)]
    archived, live = ids[1:3]
    for key in (archived, live):
        set_path(client, key)
        enable(client, key)
        assert observe(client, key) == 1
    stale, headers = scoped_batch(client, archived)
    face = client.get("/api/faces", params={"stream_id": archived}).json()["items"][0]
    session_id = client.app.state.medias[archived].session_id
    key = client.app.state.store.get("stream_key", archived)
    record_id = str(uuid.uuid4())
    target = client.app.state.media.recordings_dir / f"{record_id}.mp4"
    content = b"archived-recording" * 256
    target.write_bytes(content)
    with client.app.state.store.db:
        client.app.state.store.db.execute(
            "INSERT INTO recordings (id, session_id, started_at, ended_at, status, stream_id) VALUES (?, ?, ?, ?, 'ready', ?)",
            (record_id, session_id, utcnow(), utcnow(), archived))
    set_path(client, archived, ready=False, source=None)
    assert publish(client, live, id="pending-live").status_code == 204
    connection_list(client, [{"id": "pending-live"}, {"id": "live", "path": f"live/{live}"}])
    before = worker_configs(client)
    assert client.post("/api/streams", json={"name": "No room"}).status_code == 409
    assert client.delete(f"/api/streams/{archived}").status_code == 204
    listing = client.get("/api/streams").json()
    assert listing["active_count"] == 3 and len(listing["items"]) == 4
    item = next(item for item in listing["items"] if item["id"] == archived)
    assert item["archived_at"] and not item["online"] and not item["analysis_enabled"]
    assert item["recording"] is None
    assert key not in json.dumps(listing)
    assert client.app.state.store.get("stream_key", archived) == key
    assert int(client.app.state.store.get("catalog_version", archived)) == before[archived]["catalog_version"] + 1
    assert archived not in client.app.state.medias
    after = worker_configs(client)
    assert after == {key: value for key, value in before.items() if key != archived}
    status = client.get("/api/status", params={"stream_id": archived}).json()
    assert status["archived"] and not status["online"] and status["face_count"] == 1
    assert status["analysis"]["state"] == "archived" and not status["analysis"]["enabled"]
    settings = client.get("/api/settings", params={"stream_id": archived}).json()
    assert settings["archived"] and settings["stream_key"] == "" and not settings["analysis_enabled"]
    assert client.patch(f"/api/streams/{archived}", json={"name": " Archived camera "}).json()["name"] == "Archived camera"
    for method, path, body in (("POST", "/api/analysis", {"enabled": True}),
                               ("POST", "/api/analysis", {"enabled": False}),
                               ("POST", "/api/stream/key", None),
                               ("POST", "/api/recordings/start", None),
                               ("POST", "/api/recordings/stop", None)):
        assert client.request(method, path, params={"stream_id": archived}, json=body).status_code == 409
    assert client.delete(f"/api/streams/{archived}").status_code == 409
    assert client.get(f"/api/streams/{archived}/live/index.m3u8").status_code == 409
    assert publish(client, archived, password=key).status_code == 401
    for protocol in ("rtsp", "hls", "rtmp"):
        assert client.post("/internal/media/auth", json=dict(action="read", protocol=protocol,
            user="reader", password=client.app.state.config.internal_token, path=f"live/{archived}")).status_code == 401
    assert client.post("/internal/observations", headers=headers, json=stale).status_code == 409
    live_heartbeat = dict(client.app.state.medias[live].heartbeat)
    for key in (archived, "stream-" + "f" * 32):
        assert client.post("/internal/worker/heartbeat", headers=headers, json={
            "stream_id": key, "state": "analyzing", "provider": "must-not-land",
        }).status_code == 409
    assert client.app.state.medias[live].heartbeat == live_heartbeat
    replacement = add_stream(client, "New camera")
    assert replacement not in ids
    assert client.get("/api/streams").json()["active_count"] == 4
    assert len(client.get("/api/streams").json()["items"]) == 5
    assert client.post("/api/streams", json={"name": "Over cap again"}).status_code == 409
    history = client.get("/api/sessions", params={"stream_id": archived}).json()["items"]
    assert len(history) == 1 and history[0]["id"] == session_id and history[0]["ended_at"]
    assert client.get("/api/faces", params={"stream_id": archived, "session_id": session_id}).json()["items"] == [face]
    assert client.get(face["thumbnail_url"]).status_code == 200
    record = client.get("/api/recordings", params={"stream_id": archived}).json()["items"][0]
    assert record["id"] == record_id and record["stream_id"] == archived
    assert client.get(record["playback_url"]).content == content
    assert client.get(record["download_url"]).content == content
    assert client.delete(f"/api/recordings/{record_id}", params={"stream_id": archived}).status_code == 204
    assert not target.exists() and client.get(record["playback_url"]).status_code == 404
    assert client.delete(f"/api/faces/{face['id']}", params={"stream_id": archived}).status_code == 204
    assert client.get(face["thumbnail_url"]).status_code == 404
    assert client.get("/api/faces", params={"stream_id": archived}).json()["total"] == 0
    assert client.get("/api/recordings", params={"stream_id": archived}).json()["items"] == []
    assert client.get("/api/faces", params={"stream_id": live}).json()["total"] == 1
    assert client.get("/api/sessions", params={"stream_id": archived}).json()["items"] == history


def test_restart_rebuilds_active_streams_only_and_disables_every_analysis(tmp_path):
    config = Config(admin_password="a-long-test-password", internal_token="t" * 32, data_dir=tmp_path)
    with TestClient(create_app(config, monitoring=False)) as first:
        login(first)
        ids = ["stream"] + [add_stream(first, f"Camera {index}") for index in range(1, 4)]
        archived = ids[-1]
        for key in ids:
            set_path(first, key)
            enable(first, key)
            assert observe(first, key) == 1
        set_path(first, archived, ready=False, source=None)
        connection_list(first)
        assert first.delete(f"/api/streams/{archived}").status_code == 204
        replacement = add_stream(first, "Replacement")
        set_path(first, replacement)
        enable(first, replacement)
        active = ids[:-1] + [replacement]
        rows = first.app.state.store.streams()
        keys = {row["id"]: first.app.state.store.get("stream_key", row["id"]) for row in rows}
        versions = {row["id"]: first.app.state.store.get("catalog_version", row["id"]) for row in rows}
        sessions = {key: first.app.state.medias[key].session_id for key in active}
        files = {}
        # Crash-state rows bypass graceful Media.close finalization on purpose.
        with first.app.state.store.db:
            for key in active:
                record_id = str(uuid.uuid4())
                path = first.app.state.media.recordings_dir / f"{record_id}.mp4"
                path.write_bytes(key.encode() * 2048)
                files[key] = (record_id, path, path.read_bytes(), path.stat().st_mtime_ns)
                first.app.state.store.db.execute(
                    "INSERT INTO recordings (id, session_id, started_at, status, stream_id) VALUES (?, ?, ?, 'recording', ?)",
                    (record_id, sessions[key], utcnow(), key))

    for _ in range(2):
        with TestClient(create_app(config, monitoring=False)) as restarted:
            login(restarted)
            assert restarted.app.state.store.streams() == rows
            assert set(restarted.app.state.medias) == set(active)
            configs = worker_configs(restarted)
            assert set(configs) == set(active) and archived not in configs
            for row in rows:
                key = row["id"]
                assert restarted.app.state.store.get("stream_key", key) == keys[key]
                assert restarted.app.state.store.get("catalog_version", key) == versions[key]
                assert restarted.app.state.store.get("analysis_enabled", key) == "0"
            for key in active:
                assert not configs[key]["enabled"] and configs[key]["session_id"] is None
                media = restarted.app.state.medias[key]
                assert not media.online and media.process is None and media.recording is None
                history = restarted.get("/api/sessions", params={"stream_id": key}).json()["items"]
                assert len(history) == 1 and history[0]["id"] == sessions[key] and history[0]["ended_at"]
                record_id, path, content, mtime = files[key]
                record = restarted.get("/api/recordings", params={"stream_id": key}).json()["items"][0]
                assert record["id"] == record_id and record["session_id"] == sessions[key]
                assert record["status"] == "interrupted" and record["ended_at"]
                assert record["error"] == "Backend restarted before recording was finalized"
                assert path.read_bytes() == content and path.stat().st_mtime_ns == mtime
                assert restarted.get(record["playback_url"]).content == content
            assert restarted.get("/api/faces", params={"stream_id": archived}).json()["total"] == 1
            assert restarted.get("/api/settings", params={"stream_id": archived}).json()["stream_key"] == ""
            assert publish(restarted, archived).status_code == 401
            headers = {"Authorization": f"Bearer {config.internal_token}"}
            assert restarted.post("/internal/worker/heartbeat", headers=headers, json={
                "stream_id": archived, "state": "analyzing", "provider": "stale-worker",
            }).status_code == 409
            assert restarted.get("/api/streams").json()["active_count"] == 4


@pytest.mark.parametrize("operation", ["rotate", "archive"])
def test_management_does_not_deadlock_mediamtx_publisher_auth(client, operation):
    login(client)
    target, other = add_stream(client, "Changing"), add_stream(client, "Still live")
    target_media = set_path(client, target, ready=False, source=None)
    other_media = set_path(client, other)
    other_session = other_media.session_id
    connection_list(client, [{"id": "other-connection", "path": other_media.path}])

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://testserver", cookies=client.cookies,
                                     headers={"X-CSRF-Token": client.headers["X-CSRF-Token"]}) as request:
            async def gated_path_api():
                # MediaMTX can block its path API until this HTTP auth callback
                # finishes. An auth callback awaiting the management lock deadlocks.
                for key, expected in ((target, 401), (other, 204)):
                    response = await request.post("/internal/media/auth", json={
                        "id": "other-connection" if key == other else "blocked-target",
                        "action": "publish", "protocol": "rtmp", "user": "publisher",
                        "path": f"live/{key}", "password": client.app.state.store.get("stream_key", key),
                    })
                    assert response.status_code == expected
                return {"ready": False, "source": None}

            target_media.read_path = gated_path_api
            if operation == "rotate":
                response = await asyncio.wait_for(request.post("/api/stream/key", params={"stream_id": target}), 1)
                assert response.status_code == 200
            else:
                response = await asyncio.wait_for(request.delete(f"/api/streams/{target}"), 1)
                assert response.status_code == 204
            assert other_media.online and other_media.session_id == other_session
            assert client.app.state.admission_blocks == set()

    client.portal.call(scenario)


def test_connection_snapshot_does_not_forget_new_sibling_admission(client):
    login(client)
    other = add_stream(client)
    client.app.state.media.read_path = AsyncMock(return_value={"ready": False})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://testserver", cookies=client.cookies,
                                     headers={"X-CSRF-Token": client.headers["X-CSRF-Token"]}) as request:
            async def listing(*args, **kwargs):
                response = await request.post("/internal/media/auth", json={
                    "id": "just-admitted", "action": "publish", "protocol": "rtmp", "user": "publisher",
                    "path": f"live/{other}", "password": client.app.state.store.get("stream_key", other),
                })
                assert response.status_code == 204
                return httpx.Response(200, json={"items": [], "pageCount": 0},
                                      request=httpx.Request("GET", "http://media/v3/rtmpconns/list"))

            client.app.state.media.client.get = listing
            assert (await request.post("/api/stream/key")).status_code == 200
            assert client.app.state.admissions["just-admitted"] == other

    client.portal.call(scenario)
