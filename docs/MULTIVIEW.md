# Multi-view and automatic recording

Implementation contract for the Multi-view and default-on automatic recording
capabilities in version 1.2.0. The existing four-stream schema, publishing keys,
storage layout, proxy boundaries, ports, and app IDs are unchanged. There is no
new database format or API migration beyond the existing 1.1.0 migration.

## Dashboard

The stream workspace offers Single view / Multi-view options. Single view keeps
the existing selected-stream layout and archive access. Multi-view displays only
non-archived connected (`online`) streams, two columns on desktop and one on small
screens, up to four feeds (second row appears for the third/fourth live feed).
Each tile has a named video, recording controls/telemetry, and its own Faces,
Signal Directory, Recordings, and Settings tabs BELOW the video, never beside it.
Since 1.2.2, Signal Directory is an inline tab rather than a sidebar drawer. Each
panel keeps its own management target while directory data and CRUD forms remain
shared. Empty multi-view explains how to connect a stream or switch to single view.
Selecting archived history switches to single view. Preserve per-stream keys/scoped
requests and stable keys.

Switching view must not POST recording/analysis actions. Avoid duplicate IDs for
tabs, selects, credentials, SVG gradients, and dialogs; give every video/region a
stream-specific accessible name. All streams stay muted initially. Prefer sharing
the existing Dashboard implementation with a compact multi-view layout prop.

## Automatic recording

`AUTO_RECORD=true` is the default for standalone and Umbrel deployments; false
retains manual-only recording. This is backend-owned, independent of any viewer.
When a configured stream is confirmed ready, its monitor starts a recording using
the same H.264/disk/auth guards as manual recording. Each stream is independent.
Existing live streams also auto-record when the backend starts/restarts.

**Upgrade warning:** With the default `AUTO_RECORD=true`, already-live feeds
record after an update or backend restart, **even if you previously stopped
recording manually**. For manual-only operation, stop encoders before updating,
configure the operator deployment setting `AUTO_RECORD=false`, and apply it before
reconnecting encoders. Settings displays this read-only policy; it is not a browser
toggle, and Stop is not a persistent opt-out.

Explicit Stop suppresses auto-recording for the current publisher fingerprint,
including when no recording exists because storage is paused. Suppression survives
temporary media API unavailability and clears for a genuinely different publisher
connection. The latch does not survive a backend process restart; use
`AUTO_RECORD=false` for a persistent opt-out. Explicit Start retries/resumes recording.
Start is idempotent if a
recording is already active, avoiding manual/automatic races creating duplicates.

Low disk space stops recording. Automatic recovery requires free space above the
shared `MIN_FREE_GB` reserve plus headroom for five continuous seconds, even across
publisher reconnects; manual Stop still takes priority. Headroom is 10% of the
reserve, bounded to 16-256 MiB. The reserve is not a quota or per-feed allocation.
Spawn failures/unexpected recorder exits are latched for that connection, surfaced
as errors, and require manual Start or reconnection; no one-file-per-second retries.
Stream disconnect finalizes the old recording; a genuine publisher reconnect
automatically starts a new file unless `AUTO_RECORD=false` requires explicit Start.
Recordings are never automatically deleted, including on low disk. Face analysis
remains opt-in and disabled after backend restart.

## API additions

GET `/api/settings?stream_id=...` adds `auto_record: boolean` (read-only policy).
GET `/api/status?stream_id=...` adds:

- `auto_record: boolean`
- `recording_state`: `recording`, `waiting`, `stopped`, `disk_paused`, `error`,
  `manual`, or `archived`.
- `recording_error: string | null`, a sanitized recorder error.
- `can_stop_recording: boolean`, including a remembered publisher that can be
  suppressed during a media API outage. False before any publisher has been seen.

Existing `recording` metadata, scoped start/stop routes, and response shapes remain.
POST `/api/recordings/start` returns the existing active recording (200) if already
running. POST Stop remains idempotent. No browser auto-start effect is needed.

Display recording policy and state in each tile, including the meaning of Stop
and reconnection. Use generic Recording labels instead of Manual recording.
Single view keeps Stop available during a media-server outage when the backend
reports `can_stop_recording`; the action does not require media-server availability.
Publisher reconnects do not clear an existing low-disk recovery requirement.
