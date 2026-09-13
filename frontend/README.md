# SteamLab Frontend

Private React + TypeScript + Vite broadcast dashboard. All displayed operational data comes from the endpoints in `../docs/API.md`; there is no demo mode, external font service, analytics, or browser persistence of credentials.

## Development

```sh
npm ci
npm run dev
npm run build
```

Vite proxies `/api` to `http://localhost:8000`. Open the Vite origin, not the backend origin, so login cookies, media, thumbnails, and API calls stay same-origin. The frontend uses locally installed Open Sans with system sans-serif fallbacks and system monospace fonts.

In the Alpine workspace without npm on PATH, the ignored frontend-local bootstrap is available:

```sh
/usr/bin/node .tools/package/bin/npm-cli.js run build
```

## Browser Tests

```sh
npx playwright install chromium
npm test
```

For an existing system Chromium installation:

```sh
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium npm test
```

Tests intercept API requests with contract fixtures. They do not need or modify backend data. Actual encoder ingest, native Safari HLS, recording codecs, and nginx cookie/proxy behavior still require full-stack verification.

## Integration

- Build the container from the **repository root**: `docker build -f frontend/Dockerfile -t steamlab-web .`. The infrastructure-owned `infra/nginx.conf` is copied to `/etc/nginx/conf.d/default.conf`; the frontend does not own that configuration.
- The public application shell is served by nginx. All operational endpoints and media are private, same-origin `/api` requests with HttpOnly cookie authentication. CSRF stays in memory and is sent as `X-CSRF-Token` on mutations. A 401 returns to login.
- Status polls settle before waiting 1 second and polling again. Catalog polls fetch faces, sessions, recordings, and settings serially, then wait 4 seconds. Slow requests never produce overlapping cycles. API requests time out after 15 seconds. Navigation/logout aborts polls; catalog filters may take up to the next cycle to load.
- Hls.js is loaded only for an active stream. Playback reconnects on session/input changes and failures, with native HLS fallback. Players, timers, event listeners, and requests are disposed on exit. Playback begins muted to accommodate autoplay policies.
- Face detection confidence is shown as a percentage. Match similarity and its threshold are **raw cosine values (-1 to 1), not probabilities**. Null similarity is labeled `No match yet`. Session filtering and pagination use the API, not client-side catalog truncation. Clear-all always clears every session and says so in its confirmation.
- Stream keys are masked by default. Reveal expires after 30 seconds and hides when the page becomes hidden. Clipboard writes require HTTPS or localhost; otherwise the UI explains manual selection. Regeneration is disabled while live, while media is unavailable, or while status is stale.
- Analysis thresholds, retention, maximum faces, and sampling are read-only because the contract exposes no settings mutation endpoint. Only analysis enablement and stream-key regeneration are editable.
- Recording playback and downloads use backend-returned URLs. The backend must finalize playable MP4s and support byte ranges; active recordings cannot be played, downloaded, or deleted from the UI.
- All timestamps are displayed in the browser's local timezone; the API supplies UTC. Storage units are binary (GiB/MiB), explicitly labeled.

The production bundle includes Hls.js as a deferred chunk. Vite may report a large-chunk advisory for that third-party playback engine; it is not loaded on login or while offline.
