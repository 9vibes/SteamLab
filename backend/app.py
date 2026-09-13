import asyncio
import contextlib
import hashlib
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, StrictBool
from starlette.background import BackgroundTask

from .config import Config
from .media import Media
from .store import Store, utcnow

COOKIE = "steamlab_session"
SESSION_TTL = 12 * 60 * 60


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


class Observation(Input):
    embedding: list[FiniteFloat] = Field(min_length=128, max_length=128)
    thumbnail: str = Field(max_length=133336)
    confidence: FiniteFloat = Field(ge=0, le=1)
    quality: FiniteFloat = Field(ge=0, le=1)


class Batch(Input):
    session_id: str = Field(max_length=64)
    catalog_version: int = Field(ge=0)
    captured_at: AwareDatetime
    faces: list[Observation] = Field(max_length=20)


class Heartbeat(Input):
    state: str = Field(max_length=32)
    provider: str = Field(max_length=64)
    error: str | None = Field(default=None, max_length=256)


class MediaAuth(BaseModel):
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
        media = app.state.media = Media(cfg, store)
        app.state.sessions = {}
        app.state.login_attempts = deque()
        app.state.heartbeat = {"state": "unavailable", "provider": None, "error": None, "last_seen": None}
        app.state.heartbeat_at = 0
        monitor = asyncio.create_task(media.monitor()) if monitoring else None
        try:
            yield
        finally:
            if monitor:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
            await media.close()
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

    def settings():
        cfg, store = app.state.config, app.state.store
        return {"rtmp_url": f"rtmp://{cfg.public_host}:{cfg.rtmp_port}/live",
                "stream_key": f"stream?user=publisher&pass={store.get('stream_key')}",
                "analysis_enabled": store.get("analysis_enabled") == "1",
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
    async def get_settings():
        return settings()

    @api.post("/stream/key")
    async def rotate_key():
        media, store = app.state.media, app.state.store
        async with media.lock:
            try:
                path = await media.read_path()
            except (httpx.HTTPError, ValueError):
                raise HTTPException(409, "Media server must be reachable to rotate the key") from None
            if path.get("ready") or path.get("source"):
                raise HTTPException(409, "Disconnect the stream before rotating its key")
            # Auth callbacks share this lock. Snapshot all already-open RTMP
            # connections, including handshakes that have not announced tracks.
            try:
                connections, page = [], 0
                while True:
                    response = await media.client.get(app.state.config.media_api + "/v3/rtmpconns/list",
                                                      params={"itemsPerPage": 1000, "page": page})
                    response.raise_for_status()
                    data = response.json()
                    connections.extend(item["id"] for item in data["items"])
                    page += 1
                    if page >= data["pageCount"]:
                        break
            except (httpx.HTTPError, ValueError, KeyError):
                raise HTTPException(503, "Could not verify pending ingest connections; key unchanged") from None
            with store.db:
                store.set("stream_key", secrets.token_urlsafe(32))
            try:
                for connection in connections:
                    response = await media.client.post(app.state.config.media_api + f"/v3/rtmpconns/kick/{connection}")
                    if response.status_code != 404:
                        response.raise_for_status()
            except httpx.HTTPError:
                raise HTTPException(503, "Key changed, but closing pending connections failed; disconnect ingest and retry") from None
        return settings()

    @api.get("/status")
    async def status():
        media, store = app.state.media, app.state.store
        enabled = store.get("analysis_enabled") == "1"
        heartbeat = dict(app.state.heartbeat)
        if time.monotonic() - app.state.heartbeat_at > 10:
            heartbeat.update(state="unavailable", error="No recent heartbeat from face worker")
        if not enabled:
            heartbeat.update(state="disabled", error=None)
        elif media.disk_free() < media.min_free_bytes:
            heartbeat.update(state="paused", error="Low disk space; face analysis will resume after space is freed")
        return {"online": media.online, "media_available": media.available,
                "session_id": media.session_id, "started_at": media.started_at,
                "bitrate_mbps": media.bitrate, "bitrate_history": list(media.meter.history),
                "tracks": media.tracks, "recording": media.recording,
                "disk_free_bytes": media.disk_free(), "min_free_bytes": media.min_free_bytes,
                "warning": media.warning, "analysis": {**heartbeat, "enabled": enabled},
                "face_count": store.db.execute("SELECT COUNT(*) FROM faces").fetchone()[0]}

    @api.post("/analysis")
    async def analysis(body: Analysis):
        store = app.state.store
        with store.db:
            store.set("analysis_enabled", int(body.enabled))
            store.invalidate_catalog()
        return {"enabled": body.enabled}

    @api.get("/sessions")
    async def sessions():
        return {"items": [dict(row) for row in app.state.store.db.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 100")]}

    @api.get("/faces")
    async def faces(session_id: Annotated[str | None, Query(max_length=64)] = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 100,
                    offset: Annotated[int, Query(ge=0)] = 0):
        return app.state.store.faces(session_id, limit, offset)

    @api.get("/faces/{face_id}/thumbnail")
    async def thumbnail(face_id: int):
        row = app.state.store.db.execute("SELECT thumbnail FROM faces WHERE id=?", (face_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Face not found")
        return Response(bytes(row[0]), media_type="image/jpeg")

    @api.delete("/faces", status_code=204)
    async def clear_faces():
        store = app.state.store
        with store.db:
            store.db.execute("DELETE FROM faces")
            store.invalidate_catalog()
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @api.delete("/faces/{face_id}", status_code=204)
    async def delete_face(face_id: int):
        store = app.state.store
        with store.db:
            if not store.db.execute("DELETE FROM faces WHERE id=?", (face_id,)).rowcount:
                raise HTTPException(404, "Face not found")
            store.invalidate_catalog()
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @api.get("/recordings")
    async def recordings():
        return app.state.media.recordings()

    @api.post("/recordings/start")
    async def start_recording():
        return await app.state.media.start()

    @api.post("/recordings/stop", status_code=204)
    async def stop_recording():
        await app.state.media.stop()

    @api.get("/recordings/{recording_id}/file")
    async def recording_file(recording_id: UUID, download: bool = False):
        key = str(recording_id)
        row = app.state.store.db.execute("SELECT status FROM recordings WHERE id=?", (key,)).fetchone()
        target = app.state.media.recordings_dir / f"{key}.mp4"
        if row is None or not target.is_file():
            raise HTTPException(404, "Recording not found")
        if row[0] not in ("ready", "interrupted"):
            raise HTTPException(409, "Recording is not available for playback yet")
        return FileResponse(target, media_type="video/mp4", filename=f"kunas-labs-{key}.mp4",
                            content_disposition_type="attachment" if download else "inline")

    @api.delete("/recordings/{recording_id}", status_code=204)
    async def delete_recording(recording_id: UUID):
        media, store, key = app.state.media, app.state.store, str(recording_id)
        async with media.lock:
            row = store.db.execute("SELECT status FROM recordings WHERE id=?", (key,)).fetchone()
            if row is None:
                raise HTTPException(404, "Recording not found")
            if row[0] == "recording":
                raise HTTPException(409, "Stop recording before deleting it")
            (media.recordings_dir / f"{key}.mp4").unlink(missing_ok=True)
            with store.db:
                store.db.execute("DELETE FROM recordings WHERE id=?", (key,))

    @api.get("/live/{file}")
    async def live(file: str, request: Request):
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(m3u8|mp4|m4s|ts)", file):
            raise HTTPException(404, "Media file not found")
        media = app.state.media
        params = [(key, value) for key, value in request.query_params.multi_items()
                  if key in ("_HLS_msn", "_HLS_part", "_HLS_skip")]
        headers = {key: request.headers[key] for key in ("range", "if-range") if key in request.headers}
        upstream = media.client.build_request("GET", app.state.config.hls_base + "/" + file,
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
        if body.path == "live/stream":
            if body.action == "publish" and body.protocol == "rtmp" and body.user == "publisher":
                async with app.state.media.lock:
                    allowed = equal(body.password, app.state.store.get("stream_key"))
            elif body.action == "read" and body.protocol in ("rtmp", "rtsp", "hls") and body.user == "reader":
                allowed = equal(body.password, app.state.config.internal_token)
        if not allowed:
            raise HTTPException(401, "Not authorized")

    worker = APIRouter(prefix="/internal", dependencies=[Depends(internal)])

    @worker.get("/worker/config")
    async def worker_config():
        cfg, store, media = app.state.config, app.state.store, app.state.media
        low_disk = media.disk_free() < media.min_free_bytes
        return {"enabled": store.get("analysis_enabled") == "1" and not low_disk, "session_id": media.session_id,
                "catalog_version": int(store.get("catalog_version")), "rtsp_url": cfg.rtsp_url,
                "analysis_fps": cfg.analysis_fps, "detection_threshold": cfg.detection_threshold,
                "pause_reason": "Low disk space" if low_disk else None}

    @worker.post("/worker/heartbeat", status_code=204)
    async def heartbeat(body: Heartbeat):
        app.state.heartbeat = {**body.model_dump(), "last_seen": utcnow()}
        app.state.heartbeat_at = time.monotonic()

    @worker.post("/observations")
    async def observations(body: Batch):
        store, media = app.state.store, app.state.media
        age = (datetime.now(timezone.utc) - body.captured_at).total_seconds()
        if (store.get("analysis_enabled") != "1" or not media.online or body.session_id != media.session_id
                or body.catalog_version != int(store.get("catalog_version")) or not -2 <= age <= 15):
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
