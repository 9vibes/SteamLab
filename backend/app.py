import asyncio
import contextlib
import hashlib
import re
import secrets
import time
import unicodedata
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, StrictBool, field_validator
from starlette.background import BackgroundTask

from .config import Config
from .media import Media
from .store import Store, utcnow

COOKIE = "steamlab_session"
SESSION_TTL = 12 * 60 * 60
StreamId = Annotated[str, Query(min_length=1, max_length=64)]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def equal(left, right):
    return secrets.compare_digest(digest(left), digest(right))


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > 3 * 1024 * 1024:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
            if not event.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Login(Input):
    password: str = Field(min_length=1, max_length=1024)


class Analysis(Input):
    enabled: StrictBool


class StreamName(Input):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("Use a nonempty stream name without control characters")
        return value


class Observation(Input):
    embedding: list[FiniteFloat] = Field(min_length=128, max_length=128)
    thumbnail: str = Field(max_length=133336)
    confidence: FiniteFloat = Field(ge=0, le=1)
    quality: FiniteFloat = Field(ge=0, le=1)


class Batch(Input):
    stream_id: str = Field(default="stream", max_length=64)
    session_id: str = Field(max_length=64)
    catalog_version: int = Field(ge=0)
    captured_at: AwareDatetime
    faces: list[Observation] = Field(max_length=20)


class Heartbeat(Input):
    stream_id: str = Field(default="stream", max_length=64)
    state: str = Field(max_length=32)
    provider: str = Field(max_length=64)
    error: str | None = Field(default=None, max_length=256)


class MediaAuth(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    user: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=1024)
    action: str = Field(max_length=32)
    path: str = Field(default="", max_length=128)
    protocol: str = Field(default="", max_length=32)


def create_app(config=None, *, monitoring=True):
    @asynccontextmanager
    async def lifespan(app):
        cfg = config or Config.from_env()
        app.state.config = cfg
        store = app.state.store = Store(cfg)
        app.state.medias = {row["id"]: Media(cfg, store, row["id"]) for row in store.streams() if not row["archived_at"]}
        # The original stream also owns shared disk/file access for legacy routes.
        app.state.media = app.state.medias["stream"]
        app.state.ingest_lock = asyncio.Lock()
        app.state.admission_blocks = set()
        app.state.admissions = {}
        app.state.sessions = {}
        app.state.login_attempts = deque()
        app.state.monitors = {key: asyncio.create_task(media.monitor()) for key, media in app.state.medias.items()} if monitoring else {}
        maintenance = asyncio.create_task(maintain()) if monitoring else None
        try:
            yield
        finally:
            tasks = [*app.state.monitors.values(), *([maintenance] if maintenance else [])]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(media.close() for media in app.state.medias.values()))
            store.db.close()

    app = FastAPI(title="KUNAS/Labs", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def private_responses(request, call_next):
        # SameSite cookies plus a per-session CSRF token protect changes. Reject
        # explicitly cross-site browser submissions even on the login endpoint.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site request denied"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def browser(request: Request):
        key = digest(request.cookies.get(COOKIE, ""))
        session = app.state.sessions.get(key)
        if not session or session["expires"] <= time.monotonic():
            app.state.sessions.pop(key, None)
            raise HTTPException(401, "Not authenticated")
        if request.method not in ("GET", "HEAD") and not equal(request.headers.get("x-csrf-token", ""), session["csrf_token"]):
            raise HTTPException(403, "CSRF token missing or invalid")
        request.state.session = session

    async def internal(request: Request):
        if not equal(request.headers.get("authorization", ""), f"Bearer {app.state.config.internal_token}"):
            raise HTTPException(401, "Not authenticated")

    def get_stream(stream_id):
        row = app.state.store.stream(stream_id)
        if row is None:
            raise HTTPException(404, "Stream not found")
        return row

    def get_media(stream_id):
        if get_stream(stream_id)["archived_at"]:
            raise HTTPException(409, "Stream is archived")
        return app.state.medias[stream_id]

    def stream_summary(row):
        media = app.state.medias.get(row["id"]) if not row["archived_at"] else None
        return {**row, "is_default": row["id"] == "stream", "online": media.online if media else False,
                "media_available": media.available if media else False,
                "recording": media.recording if media else None, "bitrate_mbps": media.bitrate if media else 0,
                "analysis_enabled": app.state.store.get("analysis_enabled", row["id"]) == "1"}

    async def connections():
        result, page = [], 0
        known = set(app.state.admissions)
        while True:
            response = await app.state.media.client.get(app.state.config.media_api + "/v3/rtmpconns/list",
                                                       params={"itemsPerPage": 1000, "page": page})
            response.raise_for_status()
            data = response.json()
            result.extend(data["items"])
            page += 1
            if page >= data["pageCount"]:
                break
        live_ids = {item["id"] for item in result}
        # Auth callbacks may have admitted a new connection while the API request
        # was in flight. Only remove entries known before taking the snapshot.
        for key in known - live_ids:
            app.state.admissions.pop(key, None)
        return result

    @asynccontextmanager
    async def changing_ingest(stream_id):
        async with app.state.ingest_lock:
            media = get_media(stream_id)
            app.state.admission_blocks.add(stream_id)
            try:
                async with media.lock:
                    yield media
            finally:
                app.state.admission_blocks.discard(stream_id)

    async def maintain():
        while True:
            try:
                app.state.store.prune()
                async with app.state.ingest_lock:
                    await connections()
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            except Exception:
                app.state.media.warning = "Storage maintenance encountered an error; retrying."
            await asyncio.sleep(60)

    def settings(stream_id="stream"):
        cfg, store = app.state.config, app.state.store
        row = get_stream(stream_id)
        return {"stream_id": stream_id, "stream_name": row["name"], "archived": bool(row["archived_at"]),
                "rtmp_url": f"rtmp://{cfg.public_host}:{cfg.rtmp_port}/live",
                "stream_key": f"{stream_id}?user=publisher&pass={store.get('stream_key', stream_id)}" if not row["archived_at"] else "",
                "analysis_enabled": store.get("analysis_enabled", stream_id) == "1",
                "match_threshold": cfg.match_threshold, "detection_threshold": cfg.detection_threshold,
                "face_retention_days": cfg.face_retention_days, "max_faces": cfg.max_faces,
                "analysis_fps": cfg.analysis_fps}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        attempts, now = app.state.login_attempts, time.monotonic()
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        # Global limit is intentional: the private nginx hop must not be trusted
        # to supply arbitrary client IPs for an attacker-controlled rate-limit key.
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many login attempts; wait one minute", headers={"Retry-After": "60"})
        attempts.append(now)
        if not equal(body.password, app.state.config.admin_password):
            raise HTTPException(401, "Invalid password")
        app.state.sessions = {key: value for key, value in app.state.sessions.items() if value["expires"] > now}
        app.state.sessions.pop(digest(request.cookies.get(COOKIE, "")), None)
        if len(app.state.sessions) >= 32:
            oldest = min(app.state.sessions, key=lambda key: app.state.sessions[key]["expires"])
            app.state.sessions.pop(oldest)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        app.state.sessions[digest(token)] = {"expires": now + SESSION_TTL, "csrf_token": csrf}
        response.set_cookie(COOKIE, token, httponly=True, secure=app.state.config.cookie_secure,
                            samesite="strict", max_age=SESSION_TTL, path="/")
        return {"csrf_token": csrf}

    api = APIRouter(prefix="/api", dependencies=[Depends(browser)])

    @api.get("/auth/me")
    async def me(request: Request):
        return {"csrf_token": request.state.session["csrf_token"]}

    @api.post("/auth/logout", status_code=204)
    async def logout(request: Request, response: Response):
        app.state.sessions.pop(digest(request.cookies.get(COOKIE, "")), None)
        response.delete_cookie(COOKIE, path="/", httponly=True, secure=app.state.config.cookie_secure, samesite="strict")

    @api.get("/settings")
    async def get_settings(stream_id: StreamId = "stream"):
        return settings(stream_id)

    @api.get("/streams")
    async def list_streams():
        rows = app.state.store.streams()
        return {"items": [stream_summary(row) for row in rows], "max_streams": 4,
                "active_count": sum(row["archived_at"] is None for row in rows)}

    @api.post("/streams", status_code=201)
    async def add_stream(body: StreamName):
        async with app.state.ingest_lock:
            try:
                row = app.state.store.add_stream(body.name)
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            media = app.state.medias[row["id"]] = Media(app.state.config, app.state.store, row["id"])
            if monitoring:
                app.state.monitors[row["id"]] = asyncio.create_task(media.monitor())
            return stream_summary(row)

    @api.patch("/streams/{stream_id}")
    async def rename_stream(stream_id: str, body: StreamName):
        get_stream(stream_id)
        with app.state.store.db:
            app.state.store.db.execute("UPDATE streams SET name=? WHERE id=?", (body.name, stream_id))
        return stream_summary(get_stream(stream_id))

    @api.delete("/streams/{stream_id}", status_code=204)
    async def archive_stream(stream_id: str):
        if stream_id == "stream":
            raise HTTPException(409, "The original stream can be renamed but not archived")
        store = app.state.store
        async with changing_ingest(stream_id) as media:
            if media.recording:
                raise HTTPException(409, "Stop recording before archiving this stream")
            try:
                path = await media.read_path()
                pending = await connections()
            except (httpx.HTTPError, ValueError, KeyError):
                raise HTTPException(409, "Media server must be reachable before archiving") from None
            if path.get("ready") or path.get("source") or any(
                item.get("path") == media.path or app.state.admissions.get(item["id"]) == stream_id for item in pending
            ):
                raise HTTPException(409, "Disconnect the publisher before archiving this stream")
            with store.db:
                store.db.execute("UPDATE streams SET archived_at=? WHERE id=?", (utcnow(), stream_id))
                store.set("analysis_enabled", 0, stream_id)
                store.invalidate_catalog(stream_id)
                store.db.execute("UPDATE sessions SET ended_at=? WHERE stream_id=? AND ended_at IS NULL", (utcnow(), stream_id))
            media.archived, media.online, media.session_id = True, False, None
            task = app.state.monitors.pop(stream_id, None)
            if task:
                task.cancel()
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        app.state.medias.pop(stream_id)
        await media.close()

    @api.post("/stream/key")
    async def rotate_key(stream_id: StreamId = "stream"):
        store = app.state.store
        async with changing_ingest(stream_id) as media:
            return await rotate_locked(media, store, stream_id)

    async def rotate_locked(media, store, stream_id):
        try:
            path = await media.read_path()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(409, "Media server must be reachable to rotate the key") from None
        if path.get("ready") or path.get("source"):
            raise HTTPException(409, "Disconnect the stream before rotating its key")
        # The admission map scopes pre-track handshakes as well as established
        # connections. Rotating one key must never kick another stream.
        try:
            targets = [item["id"] for item in await connections() if item.get("path") == media.path
                       or app.state.admissions.get(item["id"]) == stream_id]
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(503, "Could not verify pending ingest connections; key unchanged") from None
        with store.db:
            store.set("stream_key", secrets.token_urlsafe(32), stream_id)
        try:
            for connection in targets:
                response = await media.client.post(app.state.config.media_api + f"/v3/rtmpconns/kick/{connection}")
                if response.status_code != 404:
                    response.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(503, "Key changed, but closing pending connections failed; disconnect ingest and retry") from None
        return settings(stream_id)

    @api.get("/status")
    async def status(stream_id: StreamId = "stream"):
        row, store = get_stream(stream_id), app.state.store
        media = app.state.medias.get(stream_id)
        disk = app.state.media
        if row["archived_at"]:
            return {"stream_id": stream_id, "stream_name": row["name"], "archived": True,
                    "online": False, "media_available": False, "session_id": None, "started_at": None,
                    "bitrate_mbps": 0, "bitrate_history": [], "tracks": [], "recording": None,
                    "disk_free_bytes": disk.disk_free(), "min_free_bytes": disk.min_free_bytes,
                    "warning": None, "analysis": {"enabled": False, "state": "archived", "provider": None,
                                                   "error": None, "last_seen": None},
                    "face_count": store.db.execute("SELECT COUNT(*) FROM faces WHERE stream_id=?", (stream_id,)).fetchone()[0]}
        enabled = store.get("analysis_enabled", stream_id) == "1"
        heartbeat = dict(media.heartbeat)
        if time.monotonic() - media.heartbeat_at > 10:
            heartbeat.update(state="unavailable", error="No recent heartbeat from face worker")
        if not enabled:
            heartbeat.update(state="disabled", error=None)
        elif media.disk_free() < media.min_free_bytes:
            heartbeat.update(state="paused", error="Low disk space; face analysis will resume after space is freed")
        return {"stream_id": stream_id, "stream_name": row["name"], "archived": False,
                "online": media.online, "media_available": media.available,
                "session_id": media.session_id, "started_at": media.started_at,
                "bitrate_mbps": media.bitrate, "bitrate_history": list(media.meter.history),
                "tracks": media.tracks, "recording": media.recording,
                "disk_free_bytes": media.disk_free(), "min_free_bytes": media.min_free_bytes,
                "warning": media.warning, "analysis": {**heartbeat, "enabled": enabled},
                "face_count": store.db.execute("SELECT COUNT(*) FROM faces WHERE stream_id=?", (stream_id,)).fetchone()[0]}

    @api.post("/analysis")
    async def analysis(body: Analysis, stream_id: StreamId = "stream"):
        get_media(stream_id)
        store = app.state.store
        with store.db:
            store.set("analysis_enabled", int(body.enabled), stream_id)
            store.invalidate_catalog(stream_id)
        return {"enabled": body.enabled}

    @api.get("/sessions")
    async def sessions(stream_id: StreamId = "stream"):
        get_stream(stream_id)
        return {"items": [dict(row) for row in app.state.store.db.execute(
            "SELECT * FROM sessions WHERE stream_id=? ORDER BY started_at DESC LIMIT 100", (stream_id,))]}

    @api.get("/faces")
    async def faces(session_id: Annotated[str | None, Query(max_length=64)] = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 100,
                    offset: Annotated[int, Query(ge=0)] = 0, stream_id: StreamId = "stream"):
        get_stream(stream_id)
        return app.state.store.faces(session_id, limit, offset, stream_id)

    @api.get("/faces/{face_id}/thumbnail")
    async def thumbnail(face_id: int, stream_id: str | None = None):
        row = app.state.store.db.execute("SELECT thumbnail, stream_id FROM faces WHERE id=?", (face_id,)).fetchone()
        if row is None or (stream_id is not None and row["stream_id"] != stream_id):
            raise HTTPException(404, "Face not found")
        return Response(bytes(row[0]), media_type="image/jpeg")

    @api.delete("/faces", status_code=204)
    async def clear_faces(stream_id: StreamId = "stream"):
        get_stream(stream_id)
        store = app.state.store
        with store.db:
            store.db.execute("DELETE FROM faces WHERE stream_id=?", (stream_id,))
            store.invalidate_catalog(stream_id)
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @api.delete("/faces/{face_id}", status_code=204)
    async def delete_face(face_id: int, stream_id: str | None = None):
        store = app.state.store
        row = store.db.execute("SELECT stream_id FROM faces WHERE id=?", (face_id,)).fetchone()
        if row is None or (stream_id is not None and row[0] != stream_id):
            raise HTTPException(404, "Face not found")
        with store.db:
            if not store.db.execute("DELETE FROM faces WHERE id=?", (face_id,)).rowcount:
                raise HTTPException(404, "Face not found")
            store.invalidate_catalog(row[0])
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @api.get("/recordings")
    async def recordings(stream_id: StreamId = "stream"):
        get_stream(stream_id)
        return app.state.media.recordings(stream_id)

    @api.post("/recordings/start")
    async def start_recording(stream_id: StreamId = "stream"):
        return await get_media(stream_id).start()

    @api.post("/recordings/stop", status_code=204)
    async def stop_recording(stream_id: StreamId = "stream"):
        await get_media(stream_id).stop()

    @api.get("/recordings/{recording_id}/file")
    async def recording_file(recording_id: UUID, download: bool = False, stream_id: str | None = None):
        key = str(recording_id)
        row = app.state.store.db.execute("SELECT status, stream_id FROM recordings WHERE id=?", (key,)).fetchone()
        target = app.state.media.recordings_dir / f"{key}.mp4"
        if row is None or (stream_id is not None and row["stream_id"] != stream_id) or not target.is_file():
            raise HTTPException(404, "Recording not found")
        if row[0] not in ("ready", "interrupted"):
            raise HTTPException(409, "Recording is not available for playback yet")
        return FileResponse(target, media_type="video/mp4", filename=f"kunas-labs-{key}.mp4",
                            content_disposition_type="attachment" if download else "inline")

    @api.delete("/recordings/{recording_id}", status_code=204)
    async def delete_recording(recording_id: UUID, stream_id: str | None = None):
        store, key = app.state.store, str(recording_id)
        row = store.db.execute("SELECT status, stream_id FROM recordings WHERE id=?", (key,)).fetchone()
        if row is None or (stream_id is not None and row["stream_id"] != stream_id):
            raise HTTPException(404, "Recording not found")
        if row[0] == "recording":
            raise HTTPException(409, "Stop recording before deleting it")
        (app.state.media.recordings_dir / f"{key}.mp4").unlink(missing_ok=True)
        with store.db:
            store.db.execute("DELETE FROM recordings WHERE id=?", (key,))

    @api.get("/live/{file}")
    @api.get("/streams/{stream_id}/live/{file}")
    async def live(file: str, request: Request, stream_id: str = "stream"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(m3u8|mp4|m4s|ts)", file):
            raise HTTPException(404, "Media file not found")
        media = get_media(stream_id)
        params = [(key, value) for key, value in request.query_params.multi_items()
                  if key in ("_HLS_msn", "_HLS_part", "_HLS_skip")]
        headers = {key: request.headers[key] for key in ("range", "if-range") if key in request.headers}
        upstream = media.client.build_request("GET", media.hls_base + "/" + file,
                                              params=params, headers=headers)
        try:
            response = await media.client.send(upstream, stream=True,
                auth=httpx.BasicAuth("reader", app.state.config.internal_token))
        except httpx.HTTPError:
            raise HTTPException(503, "Live stream is temporarily unavailable") from None
        if response.status_code not in (200, 206):
            code = 404 if response.status_code == 404 else 503
            await response.aclose()
            raise HTTPException(code, "Live stream is not ready")
        headers = {key: value for key, value in response.headers.items()
                   if key in ("content-type", "content-length", "content-range", "accept-ranges")}
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code, headers=headers,
                                 background=BackgroundTask(response.aclose))

    app.include_router(api)

    @app.post("/internal/media/auth", status_code=204)
    async def media_auth(body: MediaAuth):
        allowed = False
        stream_id = body.path.removeprefix("live/")
        if body.path == f"live/{stream_id}":
            if body.action == "publish" and body.protocol == "rtmp" and body.user == "publisher":
                # MediaMTX's path manager can be waiting for this callback while
                # a management request is querying its API. Never wait on a lock
                # here: synchronously reject only the stream being changed.
                row = app.state.store.stream(stream_id)
                if row and not row["archived_at"] and stream_id not in app.state.admission_blocks:
                    allowed = equal(body.password, app.state.store.get("stream_key", stream_id))
                    if allowed and body.id:
                        app.state.admissions[body.id] = stream_id
            elif body.action == "read" and body.protocol in ("rtmp", "rtsp", "hls") and body.user == "reader":
                row = app.state.store.stream(stream_id)
                allowed = bool(row and not row["archived_at"]) and equal(body.password, app.state.config.internal_token)
        if not allowed:
            raise HTTPException(401, "Not authorized")

    worker = APIRouter(prefix="/internal", dependencies=[Depends(internal)])

    @worker.get("/worker/config")
    async def worker_config():
        return analysis_config("stream")

    def analysis_config(stream_id):
        cfg, store, media = app.state.config, app.state.store, app.state.medias[stream_id]
        low_disk = media.disk_free() < media.min_free_bytes
        return {"stream_id": stream_id, "enabled": store.get("analysis_enabled", stream_id) == "1" and not low_disk,
                "session_id": media.session_id,
                "catalog_version": int(store.get("catalog_version", stream_id)), "rtsp_url": media.rtsp_url,
                "analysis_fps": cfg.analysis_fps, "detection_threshold": cfg.detection_threshold,
                "pause_reason": "Low disk space" if low_disk else None}

    @worker.get("/worker/configs")
    async def worker_configs():
        return {"streams": [analysis_config(row["id"]) for row in app.state.store.streams() if not row["archived_at"]]}

    @worker.post("/worker/heartbeat", status_code=204)
    async def heartbeat(body: Heartbeat):
        media = app.state.medias.get(body.stream_id)
        if media is None or media.archived:
            raise HTTPException(409, "Stream is unknown or archived")
        media.heartbeat = {**body.model_dump(exclude={"stream_id"}), "last_seen": utcnow()}
        media.heartbeat_at = time.monotonic()

    @worker.post("/observations")
    async def observations(body: Batch):
        store, media = app.state.store, app.state.medias.get(body.stream_id)
        age = (datetime.now(timezone.utc) - body.captured_at).total_seconds()
        if (media is None or media.archived or store.get("analysis_enabled", body.stream_id) != "1"
                or not media.online or body.session_id != media.session_id
                or body.catalog_version != int(store.get("catalog_version", body.stream_id)) or not -2 <= age <= 15):
            raise HTTPException(409, "Observation is stale or analysis is disabled")
        if media.disk_free() < media.min_free_bytes:
            raise HTTPException(409, "Face storage paused: insufficient disk space")
        try:
            return {"accepted": store.observations(body)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    app.include_router(worker)
    return app


app = create_app()
