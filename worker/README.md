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
Both run as an unprivileged UID.
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

- Independent config polling (1-second wait) and heartbeat (2-second wait) use
  1.5-second network timeouts; inference cannot delay either thread.
- FFmpeg drains RTSP over TCP to BGR rawvideo, aspect-preserving letterboxed 640x360,
  at the configured analysis rate (default 2 FPS, bounded to 0.2-10 FPS).
- Capture uses exact frame reads and a single overwriting latest-frame slot, never
  an inference backlog. A 10-second complete-frame timeout restarts FFmpeg.
- SIGINT/SIGTERM stop network loops and reap FFmpeg. Process exit also bounds shutdown
  when native inference cannot be cancelled. FFmpeg stderr is discarded to protect URLs.
- Config becomes unusable after five seconds without a successful refresh. Any config
  change invalidates in-flight work and restarts capture with a new local generation.
- Disabled/offline sessions do not decode. Batches are rechecked after inference and
  dropped when older than three seconds. Backend 409 invalidates local configuration;
  no failed batch is replayed. The backend's atomic version/session check closes the
  unavoidable race between the final local check and the HTTP request.
- Low disk space disables capture through a stable paused configuration. The worker
  reports the pause reason and resumes only when the backend reports available space.
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
