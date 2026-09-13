import asyncio
import shutil
import signal
import time
import uuid
from collections import deque
from datetime import datetime
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException

from .store import utcnow


class Bitrate:
    def __init__(self):
        self.previous = None
        self.history = deque(maxlen=60)

    def update(self, source, received, now):
        rate = 0.0
        if self.previous is not None:
            old_source, old_bytes, old_time = self.previous
            if source and source == old_source and received >= old_bytes and now > old_time:
                rate = (received - old_bytes) * 8 / (now - old_time) / 1_000_000
        self.previous = (source, received, now) if source else None
        self.history.append(round(rate, 3))
        return round(rate, 3)


class Media:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.client = httpx.AsyncClient(timeout=3, trust_env=False)
        self.lock = asyncio.Lock()
        self.available = False
        self.online = False
        self.source = None
        self.session_id = None
        self.started_at = None
        self.tracks = []
        self.meter = Bitrate()
        self.bitrate = 0
        self.warning = None
        self.recording = None
        self.process = None
        self.recording_source = None
        self.recordings_dir = config.data_dir / "recordings"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlsplit(config.rtsp_url)
        self.reader_url = parsed._replace(netloc=f"reader:{quote(config.internal_token, safe='')}@{parsed.netloc}").geturl()

    @property
    def min_free_bytes(self):
        return int(self.config.min_free_gb * 1024 ** 3)

    def disk_free(self):
        return shutil.disk_usage(self.recordings_dir).free

    async def read_path(self):
        response = await self.client.get(self.config.media_api + "/v3/paths/get/live/stream")
        if response.status_code == 404:
            return {"ready": False}
        response.raise_for_status()
        return response.json()

    async def refresh(self):
        async with self.lock:
            try:
                path = await self.read_path()
                self.available = True
            except (httpx.HTTPError, ValueError):
                path = {"ready": False}
                self.available = False
            online = bool(path.get("ready"))
            source = (path.get("source") or {}).get("id") if online else None
            # readyTime distinguishes publisher sessions even for sources without IDs.
            source = f"{source}:{path.get('readyTime')}" if online else None
            changed = source != self.source
            if changed:
                # Invalidate observations before recorder shutdown can yield to
                # requests or wait on a stalled FFmpeg process.
                self.online = False
                with self.store.db:
                    if self.session_id:
                        self.store.db.execute("UPDATE sessions SET ended_at=? WHERE id=?", (utcnow(), self.session_id))
                self.session_id, self.started_at = None, None
            if self.recording and (changed or not self.available):
                await self.stop_locked("Stream disconnected or changed", interrupted=True)
            if changed:
                with self.store.db:
                    self.session_id = str(uuid.uuid4()) if online else None
                    self.started_at = utcnow() if online else None
                    if online:
                        self.store.db.execute("INSERT INTO sessions VALUES (?, ?, NULL)", (self.session_id, self.started_at))
                self.source = source
            self.online, self.tracks = online, path.get("tracks") or []
            self.bitrate = self.meter.update(source, path.get("bytesReceived", 0), time.monotonic())
            if self.process and self.process.returncode is not None:
                await self.stop_locked("Recorder exited unexpectedly", interrupted=True)
            if self.disk_free() < self.min_free_bytes:
                if self.recording:
                    await self.stop_locked("Recording stopped: disk space below configured reserve", interrupted=True)
                self.warning = "Low disk space. Recording and face analysis are paused until space is freed."
            elif self.warning and self.warning.startswith("Low disk"):
                self.warning = None

    async def monitor(self):
        tick = 0
        while True:
            try:
                await self.refresh()
                if tick % 60 == 0:
                    self.store.prune()
                tick += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log reader credentials or stream URLs through exception text.
                self.warning = "Stream monitor encountered an error; retrying."
            await asyncio.sleep(1)

    async def start(self):
        await self.refresh()
        async with self.lock:
            if self.recording:
                raise HTTPException(409, "A recording is already running")
            if not self.available or not self.online:
                raise HTTPException(409, "Connect a stream before recording")
            if self.disk_free() < self.min_free_bytes:
                raise HTTPException(409, "Insufficient free disk space")
            if not any(track.startswith("H264") for track in self.tracks):
                raise HTTPException(409, "Recording requires H.264 video; set your encoder to H.264/AAC")
            recording_id, started = str(uuid.uuid4()), utcnow()
            target = self.recordings_dir / f"{recording_id}.mp4"
            try:
                self.process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", "-timeout", "3000000", "-i", self.reader_url,
                    "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                    "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                    "-flush_packets", "1", "-f", "mp4", "-n", str(target),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError:
                raise HTTPException(503, "FFmpeg could not start") from None
            self.recording = {"id": recording_id, "started_at": started}
            self.recording_source = self.source
            with self.store.db:
                self.store.db.execute("INSERT INTO recordings VALUES (?, ?, ?, NULL, 'recording', NULL)",
                                      (recording_id, self.session_id, started))
            self.warning = None
            return dict(self.recording)

    async def stop_locked(self, reason=None, interrupted=False):
        if not self.recording:
            return
        process, recording = self.process, self.recording
        if process and process.returncode is None:
            try:
                if process.stdin:
                    process.stdin.write(b"q\n")
                    await process.stdin.drain()
                    process.stdin.close()
                else:
                    process.send_signal(signal.SIGINT)
            except (ProcessLookupError, BrokenPipeError, ConnectionResetError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                reason, interrupted = "Recorder required forced shutdown", True
        target = self.recordings_dir / f"{recording['id']}.mp4"
        has_video = target.exists() and target.stat().st_size > 1024
        status = "interrupted" if interrupted else "ready"
        if not has_video:
            status, reason = "error", "No video was recorded; check the encoder and stream connection"
        elif process and process.returncode not in (0, 255) and not interrupted:
            status, reason = "interrupted", "Recorder did not finalize normally"
        with self.store.db:
            self.store.db.execute("UPDATE recordings SET ended_at=?, status=?, error=? WHERE id=?",
                                  (utcnow(), status, reason, recording["id"]))
        self.recording, self.process, self.recording_source = None, None, None
        if reason:
            self.warning = reason

    async def stop(self):
        async with self.lock:
            await self.stop_locked()

    async def close(self):
        async with self.lock:
            await self.stop_locked("Server shutting down", interrupted=True)
        await self.client.aclose()

    def recordings(self):
        result = []
        for row in self.store.db.execute("SELECT * FROM recordings ORDER BY started_at DESC"):
            item = dict(row)
            target = self.recordings_dir / f"{row['id']}.mp4"
            size = target.stat().st_size if target.exists() else 0
            playable = row["status"] in ("ready", "interrupted") and size > 1024
            result.append({**item, "size_bytes": size,
                           "duration_seconds": (datetime.fromisoformat(row["ended_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds() if row["ended_at"] else None,
                           "playback_url": f"/api/recordings/{row['id']}/file" if playable else None,
                           "download_url": f"/api/recordings/{row['id']}/file?download=1" if playable else None})
        return {"items": result}
