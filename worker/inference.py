"""YuNet is always OpenCV CPU; SFace inference is exclusively ONNX Runtime."""

import base64
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from .download_models import MODELS

MAX_FACES = 20
MAX_JPEG_BYTES = 75000  # Also keeps the base64 representation <= 100,000 bytes.
MIN_FACE_PIXELS = 32
MIN_BLUR_VARIANCE = 35.0


def preprocess_sface(aligned_bgr):
    if aligned_bgr.shape != (112, 112, 3) or aligned_bgr.dtype != np.uint8:
        raise ValueError("SFace requires a 112x112 BGR uint8 crop")
    # Matches OpenCV FaceRecognizerSF::feature: scale=1, mean=0, swapRB=true.
    # SFace's ONNX graph contains its own input normalization.
    return np.ascontiguousarray(aligned_bgr[:, :, ::-1].transpose(2, 0, 1)[None],
                               dtype=np.float32)


def normalize_embedding(value):
    vector = np.asarray(value, dtype=np.float64).reshape(-1)
    if vector.size != 128 or not np.isfinite(vector).all():
        raise ValueError("Invalid SFace output")
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("Invalid SFace output norm")
    return (vector / norm).tolist()


def create_session(model, device):
    if device not in ("cpu", "cuda"):
        raise ValueError("INFERENCE_DEVICE must be cpu or cuda")
    provider = "CUDAExecutionProvider" if device == "cuda" else "CPUExecutionProvider"
    if provider not in ort.get_available_providers():
        raise RuntimeError("Requested inference provider unavailable")
    options = ort.SessionOptions()
    options.log_severity_level = 4
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    if device == "cuda":
        # Fail on CPU graph assignment, including when CUDA libraries cannot be loaded.
        options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    session = ort.InferenceSession(str(model), sess_options=options, providers=[provider])
    session.disable_fallback()
    if session.get_providers()[0] != provider:
        raise RuntimeError("Requested inference provider failed to initialize")
    inputs = session.get_inputs()
    if len(inputs) != 1 or inputs[0].shape != [1, 3, 112, 112] or inputs[0].type != "tensor(float)":
        raise RuntimeError("Unexpected SFace input contract")
    # Force lazy CUDA initialization now, rather than reporting a merely registered EP.
    normalize_embedding(session.run(None, {inputs[0].name: np.zeros((1, 3, 112, 112), np.float32)})[0])
    return session, inputs[0].name, provider


class FaceEngine:
    def __init__(self, model_dir, device):
        cv2.setNumThreads(2)
        cv2.setLogLevel(0)
        model_dir = Path(model_dir)
        detector_path, recognizer_path = (model_dir / item[1] for item in MODELS)
        if not detector_path.is_file() or not recognizer_path.is_file():
            raise RuntimeError("Required face models are missing")
        self.detector = cv2.FaceDetectorYN.create(
            str(detector_path), "", (640, 360), 0.85, 0.3, 500,
            cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        self.aligner = cv2.FaceRecognizerSF.create(
            str(recognizer_path), "", cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        self.session, self.input_name, self.provider = create_session(recognizer_path, device)

    def analyze(self, frame, threshold, is_current=lambda: True):
        self.detector.setInputSize((frame.shape[1], frame.shape[0]))
        self.detector.setScoreThreshold(threshold)
        _, detections = self.detector.detect(frame)
        if detections is None:
            return []
        faces = []
        for face in sorted(detections, key=lambda row: float(row[-1]), reverse=True):
            if len(faces) >= MAX_FACES or not is_current():
                break
            if len(face) != 15 or not np.isfinite(face).all():
                continue
            confidence = float(face[-1])
            x, y, width, height = map(float, face[:4])
            if not threshold <= confidence <= 1 or min(width, height) < MIN_FACE_PIXELS:
                continue
            left, top = max(0, int(x)), max(0, int(y))
            right, bottom = min(frame.shape[1], int(x + width)), min(frame.shape[0], int(y + height))
            if min(right - left, bottom - top) < MIN_FACE_PIXELS:
                continue
            crop = frame[top:bottom, left:right]
            blur = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            if not np.isfinite(blur) or blur < MIN_BLUR_VARIANCE:
                continue
            aligned = self.aligner.alignCrop(frame, face)
            result = self.session.run(None, {self.input_name: preprocess_sface(aligned)})[0]
            embedding = normalize_embedding(result)
            ok, jpeg = cv2.imencode(".jpg", aligned, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok or jpeg.nbytes > MAX_JPEG_BYTES:
                continue
            quality = confidence * min(1.0, min(width, height) / 112) * min(1.0, blur / 150)
            faces.append({"embedding": embedding, "thumbnail": base64.b64encode(jpeg).decode("ascii"),
                          "confidence": confidence, "quality": float(quality)})
        return faces
