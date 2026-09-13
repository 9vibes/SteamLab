"""Offline regression checks; actual CUDA execution needs a target GPU host."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04@sha256:2fcc4280646484290cc50dce5e65f388dd04352b07cbe89a635703bd1f9aedb6"


def test_gpu_base_is_pinned_to_cuda_124_cudnn9():
    dockerfile = (ROOT / "worker/Dockerfile.gpu").read_text()
    assert f"FROM {BASE}\n" in dockerfile
    assert "NVIDIA_REQUIRE_CUDA=" not in dockerfile
    assert "NVIDIA_DISABLE_REQUIRE" not in dockerfile
    assert "INFERENCE_DEVICE=cuda" in dockerfile
    assert "NVIDIA_DRIVER_CAPABILITIES=compute,utility" in dockerfile
    assert "USER 65532:65532" in dockerfile


def test_gpu_ort_version_is_preserved():
    requirements = (ROOT / "worker/requirements-gpu.txt").read_text().splitlines()
    assert "onnxruntime-gpu==1.22.0" in requirements
    assert not any(line.startswith("onnxruntime==") for line in requirements)
