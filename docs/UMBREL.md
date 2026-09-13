# SteamLab NVIDIA on Umbrel

Install **SteamLab NVIDIA** (`kunas-steamlab`) from the KUNAS community store:

```text
https://github.com/9vibes/KNS-Umbrel
```

This package targets Linux x86-64 with an NVIDIA GPU, a driver compatible with CUDA
12.4, and NVIDIA Container Toolkit configured for Docker. The worker requires a real
CUDA provider and does not silently fall back to CPU. The source repository retains
CPU Docker Compose support for non-Umbrel installations.

## First launch

1. Open SteamLab from Umbrel. The browser port is **28081**.
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
- Publishing key: generated independently and persisted by SteamLab in SQLite.
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
then publishes amd64 images to GHCR. A `v1.0.0` tag publishes:

- `ghcr.io/9vibes/steamlab-web:1.0.0`
- `ghcr.io/9vibes/steamlab-backend:1.0.0`
- `ghcr.io/9vibes/steamlab-worker:1.0.0-cpu`
- `ghcr.io/9vibes/steamlab-worker:1.0.0-cuda`

The Umbrel package uses the CUDA image and pins published image digests. Packages
must permit anonymous pulls before a store update is published. Repository visibility
alone does not guarantee GitHub Container Registry package visibility.

To verify anonymous registry access and optionally download/checksum every image
layer without retaining it, run with the backend Python dependencies installed:

```sh
python scripts/verify_images.py --pull \
  ghcr.io/9vibes/steamlab-web:1.0.0 \
  ghcr.io/9vibes/steamlab-backend:1.0.0 \
  ghcr.io/9vibes/steamlab-worker:1.0.0-cuda
```

This helper does not use GitHub credentials or Docker's credential configuration.
The CUDA image alone is approximately 2.7 GB compressed; allow sufficient disk
space for unpacked images as well as recordings.
