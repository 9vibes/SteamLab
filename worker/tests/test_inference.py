import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy", reason="Install worker/requirements-test.txt for native inference tests")
cv2 = pytest.importorskip("cv2", reason="OpenCV wheels require a supported glibc environment")
pytest.importorskip("onnxruntime", reason="ONNX Runtime requires a supported glibc environment")

from worker import inference


def test_preprocess_matches_opencv_raw_rgb_blob():
    crop = np.zeros((112, 112, 3), dtype=np.uint8)
    crop[:] = [17, 89, 255]
    tensor = inference.preprocess_sface(crop)
    expected = cv2.dnn.blobFromImage(crop, 1, (112, 112), (0, 0, 0), True, False)
    np.testing.assert_array_equal(tensor, expected)
    assert tensor.dtype == np.float32 and tensor.flags.c_contiguous
    np.testing.assert_array_equal(tensor[0, :, 0, 0], [255, 89, 17])


@pytest.mark.parametrize("value", [np.zeros(128), np.ones(127), np.full(128, np.nan), np.full(128, np.inf)])
def test_invalid_embeddings_rejected(value):
    with pytest.raises(ValueError):
        inference.normalize_embedding(value)


def test_embeddings_are_finite_unit_length():
    result = inference.normalize_embedding(np.arange(128, dtype=np.float32)[None])
    assert len(result) == 128
    assert np.isfinite(result).all()
    assert np.linalg.norm(result) == pytest.approx(1, abs=1e-7)


def test_cuda_missing_is_explicit_failure(monkeypatch):
    monkeypatch.setattr(inference.ort, "get_available_providers", lambda: ["CPUExecutionProvider"])
    with pytest.raises(RuntimeError, match="unavailable"):
        inference.create_session("unused.onnx", "cuda")


def test_cuda_library_load_fallback_is_rejected(monkeypatch):
    monkeypatch.setattr(inference.ort, "get_available_providers", lambda: ["CPUExecutionProvider", "CUDAExecutionProvider"])
    session = SimpleNamespace(disable_fallback=lambda: None, get_providers=lambda: ["CPUExecutionProvider"])
    monkeypatch.setattr(inference.ort, "InferenceSession", lambda *_, **__: session)
    with pytest.raises(RuntimeError, match="failed to initialize"):
        inference.create_session("unused.onnx", "cuda")


def engine_with_detections(detections):
    engine = inference.FaceEngine.__new__(inference.FaceEngine)
    engine.detector = SimpleNamespace(setInputSize=lambda _: None, setScoreThreshold=lambda _: None,
                                      detect=lambda _: (None, detections))
    engine.aligner = SimpleNamespace(alignCrop=lambda image, face: cv2.resize(image[:112, :112], (112, 112)))
    engine.session = SimpleNamespace(run=lambda *_: [np.ones((1, 128), np.float32)])
    engine.input_name = "data"
    return engine


def detection(*, confidence=0.95, size=80):
    return np.array([10, 10, size, size, 30, 30, 60, 30, 45, 50, 32, 70, 60, 70, confidence], dtype=np.float32)


def test_face_limit_quality_and_thumbnail_contract():
    image = np.random.default_rng(4).integers(0, 256, (360, 640, 3), dtype=np.uint8)
    engine = engine_with_detections(np.stack([detection()] * 30))
    faces = engine.analyze(image, 0.85)
    assert len(faces) == 20
    for face in faces:
        assert len(face["embedding"]) == 128
        assert np.linalg.norm(face["embedding"]) == pytest.approx(1)
        assert 0.85 <= face["confidence"] <= 1
        assert 0 < face["quality"] <= 1
        assert len(face["thumbnail"]) <= 100000
        jpeg = base64.b64decode(face["thumbnail"], validate=True)
        assert jpeg[:2] == b"\xff\xd8" and len(jpeg) <= 100000
        assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (112, 112, 3)


def test_small_low_confidence_blurry_and_stale_faces_filtered():
    image = np.random.default_rng(4).integers(0, 256, (360, 640, 3), dtype=np.uint8)
    engine = engine_with_detections(np.stack([detection(size=20), detection(confidence=0.5)]))
    assert engine.analyze(image, 0.85) == []
    engine = engine_with_detections(np.stack([detection()]))
    assert engine.analyze(np.zeros_like(image), 0.85) == []
    assert engine.analyze(image, 0.85, lambda: False) == []


def test_real_sface_ort_matches_opencv_feature():
    model_dir = os.environ.get("STEAMLAB_TEST_MODEL_DIR")
    if not model_dir:
        pytest.skip("Set STEAMLAB_TEST_MODEL_DIR to verified downloaded models for real-model parity")
    engine = inference.FaceEngine(Path(model_dir), "cpu")
    crop = np.random.default_rng(12).integers(0, 256, (112, 112, 3), dtype=np.uint8)
    expected = inference.normalize_embedding(engine.aligner.feature(crop))
    actual = inference.normalize_embedding(engine.session.run(None, {engine.input_name: inference.preprocess_sface(crop)})[0])
    assert np.dot(expected, actual) > 0.99999
    assert engine.provider == "CPUExecutionProvider"
    assert engine.analyze(np.zeros((360, 640, 3), np.uint8), 0.85) == []
