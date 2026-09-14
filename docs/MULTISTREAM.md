# Four-stream implementation contract

One administrator, backend process, MediaMTX server, and analysis worker. At most
four active stream definitions; each can publish, record, and analyze concurrently.
Selection only changes the browser view. Shared analysis/storage limits remain global.

The four-stream foundation shipped in 1.1.0. **Version 1.2.0 adds Multi-view and
default-on automatic recording**, governed by the authoritative
[MULTIVIEW.md](MULTIVIEW.md) contract, while retaining the existing schema,
publishing keys, storage, proxy boundaries, ports, and app IDs.

## Multi-view (1.2.0)

Single view preserves selected-feed access, including offline settings/history.
Multi-view includes only connected (`online`), non-archived feeds, up to four in
two desktop columns (2x2 at capacity) or one mobile column. Each named video tile
has recording controls/telemetry and its own independent Faces, Recordings, and
Settings tabs below the video, never beside it. Players initially stay muted.
The directory and add/rename/archive management remain outside the grid; directory
selection targets management without filtering live tiles. Selecting archived
history switches to Single view. Empty Multi-view explains how to connect a stream
or switch views. Stable stream keys and scoped requests keep tiles independent;
view/tab changes never POST recording/analysis actions or affect server feeds.

## Automatic recording (1.2.0)

`AUTO_RECORD=true` is the default in backend `Config`, source/Umbrel Compose, the
environment example, and setup-generated `.env`; `false` selects manual-only
recording. Each backend monitor starts one recorder on confirmed readiness,
including existing live feeds after backend startup/restart, without any viewer.
Automatic and explicit Start share H.264, disk, and reader authentication guards.

**Upgrade warning:** The default `AUTO_RECORD=true` records already-live feeds
after an update or backend restart, **even if previously stopped manually**.
For manual-only operation, stop encoders before updating, set the operator
deployment setting `AUTO_RECORD=false`, and apply it before reconnecting encoders.
The dashboard policy is read-only; Stop is not a persistent opt-out.

- Explicit Stop latches the current publisher fingerprint (source ID and ready
  time), even if disk-paused with no recorder. Temporary MediaMTX API outages do
  not clear it; a genuinely different publisher connection does. This latch is
  in-memory, not a persistent opt-out across backend restarts.
- Disconnect finalizes the old file; a genuine publisher reconnect starts a new
  one unless `AUTO_RECORD=false` requires explicit Start. Explicit Start
  overrides Stop or a failure latch and retries/resumes subject to the guards.
  Start returns the existing active recording with HTTP 200 to prevent duplicates.
- Low disk pauses recording. Automatic recovery requires free space above the
  reserve plus headroom for five continuous seconds, even across publisher
  reconnects, unless manually stopped.
  Headroom is 10% of the reserve bounded to 16-256 MiB.
- A spawn failure/unexpected recorder exit is latched for the same connection,
  with `recording_state=error` and a sanitized error. No repeated automatic attempts
  or file churn; retry using explicit Start or a genuine reconnect.

Video auto-recording is intentional and independent of face analysis, which stays
opt-in and disabled after backend restart; it never enables face recognition.
Four simultaneous recordings grow storage at the combined rate of all feeds.
`MIN_FREE_GB` is a shared safety reserve, not a quota or a per-stream allowance.
Monitor capacity and manage separate storage/quotas and video retention deliberately;
recordings are never automatically deleted, including on low disk.

## Registry and migration

Default stream ID `stream`, initial name `Stream 1`, path `live/stream`; retain its
publishing key and all existing data. New IDs: `stream-` plus 32 lowercase UUID hex
digits; paths `live/{id}`. Names are trimmed, 1-64 characters, no control characters.
Default stream can be renamed but not archived. Added streams can be archived only
when offline, not recording, and the media server confirms no pending publisher.
Archived streams retain faces, sessions and recordings but cannot ingest or analyze.
IDs are never reused. Archive frees an active slot; no restore endpoint in this version.

The 1.1.0 migration creates the `streams` table with id, name, created_at, archived_at,
adds stream_id ownership to sessions/faces/recordings, and backfills legacy rows to
`stream`, preserving IDs and files. Version 1.2.0 retains this schema and introduces
no new database format or API migration beyond the existing 1.1.0 migration.
Default settings retain original keys. Other settings use `stream:{id}:{key}`.
MAX_FACES is the total application-wide cap, not four times the configured value.

## Browser API

- GET `/api/streams` -> `{items: Stream[], max_streams: 4, active_count}`. Includes
  archived streams so their history remains accessible; contains no publishing keys.
- POST `/api/streams` `{name}` -> Stream (201); fifth active stream returns 409.
- PATCH `/api/streams/{id}` `{name}` -> Stream.
- DELETE `/api/streams/{id}` -> 204; archives, preserves history; rejects default,
  online, recording, or media-unavailable (409).

Stream: `{id, name, created_at, archived_at: string|null, is_default, online,
media_available, recording: {id, started_at}|null, bitrate_mbps, analysis_enabled}`.

Since 1.1.0, scoped routes accept an optional `stream_id` query parameter (default `stream`):
`/api/status`, `/api/settings`, `/api/stream/key`, `/api/analysis`, `/api/sessions`,
`/api/faces` GET/DELETE, `/api/recordings` GET, `/api/recordings/start`,
`/api/recordings/stop`. Return existing shapes plus stream_id where useful.
Status adds `stream_id`, `stream_name`, `archived`.
Settings adds `stream_id`, `stream_name`, `archived`; archived key is empty.
In 1.2.0, status also adds `auto_record: boolean`, a sanitized
`recording_error: string | null`, `can_stop_recording: boolean`, and
`recording_state`: `recording`, `waiting`, `stopped`, `disk_paused`,
`error`, `manual`, or `archived`. Settings adds read-only `auto_record: boolean`.
Existing `recording` metadata remains `{id, started_at} | null`. No route changes;
Start becomes idempotent (200 when already active), and Stop remains idempotent
(204). See [API.md](API.md) for state semantics and recording guards.
Single face/recording endpoints remain globally unique and authenticated; optional
stream_id on requests must match ownership. Returned resource URLs include ownership
query parameters. Archives allow reading/deleting history and renaming only.
Analysis POST response unchanged `{enabled}`. Shared auth/logout routes unchanged.

HLS player MUST use `/api/streams/{id}/live/index.m3u8` so relative playlists and
segments retain scope. Original `/api/live/{file}` remains a default-stream alias.

## Worker API

- GET `/internal/worker/configs` (since 1.1.0) -> `{streams: WorkerConfig[]}`, active streams only.
- Existing GET `/internal/worker/config` remains default-stream config for rollout.
- WorkerConfig: previous fields (`enabled, session_id, catalog_version, rtsp_url,
  analysis_fps, detection_threshold, pause_reason`) plus `stream_id`.
- POST `/internal/observations`: previous body plus `stream_id` (default `stream`
  for the shipped worker); session, catalog generation and face grouping are scoped.
- POST `/internal/worker/heartbeat`: previous body plus `stream_id` (default `stream`).
  Unknown/archived streams return 409; no heartbeats affect other streams.

The multistream worker shares one initialized engine, never concurrently calls its mutable
OpenCV detector, and fairly round-robins latest-frame slots. Up to four independent
FFmpeg decoders drain in parallel. Config polling/heartbeat remain independent from
inference. Config/session/version invalidation and decoder shutdown are per stream;
list reorder and display-name changes must not restart surviving streams. Capture
FPS is a target, not a guarantee of per-stream analysis throughput. Preserve strict
CUDA initialization and the tested FFmpeg RTSP option detection.

Worker HTTP requests use curl child processes with nonblocking pipes, response-size
limits, and a 1.5-second wall-clock deadline plus bounded kill/reap cleanup. Delivery
failures remain visible per stream until a successful current-generation submission.
The worker images include curl; local worker and integration runs also require it.

Publisher authentication never waits on the management lock. Rotation/archive
temporarily block only the target stream's admissions while querying MediaMTX,
preventing callback/path-manager deadlocks and preserving other feeds.

## Deployment

MediaMTX pattern `~^live/(stream|stream-[a-f0-9]{32})$`; backend DB authorizes every
path and credential, regex alone never grants access. Update both source config and
Umbrel template. No new host ports or containers. Keep default compose/app ID, data
volume, cookie, and GPU image requirements. Deploy all matching 1.2.0 components
together, retaining the four-stream MediaMTX configuration introduced in 1.1.0.
