# SteamLab

Self-hosted, single-stream OBS monitoring with an authenticated browser dashboard,
live HLS playback, manual MP4 recording, and optional face grouping. This initial
application has one administrator password and one stream (`live/stream`), not
multi-user accounts or an identity-recognition service. There is no demo data.
The exact API and service interface is [docs/API.md](docs/API.md).

**Umbrel:** the NVIDIA package is published through the
[KUNAS community store](https://github.com/9vibes/KNS-Umbrel).
See [docs/UMBREL.md](docs/UMBREL.md) for requirements, login, and OBS setup.

## Requirements

- Docker Engine and Docker Compose v2 (`docker compose`).
- OpenSSL on the host for the optional setup script.
- Enough disk space for recordings and biometric data; 2 GiB free is the default minimum guard, not a storage quota.
- OBS with H.264 video, AAC audio, and a 1-second keyframe interval. Streams are not transcoded.

## Start Locally

Run from the repository root:

```sh
sh scripts/setup.sh localhost
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

The script validates a hostname/IPv4 argument, requires installed OpenSSL,
generates independent random secrets under `umask 077`, and refuses to overwrite
an existing `.env`. It does not print passwords, install software, or start services.
Open `http://localhost:8080` and log in with `ADMIN_PASSWORD` from `.env`.
Both published ports default to loopback, so this works with OBS on the same host.

Manual alternative:

```sh
umask 077
cp .env.example .env
chmod 600 .env
openssl rand -hex 24
openssl rand -hex 32
```

Put the first generated value in `ADMIN_PASSWORD` and the second in
`INTERNAL_TOKEN`, and edit `PUBLIC_HOST` as needed. Do not reuse either secret as
the publishing key. Empty secrets cause Compose interpolation errors; minimum
lengths are 16 and 32 characters respectively and are also validated by the
backend. Use hex values to avoid `.env` quoting, `$` interpolation, and media URL
escaping problems. Never commit `.env` or share expanded `docker compose config`
output, container environment inspection, or authenticated stream URLs.

## OBS And Access

After login, copy the server URL and **entire** stream key from Settings:

```text
Server:     rtmp://<PUBLIC_HOST>:<RTMP_PORT>/live
Stream key: stream?user=publisher&pass=<generated publishing secret>
Full URL:   rtmp://<PUBLIC_HOST>:<RTMP_PORT>/live/stream?user=publisher&pass=<generated publishing secret>
```

The backend generates and persists the publishing secret; there is no public token
endpoint or `.env` publishing-key setting. Keep `?user=publisher&pass=...` intact
when pasting the stream key. Key rotation is available only while offline and with
MediaMTX reachable. A second publisher cannot replace an active publisher.

For remote OBS, set `PUBLIC_HOST` to the host's VPN DNS name or IPv4 address and
`RTMP_BIND` to its VPN interface IP. `PUBLIC_HOST` is an advertised address, not a
bind setting; it must not include a scheme, port, path, or query. Set `RTMP_PORT`
if changing the published port, then recreate services with `docker compose up -d`.
RTMP is plaintext: credentials and video are exposed in transit without a VPN.
Do not expose this ingest port to the public Internet. Restrict it to trusted VPN
publishers even though publish and read actions require credentials.

For browser access over the Internet, use an existing HTTPS reverse proxy:

- Proxy the whole origin to `http://127.0.0.1:8080` when the proxy runs on the host. A proxy container's loopback is not the Docker host; give it a deliberately configured private route to `web:80`, or to a private host bind address.
- Set `COOKIE_SECURE=true` and recreate the backend. Keep `false` only for local HTTP; secure cookies will not work over ordinary remote HTTP.
- Terminate TLS, set HSTS at that proxy, and disable caching for `/api/`, including HLS and recording downloads. Preserve query strings, cookies, CSRF headers, and Range requests. Allow long downloads with suitable read/send timeouts.
- Do not expose port 8080 publicly alongside the HTTPS proxy. Keep a host-local bind, or a private interface restricted to the proxy. Do not publish backend, RTSP, HLS, or MediaMTX API ports.

Only the static login/app shell is unauthenticated on the web origin. All browser
data, thumbnails, recordings, and HLS are authenticated by the backend. nginx
proxies **only `/api/`**; `/internal`, `/internal/…`, and `/health` return 404 and
are never forwarded. Backend `/health` is a private-network liveness endpoint.
There is no direct browser-to-MediaMTX route. Session cookies are HttpOnly;
mutating requests other than login also require `X-CSRF-Token`.

## Deployment Layout

| Service | Build / command | Connectivity |
| --- | --- | --- |
| `web` | Root build context, `frontend/Dockerfile`; final image serves static assets with nginx | Only web host port, default `127.0.0.1:8080` |
| `backend` | Root context, `backend/Dockerfile`; `uvicorn backend.app:app --host 0.0.0.0 --port 8000 --workers 1` | Private `:8000`; named data volume at `/data` |
| `mediamtx` | Pinned `bluenviron/mediamtx:1.12.3` | Only RTMP host port, default `127.0.0.1:1935` |
| `worker` | Root context, `worker/Dockerfile`; `python -m worker.main` | No host ports or database mount; CPU by default |

`infra/nginx.conf` is a server configuration mounted at
`/etc/nginx/conf.d/default.conf`; the frontend image serves its built files
from `/usr/share/nginx/html`. nginx sets security headers, a 3 MiB request body
limit, no-store responses, and one-hour idle read/send timeouts rather than a
one-hour total download limit. Proxy buffering is disabled; query strings and
Range headers pass through to the backend. The backend forwards the HLS control
query parameters `_HLS_msn`, `_HLS_part`, and `_HLS_skip` to MediaMTX, authenticating
upstream requests with private reader credentials. Worker observation batches go directly
to the backend, never through nginx: up to 20 faces with 100 KB JPEGs encoded as
base64, as specified in the API contract. Browser requests are small.

All four services share one named Compose bridge network, `steamlab_steamlab` by
default. This is not separate per-service isolation and is not an `internal: true`
network: containers can make outbound connections. Treat containers and Docker
administrators as trusted. Only web and RTMP are published; untrusted containers
must not be attached to this network. In particular, MediaMTX API authentication is
excluded deliberately, so network membership is its security boundary.

MediaMTX settings are checked against the
[upstream v1.12.3 configuration](https://github.com/bluenviron/mediamtx/blob/v1.12.3/mediamtx.yml):

- The only configured path is `live/stream`, with `overridePublisher: no`.
- HTTP authentication calls `http://backend:8000/internal/media/auth`. The backend checks the action, path, protocol, and credentials, denying anonymous reads, publisher reads, and unsupported actions. Read credentials are user `reader`, password `INTERNAL_TOKEN`, for RTMP/RTSP/HLS as applicable. Publishing uses user `publisher` and the separate persisted stream key.
- Only `api` and `metrics` actions bypass the callback. API is enabled at `http://mediamtx:9997` for backend monitoring; metrics and pprof listeners are disabled.
- RTSP is TCP-only at `rtsp://mediamtx:8554/live/stream`; HLS is private at `http://mediamtx:8888/live/stream` with `hlsVariant: fmp4`. This works over initial local HTTP; low-latency HLS requires TLS and is not enabled.
- WebRTC, SRT, playback, and MediaMTX recording are disabled. Media logging is `error` to reduce query-secret exposure. nginx and Uvicorn access logging are disabled; still treat diagnostic logs as sensitive.

Backend liveness gates web, MediaMTX, and worker startup; liveness must not depend
on MediaMTX being online. Backend monitoring handles media startup/reconnection.
Run exactly **one backend process and one backend replica**: it owns monitoring,
recording subprocesses, and the database lifecycle. Do not use reload or scale it.
Compose gives the backend 60 seconds to shut down recording cleanly.

## Data And Recording

The named volume `steamlab_data` contains `/data/steamlab.sqlite3`, face data, and
`/data/recordings`. The worker submits observations to the backend and never opens
SQLite. Treat face thumbnails and embeddings as sensitive biometric data: obtain
consent, restrict access, and use the shortest appropriate retention.

Analysis is **off initially and after every backend restart** and is explicitly enabled in the app. Defaults are
7-day face retention, `MAX_FACES=2000`, `ANALYSIS_FPS=2`,
`DETECTION_THRESHOLD=0.85`, and `MATCH_THRESHOLD=0.5`. Match similarity is raw
cosine similarity, not a probability or a verified identity. Face deletion removes
stored thumbnails, embeddings, and sightings; deleting face data does not redact
faces from existing video recordings.

Recording is manual: the backend runs FFmpeg with direct stream copy into one
fragmented MP4 file per manual recording session. It does not use MediaMTX's
recording feature, transcode video, or silently resume after a crash or restart.
Completed fragments can remain recoverable after interruption, but the unfinished
tail may be lost; the backend labels interrupted recordings separately from ready files.
Start a new recording explicitly after reconnecting or recovering.

`MIN_FREE_GB=2` is the backend's low-disk guard for starting/continuing recordings.
Below that reserve, the worker pauses decoding and the dashboard displays a storage
warning. Face analysis resumes when space recovers; recording must be started manually.
The backend also rejects in-flight face observations with HTTP 409 while space is low.
Recordings are **never automatically deleted**, including when space runs low;
delete or archive them deliberately. Face retention does not apply to videos.
Monitor actual volume-host capacity. Downloads/playback use authenticated,
Range-capable `/api/recordings/{id}/file`, with `?download=1` for attachments.

```sh
docker compose stop
# Back up the complete named data volume while stopped, using your volume backup tooling.
docker compose start
```

Back up the whole volume together so SQLite metadata and files stay consistent.
Protect backups as sensitive data. `docker compose down` preserves the volume;
**`docker compose down -v` destroys it**, including recordings and stream keys.
Keep `.env` securely backed up separately. Changing `INTERNAL_TOKEN` requires
recreating both backend and worker; existing internal readers need to reconnect.

## Optional NVIDIA GPU

CPU is the supported default. The GPU override selects `worker/Dockerfile.gpu`,
sets `INFERENCE_DEVICE=cuda`, and reserves one NVIDIA GPU:

```sh
docker compose -f compose.yaml -f compose.gpu.yaml config --quiet
docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
docker compose -f compose.yaml -f compose.gpu.yaml logs --tail=100 worker
```

Install a compatible NVIDIA host driver and NVIDIA Container Toolkit, configure
the Docker NVIDIA runtime, and verify container GPU access first. CUDA acceleration
is for **real SFace embedding inference only**; YuNet detection stays on CPU. The
worker checks its actual provider, disables CPU graph fallback for CUDA, and runs
a warm-up inference before reporting readiness. Initialization failure produces an
error heartbeat and a nonzero exit, not placeholder embeddings.

The GPU image uses `nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04`, CUDA 12.6.3,
cuDNN 9, Python 3.12, and `onnxruntime-gpu==1.22.0`. The CPU image uses
`onnxruntime==1.22.0`; both pin NumPy 2.2.6 and OpenCV headless 4.11.0.86.
Use a compatible NVIDIA host driver. The Compose device reservation alone does
not prove GPU inference works; verify the reported provider on real hardware.
Both images install checksum-verified models and licenses during the build.
Do not mount an empty directory over `/models` and hide those models.

## Verification

Deployment checks:

```sh
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose exec web nginx -t
docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').status)"
curl -i http://127.0.0.1:8080/api/auth/me
curl -i http://127.0.0.1:8080/internal/media/auth
curl -i http://127.0.0.1:8080/health
```

Expect unauthenticated `/api/auth/me` to return 401, blocked paths to return 404,
and internal liveness to return 200. Verify unauthenticated live playlists,
thumbnails, and recording files also return 401. Check `Cache-Control: no-store`
and the security headers on success and error responses. Only host web and RTMP
ports should appear in `docker compose ps`.

Backend tests from a local virtual environment (test dependencies must be present):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest backend
```

Worker test and native-model verification commands are in [worker/README.md](worker/README.md).
For frontend checks, run `npm ci`, `npm run build`, and `npm test` from `frontend/`
with Playwright's Chromium browser and system dependencies installed.
Complete an integration check with OBS: reject wrong publishing credentials and
anonymous RTMP/RTSP reads, deny a second publisher, log in, play HLS, start/stop and
download a recording (including Range requests), then test interrupted recording
recovery and the low-disk guard. Test HTTPS cookies and GPU provider selection on
the real deployment. Static configuration validation cannot establish these
application/runtime properties.

An opt-in native integration script exercises real MediaMTX and FFmpeg without Docker:

```sh
MEDIAMTX_BIN=/path/to/mediamtx FFMPEG_BIN=/path/to/ffmpeg FFPROBE_BIN=/path/to/ffprobe \
  INTEGRATION_ROOT=/existing/temporary/directory .venv/bin/python tests/integration_media.py
```

It requires free localhost ports 8000, 1935, 8554, 8888, and 9997; generates temporary
credentials; and leaves test artifacts in the specified directory. It does not use or
modify `.env` or your production data. See [docs/VERIFICATION.md](docs/VERIFICATION.md)
for the checks performed during implementation and the remaining deployment checks.
