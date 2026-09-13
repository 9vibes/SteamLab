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
