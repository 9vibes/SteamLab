# Analysis Worker

This service only detects faces and produces embeddings. It never opens the backend
database, clusters faces, persists images, or logs stream URLs and observation payloads.

## Images and Configuration

Build from the repository root:

```sh
docker build -f worker/Dockerfile -t steamlab-worker .
docker build -f worker/Dockerfile.gpu -t steamlab-worker:cuda .
```

- `BACKEND_URL=http://backend:8000`: private backend origin. HTTP is intended only for
  the isolated Docker network. Redirects and environment HTTP proxies are disabled.
- `INTERNAL_TOKEN`: required, at least 32 characters; must match the backend.
- `MODEL_DIR=/models`: both images contain verified models and upstream licenses here.
- `INFERENCE_DEVICE=cpu|cuda`: CPU image defaults to `cpu`; GPU image defaults to `cuda`.

Entrypoint: `python -m worker.main`. No ports, model volume, database volume, or runtime
model download is needed. The CPU image uses Python 3.12; the GPU image uses Ubuntu 22.04 Python 3.10.
Both run as an unprivileged UID and include the distribution's `curl` package for
bounded backend HTTP transport. CUDA base/version pins and FFmpeg option detection
are unchanged.
The configured RTSP URL's authority is authenticated as `reader` with the URL-encoded
`INTERNAL_TOKEN`, as required by MediaMTX; backend configuration can provide a bare URL.

The optional GPU image is **`worker/Dockerfile.gpu`**, using
a digest-pinned `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`, CUDA 12.4.1 and
cuDNN 9.1 with `onnxruntime-gpu==1.22.0`. See [CUDA compatibility](../docs/CUDA.md)
for the verified base metadata and remaining host checks. Use a compatible NVIDIA
host driver and NVIDIA
Container Toolkit; grant a GPU through Compose device reservations or `--gpus all`.
Set `INFERENCE_DEVICE=cuda`. `NVIDIA_VISIBLE_DEVICES=all` and
`NVIDIA_DRIVER_CAPABILITIES=compute,utility` are image defaults. Do not install CPU
and GPU ONNX Runtime wheels into the same environment.

YuNet detection and `FaceRecognizerSF.alignCrop` always use OpenCV CPU. Only SFace
inference uses the selected ONNX Runtime provider. CUDA availability, actual session
provider, graph assignment without CPU fallback, and a warm-up inference are checked
before reporting `CUDAExecutionProvider`. An unavailable or broken requested provider
causes a generic error heartbeat and nonzero process exit, not silent CPU inference.

## Runtime Behavior

- The worker requires GET `/internal/worker/configs` returning `{streams: WorkerConfig[]}`
  with at most four active definitions. Each config includes `stream_id`, `enabled`,
  `session_id`, `catalog_version`, `rtsp_url`, `analysis_fps`, `detection_threshold`, and
  `pause_reason`. IDs are `stream` or `stream-` followed by 32 lowercase UUID hex digits.
  There is no fallback to the legacy singleton endpoint; deploy the backend first.
- Independent config polling (1-second wait) and heartbeat (2-second wait after each
  sweep) use 1.5-second wall-clock request deadlines; slow model initialization, inference, and
  observation submission cannot delay either control thread. Each heartbeat POST to
  `/internal/worker/heartbeat` includes `stream_id`, `state`, `provider`, and `error`.
- One shared `FaceEngine` initializes once, even when all streams are offline. A single
  inference thread fairly round-robins nonempty latest-frame slots and never calls the
  mutable OpenCV detector concurrently. The existing two-thread OpenCV/ONNX Runtime
  defaults and strict CUDA checks are unchanged. Capture FPS is a target, not a
  per-stream inference throughput guarantee; all streams share the same model capacity.
- Up to four independent FFmpeg decoders drain RTSP over TCP to BGR rawvideo,
  aspect-preserving letterboxed 640x360, at each stream's configured analysis rate
  (default 2 FPS, bounded to 0.2-10 FPS). Each uses exact frame reads and its own single
  overwriting latest-frame slot, never an inference backlog. A 10-second complete-frame
  timeout restarts only that decoder. The tested FFmpeg 4.4 `-stimeout` detection is
  unchanged; FFmpeg stderr is discarded to protect URLs.
- Disabled/offline definitions occupy registry slots but do not decode. Config changes
  invalidate only the affected stream's in-flight work and restart only its decoder.
  List reordering and display-name changes preserve surviving states and decoders.
  Removed/archived definitions stop capture and drop local data; even an unexpected
  re-add or config rollback cannot revive an invalidated generation.
- A failed or malformed config poll immediately invalidates all local configurations
  (including duplicate IDs or more than four definitions). Independently, five seconds
  without refresh expires each config. An identical later refresh cannot revive work
  captured before expiry. Capture resumes only with fresh local generations.
- Observation POSTs to `/internal/observations` include `stream_id` alongside the
  session, catalog version, capture timestamp, and faces. Batches are rechecked after
  inference and immediately before submission, and dropped when older than three seconds.
  Only one observation request may be in flight across the worker, with no queue or
  retries. Submission is synchronous in the inference thread: after the bounded
  request and child cleanup, round-robin advances to the next stream. A stalled request
  cannot leave a permanent busy submission thread suppressing other streams' results.
  A 409 or network failure invalidates
  only the submitting stream's matching generation, never newer work or another stream.
  The backend's atomic stream/session/version check closes the unavoidable race between
  the final local check and the HTTP request; already-sent requests cannot be recalled.
- HTTP uses a curl child with nonblocking stdin/stdout and one monotonic 1.5-second
  deadline covering DNS, connection/TLS, headers, request upload, response body, and
  process exit. Curl's own `--max-time` is additional protection, not the deadline's
  enforcement mechanism. The worker kills timed-out/oversized children and waits up to
  0.5 seconds to reap them before moving on. Response bodies are capped at 65,536 bytes
  (plus four framing bytes for HTTP status); JSON requests are capped at 3 MiB, matching
  the backend limit. Only bounded transport processes exist, with no background queue.
  Credentials, URL, and JSON travel through `curl --config -` stdin, never argv or logs.
  Curl startup config, URL globbing, redirects, ambient proxies, and retries are disabled;
  only HTTP(S) is allowed. Stderr and HTTP error response bodies are never reported.
- Each stream timestamps its latest delivery failure locally and reports heartbeat
  `state=error`, `error=Observation delivery failed` until a successful current-generation
  submission clears it. Ordinary capture status updates and config refreshes do not
  hide the error. Late results from invalidated generations cannot alter this state;
  shared model/provider fatal errors still take precedence.
- SIGINT/SIGTERM invalidate work and signal all decoders before waiting, allowing child
  processes to be reaped in parallel within one three-second join budget. Retiring
  decoders still count toward the four-decoder limit until they exit. Model and network
  threads are daemons with bounded joins, so hung native code cannot hold shutdown open.
  Fatal shared model/provider errors are reported for every known stream with bounded
  parallel best-effort final heartbeats, followed by nonzero process exit.
  Shutdown also closes the backend transport, kills/reaps its children in parallel,
  and prevents new requests from racing with exit.
- Low disk space disables capture through a stable paused configuration. The worker
  reports `Low disk space` and resumes only when the backend reports available space.
  Other pause reasons are reported as `Analysis paused`, not echoed from the config.
- Samples use UTC receive timestamps, not camera presentation timestamps. Each batch
  contains at most 20 faces, 128 finite unit-normalized embedding values per face, and
  a JPEG whose base64 representation is at most 100,000 bytes.
- Faces below the configured confidence, 32-pixel minimum visible size, or grayscale
  Laplacian variance 35 are rejected. Quality is confidence multiplied by capped size
  and sharpness factors in [0,1]; it is a heuristic, not an identity probability.

Heartbeat states are `starting`, `connecting`, `analyzing`, `disabled`, `paused`, `offline`, and
`error`; provider is `unavailable` until initialization, then the actual ONNX Runtime
provider name. Heartbeats contain only fixed generic error messages.

## Model Provenance

All assets are pinned to OpenCV Zoo commit
[`47534e27c9851bb1128ccc0102f1145e27f23f98`](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98).

| Model | SHA256 from upstream Git LFS pointer | Bytes |
| --- | --- | --- |
| YuNet `face_detection_yunet_2023mar.onnx` | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | 232589 |
| SFace `face_recognition_sface_2021dec.onnx` | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` | 38696353 |

Pointer sources:
[YuNet](https://raw.githubusercontent.com/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet/face_detection_yunet_2023mar.onnx),
[SFace](https://raw.githubusercontent.com/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface/face_recognition_sface_2021dec.onnx).
The downloader uses HTTPS only, rejects insecure redirects, bounds download size and
time, retries three times, and atomically renames only after checksum/size validation.
An LFS pointer or truncated download fails the build. It also downloads each model's
upstream `LICENSE` (YuNet MIT, SFace Apache-2.0) and the Zoo root `LICENSE` from that
same immutable commit into `/models`. There are no placeholder models or images.

The Zoo [SFace wrapper](https://github.com/opencv/opencv_zoo/blob/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface/sface.py)
calls OpenCV `FaceRecognizerSF.feature`. Its
[OpenCV 4.11 implementation](https://github.com/opencv/opencv/blob/4.11.0/modules/objdetect/src/face_recognize.cpp)
uses `blobFromImage(crop, 1, Size(112,112), Scalar(0,0,0), true, false)`.
Accordingly the worker takes BGR uint8 aligned crops, swaps to RGB, converts to float32
NCHW `[1,3,112,112]`, and retains raw 0-255 values. It does **not** divide by 255 or
subtract a mean, because normalization is inside the SFace graph.

## Tests

On Python 3.12 with glibc (or in a matching development container), from the repo root:

```sh
python -m pip install -r worker/requirements-test.txt
python -m pytest worker/tests -q
python -m worker.download_models --output /tmp/steamlab-models
STEAMLAB_TEST_MODEL_DIR=/tmp/steamlab-models python -m pytest worker/tests -q
```

The final command additionally compares real SFace ONNX Runtime output with OpenCV
`feature()` and smoke-tests YuNet. Control/capture/downloader tests need only pytest;
native inference tests explicitly skip if NumPy/OpenCV/ONNX Runtime are unavailable.
GPU execution still requires a real NVIDIA device and the GPU image.

Runtime tests cover four-stream fairness and shared initialization, per-stream config
and submission isolation, expiry/rollback/removal, list reorder, bounded submissions,
decoder limits, independent heartbeats, and parallel child-process shutdown while native
inference is blocked. Native engine stubs exercise the real runtime scheduling and
control loops; the shutdown test uses four real child processes, not CUDA or RTSP.
Transport tests use real Python children in place of curl to force blocked upload,
missing headers, trickling bodies, and hung process exit, and verify physical kill/reap
and next-stream delivery. They also cover credentials/config escaping, size caps,
HTTP status/redirect errors, sticky per-stream delivery errors, and transport shutdown.
These tests do not require curl installed on the test host; live HTTP integration does.

In the shared local development environment (without native inference dependencies):

```sh
PATH=/tmp/opencode/steamlab-integration:$PATH \
  /tmp/opencode/steamlab-backend-env/bin/python -m pytest worker/tests -q
```

The PATH prefix provides FFmpeg for the existing actual timeout-option probe test.
Before deployment, integrate with the new backend endpoints and four authenticated
MediaMTX publishers, verify scoped observations/heartbeats during session changes and
archival, and smoke-test both CPU and real CUDA images. These require the backend,
media server, native models, and (for CUDA) a compatible NVIDIA host.
