# Verification

Implementation checks performed on September 13, 2026. This is not a claim of
production deployment or measured face-recognition accuracy.

## Automated checks

| Check | Result |
| --- | --- |
| Backend tests | 42 passed |
| Worker control, capture, download, and contract tests | 43 passed |
| Native inference test module | Skipped: compatible OpenCV/ONNX Runtime unavailable on this musl host |
| TypeScript and Vite production build | Passed |
| Chromium browser tests | 10 passed |
| Compose and MediaMTX YAML parsing | Passed |
| Model downloads and SHA256 validation | Passed |

Publication follow-up: the [v1.0.0 release workflow](https://github.com/9vibes/SteamLab/actions/runs/34778657447)
passed 97 backend/worker tests on Ubuntu/Python 3.12, including native OpenCV/ONNX
Runtime CPU model parity, and 10 browser tests. All four Docker image variants
built and published successfully. The Umbrel package and standalone GPU override
also passed Docker Compose 2.39.4 configuration validation. Every layer of the web,
backend, and CUDA worker release images was downloaded anonymously from GHCR and
SHA256-verified. Actual CUDA inference still requires the target NVIDIA host.

The browser tests use API fixtures, not a real face stream. Responsive checks cover
320, 390, 768, 1024, and 1440 pixels. Desktop/mobile screenshots were inspected.
Hls.js is deferred until a stream connects; Vite's large-chunk advisory remains for
that third-party playback engine.

## CUDA packaging follow-up (1.0.1)

- GPU runtime rebased to digest-pinned CUDA 12.4.1 / cuDNN 9.1 / Ubuntu 22.04;
  anonymous manifest/config verification is recorded in [CUDA.md](CUDA.md).
- Local Python 3.10.20 and 3.12.13: **99 tests passed on each**, including downloaded,
  checksum-verified native SFace/OpenCV parity and two new packaging regressions.
- `pip-audit -r worker/requirements-gpu.txt`: no known vulnerabilities found.
- Bandit worker scan: no medium/high findings; low findings are test assertions,
  existing shell-free FFmpeg subprocess calls, and existing heartbeat error handling.
- No local Docker daemon or target NVIDIA driver/device was available. Image builds
  and browser tests are release-workflow gates; GPU execution is a host-only check.

## Real media integration

`tests/integration_media.py` passed all 13 checks with native MediaMTX 1.12.3 and
FFmpeg/FFprobe 7.0.2-static:

1. Invalid publishing credentials rejected.
2. Offline key rotation revokes the previous publishing key.
3. Second publisher rejected without replacing the first.
4. Actual incoming bitrate is positive and codec tracks are reported.
5. Worker FFmpeg decoder samples complete RTSP frames and shuts down.
6. Authenticated HLS master/variant playlists and assets work; anonymous backend requests fail.
7. Anonymous direct MediaMTX HLS access denied.
8. Authenticated RTMP reader can decode media.
9. Anonymous RTSP reading denied.
10. Anonymous RTMP reading denied.
11. Manual recording produces a ready MP4 with H.264/AAC; FFprobe, decoding, metadata,
    authenticated download, and HTTP byte ranges succeed.
12. Publisher disconnect produces an interrupted but playable recording.
13. Reconnecting creates a new stream session without automatically recording.

The generated source is a test pattern plus tone. No external camera or personal
face footage was used. Recording uses FFmpeg's RTSP `-timeout` input option and
graceful stdin quit, with a bounded forced-shutdown fallback.

## Still required on the server

- Run the published Docker images on the server. Docker builds passed on GitHub
  Actions, but a full Umbrel installation and runtime test has not been performed.
- Test YuNet detection and recurring-face grouping on consented, representative
  footage; CPU model parity is now covered by the publication workflow.
- Verify `CUDAExecutionProvider` on the target NVIDIA GPU. CUDA image tag existence
  was checked, but neither GPU inference nor target-driver compatibility was tested.
- Test OBS, HTTPS secure cookies, private network bindings, and storage permissions.
- Test browser playback on the devices you will use, especially Safari's native HLS.
- Tune confidence, blur/size filters, and cosine threshold. Similarity is not a
  calibrated identity probability, and grouping may split or merge people incorrectly.

The default sampling resolution is 640x360 at 2 FPS, so small, distant, blurred,
occluded, or briefly visible faces may not enter the catalog. All settings and
deployment precautions are documented in the root README.

## Multistream verification (1.1.0)

Checks against [MULTISTREAM.md](MULTISTREAM.md) on September 13, 2026:

| Check | Result |
| --- | --- |
| Backend and worker regression suite | 235 passed |
| Native face-model test module | Skipped; compatible dependencies unavailable locally |
| Browser tests | 20 passed |
| TypeScript/Vite production build | Passed |
| Four-stream real-media integration | 21 of 21 checks passed |
| Standalone GPU and Umbrel Compose configuration | Passed |

The backend suite verifies genuine legacy-schema migration twice, preserving row
IDs, the original key, face blobs/embeddings, null-session recordings, and recording
files. It covers concurrent capacity enforcement, scoped catalogs and global limits,
independent recording/analysis/versioning, archive/history behavior, and restart safety.
Gated publisher-auth regressions exercise the MediaMTX callback/path-API deadlock
case and ensure key rotation/archive do not block another stream's authentication.

Worker tests verify one engine, fair scheduling, isolated generations, four bounded
frame slots, child-process shutdown, and stalled-delivery recovery. A real local HTTP
server and curl 8.22.0 additionally verify credentials, JSON escaping, HTTP errors,
redirect rejection, deadline termination, and successful requests after timeout.

The real-media harness used MediaMTX 1.12.3 and FFmpeg/FFprobe 7.0.2-static, generated
four distinguishable H.264/AAC feeds, and ran four simultaneous recorders. All four
HLS master/variant/asset chains and recording downloads/ranges/decoding passed.
Stopping, disconnecting, rotating, or archiving one feed left the other three
publishers and active recordings intact. Four actual worker configurations and curl
HTTP requests were exercised alongside four native RTSP raw-frame slots. This did
not run face inference on real people. Artifacts from the final run are at
`/tmp/opencode/steamlab-integration/run-morqxmg_/` in the implementation environment.

Desktop/mobile screenshots were regenerated and inspected. Browser tests use
synthetic API fixtures, including slow-response switching and archive management.

These are the local pre-release checks for 1.1.0, not a target-host deployment test. Build the matching
backend, frontend, and worker images (worker now also requires curl), and deploy
the matching MediaMTX configuration/template together. Before publication, rerun
native model parity on supported Python/glibc, test the CUDA image on the target
GPU, and measure four-feed inference throughput on consented representative footage.
The CUDA 12.4.1/cuDNN 9.1 pin and FFmpeg 4.4 compatibility logic remain unchanged;
sampling FPS is not a guarantee of achieved per-stream analysis throughput.
