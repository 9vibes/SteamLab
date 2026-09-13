"""Drain FFmpeg continuously; retain only one complete, newly captured frame."""

import os
import selectors
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

WIDTH, HEIGHT = 640, 360
FRAME_BYTES = WIDTH * HEIGHT * 3
FRAME_TIMEOUT = 10.0


@dataclass(frozen=True)
class Frame:
    data: bytes
    generation: int
    captured_at: str
    received_at: float


class LatestFrame:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None

    def put(self, frame):
        with self._lock:
            self._frame = frame

    def take(self):
        with self._lock:
            frame, self._frame = self._frame, None
            return frame

    def clear(self):
        self.take()


@lru_cache(maxsize=1)
def rtsp_timeout_option():
    # Probe without a stream URL: never log credentials. FFmpeg 4.4's -timeout
    # enables RTSP listen mode; its client socket option is named -stimeout.
    result = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=rtsp"],
                            capture_output=True, text=True, check=True, timeout=5)
    options = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    if "-stimeout" in options:
        return "-stimeout"
    if "-timeout" in options:
        return "-timeout"
    raise RuntimeError("FFmpeg RTSP socket timeout option unavailable")


def ffmpeg_command(url, fps):
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "quiet",
            "-rtsp_transport", "tcp", rtsp_timeout_option(), "10000000",
            "-fflags", "nobuffer", "-flags", "low_delay", "-threads", "1",
            "-i", url, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", (f"fps={fps:g},scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2"),
            "-threads", "1", "-filter_threads", "1", "-pix_fmt", "bgr24",
            "-f", "rawvideo", "pipe:1"]


class Decoder(threading.Thread):
    def __init__(self, url, fps, generation, slot):
        super().__init__(name="rtsp-decoder", daemon=True)
        self._url, self._fps = url, fps
        self.generation, self.slot = generation, slot
        self.stop_event = threading.Event()
        self.last_frame = 0.0
        self.failed = False

    def close(self):
        self.stop_event.set()
        self.join(timeout=3)

    def run(self):
        while not self.stop_event.is_set():
            process = None
            try:
                # Neither the command nor FFmpeg stderr may be logged: URL has credentials.
                process = subprocess.Popen(ffmpeg_command(self._url, self._fps),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, bufsize=0)
                os.set_blocking(process.stdout.fileno(), False)
                pending = bytearray()
                last_complete = time.monotonic()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while not self.stop_event.is_set():
                        if time.monotonic() - last_complete >= FRAME_TIMEOUT:
                            raise TimeoutError("Stream frame timeout")
                        if not selector.select(timeout=0.2):
                            continue
                        chunk = os.read(process.stdout.fileno(), FRAME_BYTES - len(pending))
                        if not chunk:
                            raise EOFError("Stream ended")
                        pending.extend(chunk)
                        if len(pending) == FRAME_BYTES:
                            last_complete = time.monotonic()
                            self.slot.put(Frame(bytes(pending), self.generation,
                                                datetime.now(timezone.utc).isoformat(), last_complete))
                            pending.clear()
                            self.last_frame = last_complete
                            self.failed = False
            except Exception:
                self.failed = True
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                    if process.stdout is not None:
                        process.stdout.close()
            self.stop_event.wait(0.5)
