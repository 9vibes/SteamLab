# KUNAS/Labs on Umbrel

Install **KUNAS/Labs** (`kunas-steamlab`) from the KUNAS community store:

```text
https://github.com/9vibes/KNS-Umbrel
```

This package targets Linux x86-64 with an NVIDIA GPU, a driver compatible with CUDA
12.4, and NVIDIA Container Toolkit configured for Docker. The worker requires a real
CUDA provider and does not silently fall back to CPU. The source repository retains
CPU Docker Compose support for non-Umbrel installations.

## Multistream (1.1.0)

Four-stream support requires version 1.1.0. Deploy matching backend, frontend,
worker, and MediaMTX configuration
atomically, including the Umbrel MediaMTX template. Do not mix old images with the
new configuration. No additional ingest port, container, or app ID is needed;
`kunas-steamlab`, web port 28081, RTMP port 21935, and the data directory stay unchanged.

- Up to four active feeds share one administrator. The original becomes **Stream 1**
  (`stream`, `live/stream`) and keeps its existing publishing key. Additional feeds
  have their own keys and paths `live/stream-<32 lowercase UUID hex digits>` on
  the same server URL. Select a feed before copying its complete OBS key.
- Add and rename feeds; names are trimmed, 1-64 characters, without control
  characters. Browser selection only changes the view: all feeds continue on the
  server with independent analysis toggles, recording, bitrate, and separate face groups.
- Archive an additional feed only while offline and not recording, with MediaMTX
  reachable and confirming no pending publisher. Stream 1 cannot be archived.
  Archive retains face/session/recording history and frees an active slot, but
  disables ingest/analysis. IDs are never reused and there is no restore endpoint;
  archived history remains readable/deletable and names remain editable.
- `MAX_FACES` remains one shared total including archived face groups. The low-disk
  reserve is shared across all recordings and analysis. One initialized NVIDIA
  engine fairly round-robins bounded latest-frame slots from up to four independent
  FFmpeg decoders; achieved per-feed analysis FPS depends on hardware and load,
  not just the configured capture target.
- Migration adds stream ownership to existing sessions, faces, and recordings and
  backfills them to Stream 1 without moving or overwriting files, changing row IDs,
  or replacing the existing key. Default settings retain their original keys;
  additional settings use `stream:{id}:{key}`. Back up the whole stopped app first.

The original scoped REST calls still default to Stream 1. HLS playback uses
`/api/streams/{id}/live/index.m3u8` to keep relative playlists/segments scoped;
`/api/live/{file}` remains a default-stream alias. See [API.md](API.md) for registry
and worker contracts and [VERIFICATION.md](VERIFICATION.md) for pending multistream
hardware checks and completed automated tests. These changes retain the tested CUDA 12.4.1/cuDNN 9.1 and FFmpeg 4.4 RTSP
compatibility behavior, including strict CUDA initialization with no CPU fallback.

Stop recording and back up the complete stopped app before updating. Do not
uninstall it. After updating, reload the dashboard and re-enable analysis per feed.
The migration changes the database schema: rolling back requires restoring a
matching pre-upgrade data backup, not merely selecting older container images.

## First launch

1. Open KUNAS/Labs from Umbrel. The browser port is **28081**.
2. Use the generated application password shown by Umbrel. No username is required.
3. In Settings, copy the server URL and the complete stream key into OBS.
4. Set OBS to H.264 video, AAC audio, and a 1-second keyframe interval.
5. Enable face analysis in the Faces tab. Confirm `CUDAExecutionProvider` appears.
6. Start recording manually when required.

The RTMP ingest port is **21935/TCP**. The advertised hostname defaults to Umbrel's
device `.local` name. If your encoder cannot resolve that name, use your Umbrel
server's LAN/VPN IP in OBS instead, preserving port `21935` and path `/live`.
On supported umbrelOS versions, the app's environment settings can override
`PUBLIC_HOST`, `COOKIE_SECURE`, and the analysis/storage settings without editing
source files. Only change `COOKIE_SECURE` to `true` when using HTTPS.

RTMP is plaintext. The ingest port listens on host interfaces for LAN encoders;
do not forward it through your router or expose it to the public Internet. Use a
VPN for remote publishing and HTTPS or a trusted private network for dashboard access.
Tor/browser access does not make RTMP ingest available through Tor.

## Packaging and persistence

- App password: Umbrel's deterministic `APP_PASSWORD`.
- Internal reader/worker token: Umbrel's separately derived app-specific `APP_SEED`.
- Publishing key: generated independently and persisted by KUNAS/Labs in SQLite.
- Persistent app data: `${APP_DATA_DIR}/data`, including SQLite and recordings.
- A one-shot root container initializes only app-owned data directories for UID/GID
  65532. Backend and face worker run unprivileged afterward.
- Only nginx connects to Umbrel's shared network. Backend, worker, and MediaMTX use
  a package-private network; their HTTP/RTSP/control ports are not host-published.
  nginx uses the `steamlab-backend` private alias to avoid generic service-name
  collisions with other Umbrel apps.
- The web image contains nginx configuration. MediaMTX configuration is a `.template`
  in the store package so Umbrel carries it through app updates.

Analysis resets to disabled after backend restart. Recording never automatically
resumes. Deleting face data does not redact recordings. Back up the entire app-data
directory while stopped; it contains sensitive face thumbnails and embeddings.

## Image releases

GitHub Actions tests the backend, native CPU model parity, and browser interface,
then publishes amd64 images to GHCR. The `v1.1.0` release publishes:

- `ghcr.io/9vibes/steamlab-web:1.1.0`
- `ghcr.io/9vibes/steamlab-backend:1.1.0`
- `ghcr.io/9vibes/steamlab-worker:1.1.0-cpu`
- `ghcr.io/9vibes/steamlab-worker:1.1.0-cuda`

Version 1.1.0 adds four independent streams, recording and catalog isolation, and
bounded shared-engine analysis. It retains the CUDA 12.4.1/cuDNN 9.1 and FFmpeg 4.4 RTSP
compatibility fixes from 1.0.3. The `kunas-steamlab` app ID, repository/image names,
ports, credentials, and data locations stay unchanged. Update the existing app;
do not uninstall it. Stop recording before updating and re-enable analysis afterward.

The Umbrel package uses the CUDA image and pins published image digests. Packages
must permit anonymous pulls before a store update is published. Repository visibility
alone does not guarantee GitHub Container Registry package visibility.

To verify anonymous registry access and optionally download/checksum every image
layer without retaining it, run with the backend Python dependencies installed:

```sh
python scripts/verify_images.py --pull \
  ghcr.io/9vibes/steamlab-web:1.1.0 \
  ghcr.io/9vibes/steamlab-backend:1.1.0 \
  ghcr.io/9vibes/steamlab-worker:1.1.0-cuda
```

This helper does not use GitHub credentials or Docker's credential configuration.
The CUDA image alone is approximately 2.7 GB compressed; allow sufficient disk
space for unpacked images as well as recordings.
