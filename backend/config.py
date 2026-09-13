import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    admin_password: str
    internal_token: str
    public_host: str = "localhost"
    data_dir: Path = Path("/data")
    rtmp_port: int = 1935
    cookie_secure: bool = False
    min_free_gb: float = 2
    face_retention_days: int = 7
    max_faces: int = 2000
    match_threshold: float = 0.5
    detection_threshold: float = 0.85
    analysis_fps: float = 2
    media_api: str = "http://mediamtx:9997"
    hls_base: str = "http://mediamtx:8888/live/stream"
    rtsp_url: str = "rtsp://mediamtx:8554/live/stream"

    def __post_init__(self):
        if len(self.admin_password) < 16 or len(self.internal_token) < 32:
            raise ValueError("ADMIN_PASSWORD needs 16+ characters; INTERNAL_TOKEN needs 32+")
        if any(c in self.internal_token for c in "\r\n"):
            raise ValueError("INTERNAL_TOKEN cannot contain newlines")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", self.public_host):
            raise ValueError("PUBLIC_HOST must be a hostname or IPv4 address without a port")
        if not 1 <= self.rtmp_port <= 65535:
            raise ValueError("Invalid RTMP_PORT")
        if not 0.05 <= self.min_free_gb <= 1_000_000:
            raise ValueError("MIN_FREE_GB must be between 0.05 and 1000000")
        if not 1 <= self.face_retention_days <= 365 or not 1 <= self.max_faces <= 10000:
            raise ValueError("Invalid face retention or capacity")
        if not 0 < self.match_threshold <= 1 or not 0 < self.detection_threshold <= 1:
            raise ValueError("Confidence thresholds must be in (0, 1]")
        if not 0.2 <= self.analysis_fps <= 10:
            raise ValueError("ANALYSIS_FPS must be between 0.2 and 10")

    @classmethod
    def from_env(cls):
        return cls(
            admin_password=os.environ.get("ADMIN_PASSWORD", ""),
            internal_token=os.environ.get("INTERNAL_TOKEN", ""),
            public_host=os.environ.get("PUBLIC_HOST", "localhost"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            rtmp_port=int(os.environ.get("RTMP_PORT", "1935")),
            cookie_secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
            min_free_gb=float(os.environ.get("MIN_FREE_GB", "2")),
            face_retention_days=int(os.environ.get("FACE_RETENTION_DAYS", "7")),
            max_faces=int(os.environ.get("MAX_FACES", "2000")),
            match_threshold=float(os.environ.get("MATCH_THRESHOLD", "0.5")),
            detection_threshold=float(os.environ.get("DETECTION_THRESHOLD", "0.85")),
            analysis_fps=float(os.environ.get("ANALYSIS_FPS", "2")),
        )
