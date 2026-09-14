# KUNAS/Labs API contract

All timestamps are UTC ISO 8601. Browser API uses same-origin HttpOnly session cookies.
POST/PATCH/DELETE requests (except login) require `X-CSRF-Token` from GET `/api/auth/me` or login.
Errors are JSON `{ "detail": "message" }`. Browser resources are private. No mock data.

The multistream API was introduced in **1.1.0** and is retained in **1.2.0**. Deploy matching backend,
frontend, worker, and MediaMTX configuration
atomically. [MULTISTREAM.md](MULTISTREAM.md) is the implementation contract;
[VERIFICATION.md](VERIFICATION.md) records checks and remaining hardware validation.

**1.2.0 additions:** the automatic recording fields and Start behavior
documented below follow [MULTIVIEW.md](MULTIVIEW.md). No routes, scoping,
authentication, or existing recording metadata shapes change; status/settings gain
fields and Start becomes idempotent (200 rather than 409 when already recording).

## Stream registry

- GET `/api/streams` -> `{items: Stream[], max_streams: 4, active_count}`. Includes
  archived streams for history access; never includes publishing keys.
- POST `/api/streams` JSON `{name}` -> Stream (201). A fifth active stream returns 409.
- PATCH `/api/streams/{id}` JSON `{name}` -> Stream, including for archived streams.
- DELETE `/api/streams/{id}` -> 204. Archives rather than deletes history. Returns
  409 for the default stream, an online/recording stream, an unavailable media
  server, or a pending publisher. MediaMTX must confirm the feed can be archived.

Stream: `{id, name, created_at, archived_at: string | null, is_default, online,
media_available, recording: {id, started_at} | null, bitrate_mbps, analysis_enabled}`.
Names are trimmed, 1-64 characters, with no control characters. The default ID is
`stream`, initial name `Stream 1`, path `live/stream`; its existing key is preserved.
New IDs are `stream-` plus 32 lowercase UUID hex digits, with paths `live/{id}`.
IDs are never reused. Archive frees an active slot while preserving face, session,
and recording history; no restore endpoint exists in this version. The default
stream can be renamed but never archived. Archived streams permit only renaming
and reading/deleting history, not publishing, analysis, or new recordings.

## Scope and compatibility

The following original routes accept optional `stream_id` query parameters,
defaulting to `stream` when omitted: GET `/api/status`, GET `/api/settings`, POST
`/api/stream/key`, POST `/api/analysis`, GET `/api/sessions`, GET/DELETE `/api/faces`,
GET `/api/recordings`, and POST `/api/recordings/start` or `/api/recordings/stop`.
Existing response shapes are retained with ownership fields where useful.

For example, GET `/api/status` is equivalent to GET `/api/status?stream_id=stream`;
POST `/api/analysis?stream_id=stream-<32 lowercase UUID hex digits>` toggles only
that feed. DELETE `/api/faces` clears only the default feed, not all feeds.
Selecting a feed in the browser does not stop or redirect other server feeds:
analysis toggles, sessions, face groups, recording, and bitrate monitoring are independent.

Single-face and single-recording routes keep globally unique IDs and authentication.
An optional `stream_id` must match resource ownership; omitting it preserves access
by globally unique ID. Returned resource URLs include ownership query parameters.
Authentication and logout routes are unchanged.

## Browser endpoints

- POST `/api/auth/login` JSON `{password}` -> `{csrf_token}`; sets session cookie.
- GET `/api/auth/me` -> `{csrf_token}` or 401.
- POST `/api/auth/logout` -> 204.
- GET `/api/status` -> `{online, media_available, session_id, started_at, bitrate_mbps,
  bitrate_history: number[], tracks: string[], recording: {id, started_at} | null,
  auto_record: boolean, recording_state, recording_error: string | null, can_stop_recording: boolean,
  disk_free_bytes, min_free_bytes, warning: string | null,
  analysis: {enabled, state, provider, error, last_seen}, face_count,
  stream_id, stream_name, archived}`.
- GET `/api/settings` -> `{rtmp_url, stream_key, analysis_enabled, match_threshold,
  detection_threshold, face_retention_days, max_faces, analysis_fps, auto_record: boolean,
  stream_id, stream_name, archived}`. Archived `stream_key` is empty.
- POST `/api/stream/key` -> settings; rejects while live, with a pending publisher,
  media unavailable, or archived.
- POST `/api/analysis` JSON `{enabled: boolean}` -> `{enabled}`.
- GET `/api/faces?session_id=...&limit=100&offset=0` -> `{items: Face[], total}`.
  Face: `{id: integer, label, first_seen, last_seen, sightings: integer,
  detection_confidence: number, match_similarity: number | null, thumbnail_url}`.
  Similarity is raw cosine [-1, 1], NOT a probability. First group observation has null similarity.
- GET `/api/faces/{id}/thumbnail` -> JPEG.
- DELETE `/api/faces/{id}` -> 204.
- DELETE `/api/faces` -> 204 (clears thumbnails, embeddings, and sightings).
- GET `/api/sessions` -> `{items: [{id, started_at, ended_at}]}` (latest 100).
- POST `/api/recordings/start` -> `{id, started_at}` (200). Returns the existing
  active recording if already running; otherwise explicitly retries/resumes,
  overriding manual Stop or a latched failure. Guards still reject offline/media
  unavailable, archived, low-disk, or non-H.264 feeds with 409; FFmpeg spawn failure
  returns 503.
- POST `/api/recordings/stop` -> 204 (idempotent).
- GET `/api/recordings` -> `{items: [{id, started_at, ended_at, status,
  size_bytes, duration_seconds, download_url, playback_url, error}]}`.
  Status is `recording`, `ready`, `interrupted`, or `error`.
- GET `/api/recordings/{id}/file` -> MP4, supports Range; `?download=1` attachment.
- DELETE `/api/recordings/{id}` -> 204 (409 if active).
- GET `/api/streams/{id}/live/{file}` -> authenticated, directory-scoped HLS proxy.
  The player must use `/api/streams/{id}/live/index.m3u8`, not a query-scoped master
  playlist: relative variant playlists and segments must retain the stream directory.
- GET `/api/live/{file}` -> default-stream HLS alias, including `/api/live/index.m3u8`.
- GET `/health` -> unauthenticated backend liveness only; blocked by nginx, not a browser route.

## Automatic recording (1.2.0)

`auto_record` in both status and settings is the read-only backend policy from
`AUTO_RECORD` (default `true`; set `false` in deployment configuration for manual-only
recording). It is not writable through the settings API. Status `recording_state`
is one of the following, separate from a saved recording's `status`:

| Value | Meaning |
| --- | --- |
| `recording` | A recorder is active, automatic or manually started |
| `waiting` | Automatic mode is waiting for a confirmed ready feed/start |
| `stopped` | Explicit Stop suppresses recording for the current/last publisher fingerprint |
| `disk_paused` | An online automatic-mode feed is waiting for stable disk recovery |
| `error` | A recorder failure is latched for the current/last publisher fingerprint |
| `manual` | Automatic recording is disabled and no higher-priority state applies |
| `archived` | The archived stream cannot record |

**Upgrade warning:** With the default `AUTO_RECORD=true`, already-live feeds
automatically record after an update or backend restart, **even if recording was
previously stopped manually**. To retain manual-only operation, stop encoders
before updating, configure the operator setting `AUTO_RECORD=false`, and apply it
before reconnecting. A browser Stop is not a persistent policy setting.

State precedence is archived, active recording, manual Stop, latched failure,
manual-only policy, then automatic waiting/disk pause. An offline automatic feed
reports `waiting` unless Stop or failure is latched. `recording_error` is a sanitized
string or `null`; archived status reports `null`. Existing `recording` remains
`{id, started_at} | null`.

`can_stop_recording` reports whether Stop can halt an active recording or suppress
the remembered publisher. It remains true during a media API outage when that
publisher can be suppressed, and is false before any publisher has been observed,
after explicit Stop, or for an archived stream. Single view exposes Stop during
such outages without requiring media-server availability.

Each backend monitor starts recording once its configured feed is confirmed ready,
including existing live feeds after backend startup/restart. No browser action or
open dashboard is required. Automatic Start uses the same H.264, disk, and reader
authentication guards as explicit Start. Disconnect finalizes the old file;
a genuinely different publisher connection starts a new one unless
`AUTO_RECORD=false`, which requires explicit Start.

Idempotent Stop (204) latches the publisher fingerprint (source ID and ready time),
including while disk-paused with no active recorder. Temporary MediaMTX API outages
do not clear Stop or failure suppression for the same connection. Reconnect with a
different fingerprint clears those latches; explicit Start overrides them and retries
subject to the usual guards. The latches are backend-process state, not a persisted
opt-out across restarts.

Low disk stops recording. Automatic recovery requires free space above the shared
reserve plus headroom for five continuous seconds, even after publisher reconnects;
headroom is 10% of the reserve,
bounded to 16-256 MiB. Manual Stop takes priority over recovery. Spawn failures or
unexpected recorder exits latch an error for that connection: no repeated automatic
attempts or file churn until explicit Start or a genuine reconnect.

Multi-view is presentation only: changing views/tabs does not POST recording or
analysis actions. Face analysis remains opt-in and disabled after backend restart;
default-on video recording never enables face analysis or identity recognition.

## Internal worker endpoints

Not exposed through nginx. Every call requires `Authorization: Bearer INTERNAL_TOKEN`.

- GET `/internal/worker/configs` -> `{streams: WorkerConfig[]}`, active streams only.
- GET `/internal/worker/config` -> WorkerConfig for default `stream`, retained for
  compatibility with the shipped worker; it does not enumerate additional feeds.
- WorkerConfig: `{stream_id, enabled, session_id, catalog_version: integer,
  rtsp_url, analysis_fps, detection_threshold, pause_reason: string | null}`.
  `session_id` is null when offline. Low disk space makes `enabled` false and sets
  `pause_reason` across feeds.
- POST `/internal/worker/heartbeat` JSON `{stream_id, state, provider, error: string | null}`
  -> 204. Omitted `stream_id` defaults to `stream` for the shipped worker.
  Unknown/archived streams return 409; heartbeats never affect another stream.
- POST `/internal/observations` JSON `{stream_id, session_id, catalog_version, captured_at,
  faces: [{embedding: number[128], thumbnail: base64 JPEG, confidence: number, quality: number}]}`
  -> `{accepted: integer}`. Omitted `stream_id` defaults to `stream` for the shipped
  worker. Session, catalog generation, and face grouping are scoped to that stream.
  Return 409 for unknown/archived streams, changed session/version, disabled analysis,
  or low disk space; a session belonging to another stream cannot be used.
  Max 20 faces per request, JPEG <= 100KB, finite normalized vectors required.

Worker only detects/embeds. Backend groups and stores in SQLite, atomically with thumbnails.
Worker does not access database. Never log authenticated stream URLs or image/embedding payloads.

One initialized engine is shared across feeds (one NVIDIA engine in CUDA mode).
Up to four FFmpeg decoders capture independently into bounded latest-frame slots;
inference fairly round-robins these slots and never concurrently calls the mutable
OpenCV detector. Config polling and heartbeats stay independent from inference.
Config/session/catalog invalidation and decoder shutdown are per stream; list
reorder and display-name changes must not restart surviving streams. Capture FPS
is a target, not guaranteed per-stream analysis throughput; achieved FPS depends on
hardware and load. Preserve strict CUDA initialization and tested FFmpeg 4.4 RTSP
option detection when deploying the multistream worker.

## Deployment contract

Services: `web` nginx :80 (host 8080 default), `backend` :8000, `mediamtx` :1935
(only published media port), `worker` (no ports). Media paths `live/{id}`; default
`live/stream`. No new host ports, containers, or app ID changes.
RTMP URL is `rtmp://<PUBLIC_HOST>:<RTMP_PORT>/live`; stream key
is `stream?user=publisher&pass=<generated secret>` (copy entire string into OBS).
Additional feeds use `<id>?user=publisher&pass=<that feed's secret>` at the same
server URL and ingest port. MediaMTX uses `~^live/(stream|stream-[a-f0-9]{32})$` in
both source configuration and Umbrel template; the regex alone grants no access.
The backend database authorizes every registered, non-archived path and credential.
MediaMTX HTTP auth callback: `http://backend:8000/internal/media/auth`.
Read credentials: user `reader`, password INTERNAL_TOKEN. API/metrics excluded from media
HTTP authentication and accessible only on private Docker network.
MediaMTX API base `http://mediamtx:9997`; RTSP `rtsp://mediamtx:8554/live/{id}`;
HLS base `http://mediamtx:8888/live/{id}`. Both media protocols remain private.
Backend data volume mounted `/data`; SQLite `/data/steamlab.sqlite3`, recordings `/data/recordings`.
Backend env: ADMIN_PASSWORD (>=16 chars), INTERNAL_TOKEN (>=32 chars), PUBLIC_HOST,
RTMP_PORT=1935, COOKIE_SECURE=false (true behind HTTPS), DATA_DIR=/data,
AUTO_RECORD=true, MIN_FREE_GB=2, FACE_RETENTION_DAYS=7, MAX_FACES=2000, MATCH_THRESHOLD=0.5,
DETECTION_THRESHOLD=0.85, ANALYSIS_FPS=2.
Worker env: BACKEND_URL=http://backend:8000, INTERNAL_TOKEN, MODEL_DIR=/models,
INFERENCE_DEVICE=cpu|cuda. CUDA requested must not silently fall back.
Run one backend process only; recording and monitoring are owned by that process.
`MAX_FACES` is the shared application-wide total, including archived face groups,
not a per-stream cap. `MIN_FREE_GB` is one shared reserve: low disk stops recording,
pauses analysis, and rejects observations across feeds. Analysis resets to disabled
per feed on restart; automatic video recovery follows the policy above. Recordings
are never automatically deleted. Four concurrent feeds grow storage at their
combined recording rate; the reserve is neither a quota nor a per-feed allocation.
Monitor capacity and manage separate storage/quotas and video retention explicitly.

The 1.1.0 migration creates the `streams` registry and adds `stream_id` ownership to sessions,
faces, and recordings, backfilling legacy rows to `stream`. Existing row IDs, files,
and the publishing key are preserved; files are not moved or overwritten. Default
settings retain original keys; additional settings use `stream:{id}:{key}`. Keep
the existing data volume, Compose/app ID, cookie, and GPU image requirements.
Version 1.2.0 retains this schema and introduces no new database format or API
migration beyond the existing 1.1.0 migration.
