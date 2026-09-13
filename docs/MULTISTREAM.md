# Four-stream implementation contract

One administrator, backend process, MediaMTX server, and analysis worker. At most
four active stream definitions; each can publish, record, and analyze concurrently.
Selection only changes the browser view. Shared analysis/storage limits remain global.

## Registry and migration

Default stream ID `stream`, initial name `Stream 1`, path `live/stream`; retain its
publishing key and all existing data. New IDs: `stream-` plus 32 lowercase UUID hex
digits; paths `live/{id}`. Names are trimmed, 1-64 characters, no control characters.
Default stream can be renamed but not archived. Added streams can be archived only
when offline, not recording, and the media server confirms no pending publisher.
Archived streams retain faces, sessions and recordings but cannot ingest or analyze.
IDs are never reused. Archive frees an active slot; no restore endpoint in this version.

`streams` table has id, name, created_at, archived_at. Add stream_id ownership to
sessions/faces/recordings and backfill legacy rows to `stream`; preserve IDs and files.
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

Existing scoped routes gain optional `stream_id` query parameter (default `stream`):
`/api/status`, `/api/settings`, `/api/stream/key`, `/api/analysis`, `/api/sessions`,
`/api/faces` GET/DELETE, `/api/recordings` GET, `/api/recordings/start`,
`/api/recordings/stop`. Return existing shapes plus stream_id where useful.
Status adds `stream_id`, `stream_name`, `archived`.
Settings adds `stream_id`, `stream_name`, `archived`; archived key is empty.
Single face/recording endpoints remain globally unique and authenticated; optional
stream_id on requests must match ownership. Returned resource URLs include ownership
query parameters. Archives allow reading/deleting history and renaming only.
Analysis POST response unchanged `{enabled}`. Shared auth/logout routes unchanged.

HLS player MUST use `/api/streams/{id}/live/index.m3u8` so relative playlists and
segments retain scope. Original `/api/live/{file}` remains a default-stream alias.

## Worker API

- NEW GET `/internal/worker/configs` -> `{streams: WorkerConfig[]}`, active streams only.
- Existing GET `/internal/worker/config` remains default-stream config for rollout.
- WorkerConfig: previous fields (`enabled, session_id, catalog_version, rtsp_url,
  analysis_fps, detection_threshold, pause_reason`) plus `stream_id`.
- POST `/internal/observations`: previous body plus `stream_id` (default `stream`
  for the shipped worker); session, catalog generation and face grouping are scoped.
- POST `/internal/worker/heartbeat`: previous body plus `stream_id` (default `stream`).
  Unknown/archived streams return 409; no heartbeats affect other streams.

The new worker shares one initialized engine, never concurrently calls its mutable
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
volume, cookie, and GPU image requirements. Version 1.1.0 packages all matching components.
