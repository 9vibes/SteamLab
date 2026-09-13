"""Download immutable OpenCV Zoo assets; never accept an LFS pointer as a model."""

import argparse
import hashlib
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

COMMIT = "47534e27c9851bb1128ccc0102f1145e27f23f98"
RAW = f"https://raw.githubusercontent.com/opencv/opencv_zoo/{COMMIT}"
MEDIA = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{COMMIT}"
# SHA256 and exact lengths are the git-lfs oids/sizes at COMMIT, not inferred hashes.
MODELS = (
    ("face_detection_yunet", "face_detection_yunet_2023mar.onnx",
     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", 232589),
    ("face_recognition_sface", "face_recognition_sface_2021dec.onnx",
     "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79", 38696353),
)


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("Insecure model download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, target, *, sha256=None, size=None, max_bytes=65536, attempts=3):
    """Bounded retries, bytes and wall time, with same-filesystem atomic publication."""
    if urlsplit(url).scheme != "https":
        raise ValueError("Model downloads require HTTPS")
    target = Path(target)
    opener = urllib.request.build_opener(HTTPSOnlyRedirect())
    for attempt in range(attempts):
        temporary = None
        try:
            digest = hashlib.sha256()
            count = 0
            deadline = time.monotonic() + 180
            request = urllib.request.Request(url, headers={"User-Agent": "SteamLab-model-builder"})
            with opener.open(request, timeout=15) as response:
                if urlsplit(response.geturl()).scheme != "https":
                    raise ValueError("Insecure model download response")
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
                    temporary = Path(out.name)
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError("Model download deadline exceeded")
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > max_bytes:
                            raise ValueError("Model download exceeds size limit")
                        digest.update(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
            if not count or (size is not None and count != size):
                raise ValueError("Model download length mismatch")
            if sha256 is not None and digest.hexdigest() != sha256:
                raise ValueError("Model download checksum mismatch")
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
            return
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if attempt + 1 == attempts:
                raise RuntimeError(f"Verified asset download failed: {target.name}") from None
            time.sleep(2 ** attempt)
    raise ValueError("At least one download attempt is required")


def download_all(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for folder, name, digest, size in MODELS:
        download(f"{MEDIA}/models/{folder}/{name}", directory / name,
                 sha256=digest, size=size, max_bytes=size)
        download(f"{RAW}/models/{folder}/LICENSE", directory / f"{folder}.LICENSE")
    download(f"{RAW}/LICENSE", directory / "opencv_zoo.LICENSE")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/models")
    download_all(parser.parse_args().output)
