# Verification

## Inline Signal Directory (1.2.2)

The inline-tab change passed 45 Chromium browser tests and the frontend production
build locally. Tests cover tab order and keyboard navigation, independent management
targets in four panels, a single shared directory poll, unique IDs, scoped actions,
archived-history navigation, modal focus restoration, and retained native/HLS players.
The sidebar and drawer are removed. Desktop/mobile screenshots were regenerated.
Backend behavior and the recording policy are unchanged. Release CI and anonymous
image verification are separate publication gates.

## Signal Directory sidebar (1.2.1)

The sidebar change passed 41 Chromium browser tests, the frontend production
build, and diff whitespace checks locally. Tests cover a closed-by-default drawer,
keyboard entry/Escape/focus restoration, inert background controls, responsive
320/390/801/1440-pixel layouts, stream management, continued directory polling,
and retained native/HLS players without recording or analysis mutations. Gallery
screenshots were regenerated and inspected. Backend behavior is unchanged.
The [1.2.1 release workflow](https://github.com/9vibes/SteamLab/actions/runs/34802529083)
passed on Python 3.10 and 3.12, including native model checks and 41 browser tests.
All four image variants built and published successfully. Every layer of the web,
backend, and CUDA worker images was downloaded anonymously and SHA256-verified
before updating the Umbrel package. Target-host installation and GPU throughput
remain deployment checks.

Implementation checks performed on September 13-14, 2026. This is not a claim of
production deployment or measured face-recognition accuracy.

The 1.0.0/1.1.0 results below are historical and retain their original counts and
behavioral observations. Local checks for 1.2.0 Multi-view and automatic recording
are recorded separately below. Release CI will run; no 1.2.0 CI, image publication,
or anonymous image-verification result is claimed here.

## Historical automated checks (1.0.0)

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

## Historical real media integration (1.0.0)

Historical 1.0.0 manual-recording behavior, not the 1.2.0 default-on policy:

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

- Run the matching 1.2.0 Docker images on the server after publication. Historical
  1.0.0/1.1.0 Docker builds passed on GitHub Actions; this is not a 1.2.0 build result.
  A full Umbrel installation and runtime test has not been performed.
- Test YuNet detection and recurring-face grouping on consented, representative
  footage; CPU model parity passed in historical release workflows and will run
  again in the 1.2.0 workflow.
- Verify `CUDAExecutionProvider` on the target NVIDIA GPU. CUDA image tag existence
  was checked for historical releases, not verified here for 1.2.0; neither GPU
  inference nor target-driver compatibility was tested.
- Test OBS, HTTPS secure cookies, private network bindings, and storage permissions.
- Test browser playback on the devices you will use, especially Safari's native HLS.
- Tune confidence, blur/size filters, and cosine threshold. Similarity is not a
  calibrated identity probability, and grouping may split or merge people incorrectly.

The default sampling resolution is 640x360 at 2 FPS, so small, distant, blurred,
occluded, or briefly visible faces may not enter the catalog. All settings and
deployment precautions are documented in the root README.

## Historical multistream verification (1.1.0)

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

These were the local pre-release checks for 1.1.0, not a target-host deployment test.
The release required matching backend, frontend, and worker images (including curl)
and MediaMTX configuration/template. Native model parity was subsequently rerun
on supported Python/glibc as recorded below. Testing the CUDA image on the target
GPU and measuring four-feed inference throughput on consented representative footage
remain host checks.
The CUDA 12.4.1/cuDNN 9.1 pin and FFmpeg 4.4 compatibility logic remain unchanged;
sampling FPS is not a guarantee of achieved per-stream analysis throughput.

The [1.1.0 release workflow](https://github.com/9vibes/SteamLab/actions/runs/34789387734)
subsequently passed on both Python 3.10 and 3.12, including native face-model tests
and the 20 browser tests. All four image variants built and published successfully.
Every layer of the web, backend, and CUDA worker release images was downloaded
anonymously and SHA256-verified before updating the Umbrel package. The verifier
uses small bounded parallel byte ranges to tolerate download response-size limits;
five additional verifier tests passed locally. The release does not establish
four-stream inference throughput or successful installation on a particular GPU host.

## Multi-view and automatic recording (1.2.0)

The authoritative contract is [MULTIVIEW.md](MULTIVIEW.md). The following local
verification completed on September 14, 2026. These are source-level and native-media
results, not 1.2.0 release CI, Docker image, registry, or installed Umbrel results.

| Check | Result |
| --- | --- |
| Backend, worker, and registry-verifier tests | 291 passed |
| Native face-model test module | Skipped locally; compatible dependencies unavailable |
| Chromium browser tests | 34 passed |
| TypeScript/Vite production build | Passed |
| Real-media automatic-recording integration | 21 of 21 checks passed |
| Standalone GPU and Umbrel Compose configuration | Passed |

The backend suite includes 51 automatic-recording tests: default-on and manual-only
configuration, viewer-independent startup, restart and reconnect, concurrent
idempotent Start, per-publisher Stop/failure latches, sanitized errors, and database
failure before spawning FFmpeg. Disk tests cover bounded headroom, five continuous
recovery seconds, flapping publishers, manual Stop during storage pause, and Stop
during a media API outage. Reconnects cannot bypass storage recovery requirements.

Browser tests cover two, three, and four connected feeds; a synthetic release gallery; independent tabs/actions;
unique form, tab, SVG, and dialog IDs; per-feed mute and player cleanup; mode switching
without server mutations; empty and archived states; and Stop during a media outage.
Desktop and mobile screenshots were generated and inspected. Multi-view tool panels
scroll independently below their own video. Browser fixtures are synthetic and do
not demonstrate real face-recognition accuracy.

The final real-media run used MediaMTX 1.12.3 and FFmpeg/FFprobe 7.0.2-static. It
verified automatic recordings through database rows and actual MP4 bytes before
any viewer or Start request, four concurrent recorders, six-second manual Stop
suppression, automatic new files on reconnect and rotated-key publishing, HLS,
decodable recordings and byte ranges, archive/history preservation, and unchanged
sibling recording sessions. All 21 checks passed in 117.2 seconds including cleanup.
Artifacts are at `/tmp/opencode/steamlab-integration/run-4usb69ca/` in this workspace.

The [1.2.0 release workflow](https://github.com/9vibes/SteamLab/actions/runs/34797349505)
subsequently passed on Python 3.10 and 3.12, including native model parity and 34
browser tests. All four Docker image variants built and published successfully.
The web, backend, and CUDA worker images were downloaded anonymously and every
layer SHA256-verified before updating the Umbrel package. Installed Umbrel behavior,
target GPU inference, and four-feed throughput remain host checks. Face analysis
is still opt-in.

**Upgrade warning:** `AUTO_RECORD` defaults to `true` in 1.2.0 and records already-live
feeds after an update or backend restart, **even if previously stopped manually**.
For manual-only operation, stop encoders before updating, configure and apply the
operator setting `AUTO_RECORD=false`, then reconnect encoders. Stop survives a
temporary media API outage, not a backend process restart. Recordings are never
automatically deleted, and four feeds can substantially increase storage usage.
