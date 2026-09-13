# CUDA worker packaging

## 1.0.1: lower the container prestart requirement

The previous CUDA 12.6.3 image failed before the worker started with
`nvidia-container-cli: requirement error: unsatisfied condition: cuda>=12.6`.
The GPU worker now uses NVIDIA's CUDA **12.4.1 / cuDNN 9.1 / Ubuntu 22.04**
runtime, pinned to an immutable image index. ONNX Runtime GPU stays at **1.22.0**.
Ubuntu 22.04 supplies Python 3.10; CPU/backend images remain Python 3.12.
CI tests both Python versions, including real CPU model parity. This is not a
CPU fallback for the GPU worker.

[ONNX Runtime's CUDA provider documentation](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)
documents CUDA 12.x minor-version compatibility and requires cuDNN 9.x for
this ORT family. [ORT 1.22 release notes](https://github.com/microsoft/onnxruntime/releases/tag/v1.22.0)
confirm CUDA 12.x GPU packages. This permits selecting an older CUDA 12.x base;
it does not prove every model or driver works with that base.

[NVIDIA minor-version compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
has feature and PTX limitations. A CUDA version advertised by a driver is not a
measurement of successful inference. Do not override `NVIDIA_REQUIRE_CUDA`, set
`NVIDIA_DISABLE_REQUIRE`, or modify the host driver to bypass a startup error.

## Anonymous registry evidence

The Docker Hub manifests and referenced image config were fetched anonymously;
config bytes were SHA256 checked against the manifest descriptor:

- Tag: `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`
- Index: `sha256:2fcc4280646484290cc50dce5e65f388dd04352b07cbe89a635703bd1f9aedb6`
- Linux/amd64 manifest: `sha256:0bb88834d973ca1b450fcc2a05333c6fe45510bee289912a5391274c351c4a4d`
- `CUDA_VERSION=12.4.1`
- `NV_CUDNN_VERSION=9.1.0.70-1`
- `NV_CUDNN_PACKAGE_NAME=libcudnn9-cuda-12`
- `NVIDIA_REQUIRE_CUDA` begins `cuda>=12.4`, down from `cuda>=12.6`.
  NVIDIA's full expression also contains driver/brand alternatives; the inherited
  expression is deliberately left unchanged, not replaced with the first clause.

The source tag metadata is available through the Docker Registry v2 endpoint
`https://registry-1.docker.io/v2/nvidia/cuda/manifests/12.4.1-cudnn-runtime-ubuntu22.04`
with an anonymous pull token from `https://auth.docker.io/token` (service
`registry.docker.io`, scope `repository:nvidia/cuda:pull`). Follow the amd64
manifest descriptor and then its config blob to inspect the actual environment.

## Still required on the target host

The reported dual RTX 3090 hardware does not establish the installed driver.
No host-driver compatibility or GPU inference is claimed from these packaging
checks. After the released image is installed, verify container startup and
an actual SFace inference on that host. `worker/inference.py` retains strict
CUDA graph assignment, disabled CPU fallback, provider verification, and a
real warm-up inference before reporting `CUDAExecutionProvider`.

A CI image build and an anonymous image pull are necessary release checks, not
substitutes for that final GPU test. If 12.4 prestart still fails, obtain the
actual host driver and runtime logs before selecting another base.
