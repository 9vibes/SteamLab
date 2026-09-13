# KUNAS/Labs API contract

All timestamps are UTC ISO 8601. Browser API uses same-origin HttpOnly session cookies.
POST/DELETE requests (except login) require `X-CSRF-Token` from GET `/api/auth/me` or login.
Errors are JSON `{ "detail": "message" }`. Browser resources are private. No mock data.

## Browser endpoints

- POST `/api/auth/login` JSON `{password}` -> `{csrf_token}`; sets session cookie.
- GET `/api/auth/me` -> `{csrf_token}` or 401.
- POST `/api/auth/logout` -> 204.
- GET `/api/status` -> `{online, media_available, session_id, started_at, bitrate_mbps,
  bitrate_history: number[], tracks: string[], recording: {id, started_at} | null,
  disk_free_bytes, min_free_bytes, warning: string | null,
  analysis: {enabled, state, provider, error, last_seen}, face_count}`.
- GET `/api/settings` -> `{rtmp_url, stream_key, analysis_enabled, match_threshold,
  detection_threshold, face_retention_days, max_faces, analysis_fps}`.
- POST `/api/stream/key` -> settings; rejects while live or media unavailable.
- POST `/api/analysis` JSON `{enabled: boolean}` -> `{enabled}`.
- GET `/api/faces?session_id=...&limit=100&offset=0` -> `{items: Face[], total}`.
  Face: `{id: integer, label, first_seen, last_seen, sightings: integer,
  detection_confidence: number, match_similarity: number | null, thumbnail_url}`.
  Similarity is raw cosine [-1, 1], NOT a probability. First group observation has null similarity.
- GET `/api/faces/{id}/thumbnail` -> JPEG.
- DELETE `/api/faces/{id}` -> 204.
- DELETE `/api/faces` -> 204 (clears thumbnails, embeddings, and sightings).
- GET `/api/sessions` -> `{items: [{id, started_at, ended_at}]}` (latest 100).
- POST `/api/recordings/start` -> `{id, started_at}` (409 offline/already recording).
- POST `/api/recordings/stop` -> 204 (idempotent).
- GET `/api/recordings` -> `{items: [{id, started_at, ended_at, status,
  size_bytes, duration_seconds, download_url, playback_url, error}]}`.
  Status is `recording`, `ready`, `interrupted`, or `error`.
- GET `/api/recordings/{id}/file` -> MP4, supports Range; `?download=1` attachment.
- DELETE `/api/recordings/{id}` -> 204 (409 if active).
- GET `/api/live/{file}` -> authenticated HLS proxy (player uses `/api/live/index.m3u8`).
- GET `/health` -> public liveness only.

## Internal worker endpoints

Not exposed through nginx. Every call requires `Authorization: Bearer INTERNAL_TOKEN`.

- GET `/internal/worker/config` -> `{enabled, session_id, catalog_version: integer,
  rtsp_url, analysis_fps, detection_threshold, pause_reason: string | null}`.
  session_id is null when offline. Low disk space makes enabled false and sets pause_reason.
- POST `/internal/worker/heartbeat` JSON `{state, provider, error: string | null}` -> 204.
- POST `/internal/observations` JSON `{session_id, catalog_version, captured_at,
  faces: [{embedding: number[128], thumbnail: base64 JPEG, confidence: number, quality: number}]}`
  -> `{accepted: integer}`. Return 409 if session/version changed or analysis disabled.
  Max 20 faces per request, JPEG <= 100KB, finite normalized vectors required.

Worker only detects/embeds. Backend groups and stores in SQLite, atomically with thumbnails.
Worker does not access database. Never log authenticated stream URLs or image/embedding payloads.

## Deployment contract

Services: `web` nginx :80 (host 8080 default), `backend` :8000, `mediamtx` :1935
(only published media port), `worker` (no ports). Media path `live/stream`.
RTMP URL is `rtmp://<PUBLIC_HOST>:<RTMP_PORT>/live`; stream key
is `stream?user=publisher&pass=<generated secret>` (copy entire string into OBS).
MediaMTX HTTP auth callback: `http://backend:8000/internal/media/auth`.
Read credentials: user `reader`, password INTERNAL_TOKEN. API/metrics excluded from media
HTTP authentication and accessible only on private Docker network.
MediaMTX API base `http://mediamtx:9997`; RTSP `rtsp://mediamtx:8554/live/stream`;
HLS base `http://mediamtx:8888/live/stream`.
Backend data volume mounted `/data`; SQLite `/data/steamlab.sqlite3`, recordings `/data/recordings`.
Backend env: ADMIN_PASSWORD (>=16 chars), INTERNAL_TOKEN (>=32 chars), PUBLIC_HOST,
RTMP_PORT=1935, COOKIE_SECURE=false (true behind HTTPS), DATA_DIR=/data,
MIN_FREE_GB=2, FACE_RETENTION_DAYS=7, MAX_FACES=2000, MATCH_THRESHOLD=0.5,
DETECTION_THRESHOLD=0.85, ANALYSIS_FPS=2.
Worker env: BACKEND_URL=http://backend:8000, INTERNAL_TOKEN, MODEL_DIR=/models,
INFERENCE_DEVICE=cpu|cuda. CUDA requested must not silently fall back.
Run one backend process only; recording and monitoring are owned by that process.
