"""Opt-in native integration test; no Docker, mocks, or application changes.

Run with the backend dependencies installed:
  MEDIAMTX_BIN=/path/mediamtx FFMPEG_BIN=/path/ffmpeg FFPROBE_BIN=/path/ffprobe \
    python tests/integration_media.py

Requires curl on PATH and unused localhost ports 8000, 1935, 8554, 8888, 9997. Binaries are
user-provided; this script does not download or install anything. Temporary
credentials are never printed. Artifacts are kept under /tmp/opencode/
steamlab-integration (override with INTEGRATION_ROOT). Exit 1 means a failed
check; exit 2 means prerequisites or startup failed. Preserves the original 13
checks and adds four-stream coverage. The suite has a 230-second deadline plus
bounded cleanup, suitable for a 300-second caller timeout. Native inference is
not exercised; worker coverage uses real RTSP raw-frame capture only.
"""

import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def serve():
    import uvicorn
    from backend.app import create_app
    from backend.config import Config

    config = Config(
        admin_password=os.environ["INTEGRATION_PASSWORD"],
        internal_token=os.environ["INTEGRATION_TOKEN"],
        public_host="127.0.0.1",
        data_dir=Path(os.environ["INTEGRATION_DATA"]),
        min_free_gb=0.05,
        media_api="http://localhost:9997",
        hls_base="http://localhost:8888/live/stream",
        # Static glibc FFmpeg cannot resolve localhost on some musl hosts.
        rtsp_url="rtsp://127.0.0.1:8554/live/stream",
    )
    app = create_app(config)
    auth_failures = set()

    @app.middleware("http")
    async def auth_diagnostic(request, call_next):
        body = await request.json() if request.url.path == "/internal/media/auth" else None
        response = await call_next(request)
        if body and body.get("protocol") == "hls" and response.status_code != 204:
            # Observe the real callback without logging credentials or modifying it.
            detail = {"protocol": "hls", "id_type": type(body.get("id")).__name__,
                      "path": body.get("path"), "status": response.status_code}
            diagnostic = json.dumps(detail, sort_keys=True)
            if diagnostic not in auth_failures:
                auth_failures.add(diagnostic)
                print("MEDIA AUTH DIAGNOSTIC: " + diagnostic, flush=True)
        return response

    uvicorn.run(app, host="127.0.0.1", port=8000,
                access_log=False, log_level="error")


def main():
    if not shutil.which("curl"):
        print("BLOCKED: install curl or add it to PATH for the worker HTTP transport")
        return 2
    binaries = {}
    for name in ("MEDIAMTX", "FFMPEG", "FFPROBE"):
        path = Path(os.environ.get(name + "_BIN", "/nonexistent")).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            print(f"BLOCKED: set {name}_BIN to an executable native binary")
            return 2
        binaries[name] = str(path)
    for port in (8000, 1935, 8554, 8888, 9997):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                print(f"BLOCKED: localhost port {port} is occupied")
                return 2
    root = Path(os.environ.get("INTEGRATION_ROOT", "/tmp/opencode/steamlab-integration"))
    if not root.is_dir():
        print("BLOCKED: create INTEGRATION_ROOT before running")
        return 2
    work = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    (work / "bin").mkdir()
    (work / "bin" / "ffmpeg").symlink_to(binaries["FFMPEG"])
    password, token = secrets.token_hex(24), secrets.token_hex(32)
    private = [password, token]
    env = {**os.environ, "INTEGRATION_PASSWORD": password,
           "INTEGRATION_TOKEN": token, "INTEGRATION_DATA": str(work / "data"),
           "PATH": str(work / "bin") + os.pathsep + os.environ.get("PATH", "")}
    config = (REPO / "infra/mediamtx.yml").read_text().replace(
        "http://backend:8000/", "http://127.0.0.1:8000/")
    for port in (9997, 8554, 1935, 8888):
        config = config.replace(f"Address: :{port}", f"Address: 127.0.0.1:{port}")
    (work / "mediamtx.yml").write_text(config)
    processes, results, logs, shutdowns = [], [], {}, []
    started = time.monotonic()
    client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False)
    anon = httpx.Client(base_url="http://127.0.0.1:8000", timeout=10, trust_env=False)

    def redact(text):
        for value in private:
            text = text.replace(value, "[REDACTED]")
        return re.sub(r"(?i)(pass=|reader:)[^&\s@]+", r"\1[REDACTED]", text)

    def launch(args, interactive=False):
        # Drain diagnostics to an unnamed file: an unread PIPE can stall MediaMTX.
        log = tempfile.TemporaryFile()
        process = subprocess.Popen(args, env=env,
                                   stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        processes.append(process)
        logs[process] = log
        return process

    def stop(process):
        before = time.monotonic()
        method = "already exited"
        if process.poll() is None:
            method = "stdin q" if process.stdin else "SIGINT"
            if process.stdin:
                try:
                    process.stdin.write(b"q\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            else:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                method += "; group SIGTERM"
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    method += "; group SIGKILL"
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=1)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        shutdowns.append({"process": processes.index(process), "method": method,
                          "seconds": round(time.monotonic() - before, 3),
                          "exit": process.returncode})

    def wait_for(fn, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = fn()
                if value:
                    return value
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last = type(exc).__name__
            time.sleep(0.4)
        raise AssertionError(f"condition timed out after {timeout}s; last error={last}")

    def check(name, fn):
        try:
            detail = fn()
            results.append({"check": name, "passed": True, "detail": detail})
            print(redact(f"PASS {name}: {detail}"), flush=True)
            return detail
        except TimeoutError:
            raise
        except Exception as exc:
            detail = redact(str(exc))
            results.append({"check": name, "passed": False, "detail": detail})
            print(f"FAIL {name}: {detail}", flush=True)

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def status(stream_id="stream"):
        response = client.get("/api/status", params={"stream_id": stream_id})
        response.raise_for_status()
        return response.json()

    def publish(url, width=320):
        return launch([binaries["FFMPEG"], "-hide_banner", "-loglevel", "error",
                       "-filter_threads", "1", "-re", "-f", "lavfi", "-i",
                       f"testsrc=size={width}x240:rate=25",
                       "-re", "-f", "lavfi", "-i",
                       f"sine=frequency={1000 + (width - 320) * 10}:sample_rate=44100",
                       "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                       "-threads", "1",
                       "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac", "-b:a", "96k",
                       "-f", "flv", url], interactive=True)

    def rejected(process):
        try:
            code = process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            stop(process)
            raise AssertionError("media operation was not rejected within 12s")
        logs[process].seek(0)
        error = redact(logs[process].read().decode(errors="replace"))
        require(code != 0, "media operation unexpectedly succeeded")
        require(bool(error.strip()), "no rejection diagnostic")
        # RTMP commonly closes the connection without an explicit auth message.
        # Successful publishing/reading controls below rule out transport failure.
        require(bool(re.search(r"server error|401|unauthorized|authentication|already publishing|"
                               r"connection reset by peer|broken pipe|input/output error",
                               error, re.IGNORECASE)),
                "not an explicit server rejection: " + error)
        require(not re.search(r"resolve hostname|connection refused|option .*not found", error,
                              re.IGNORECASE), "local prerequisite failure: " + error)
        return f"exit={code}; {error.strip()[-700:]}"

    def hls_master(playlist_url, stream_id="stream"):
        last = None
        def ready():
            nonlocal last
            last = client.get(playlist_url)
            if last.status_code == 503:
                direct = anon.get(f"http://127.0.0.1:8888/live/{stream_id}/index.m3u8",
                                  auth=httpx.BasicAuth("reader", token))
                require(direct.status_code != 401, "valid HLS reader credential rejected upstream")
            return last if last.status_code == 200 else None
        try:
            return wait_for(ready)
        except AssertionError:
            diagnostics = {"backend": {"status": last.status_code, "body": last.text}
                           if last is not None else {"error": "no response"}}
            for host in ("localhost", "127.0.0.1"):
                try:
                    direct = anon.get(f"http://{host}:8888/live/{stream_id}/index.m3u8",
                                      auth=httpx.BasicAuth("reader", token))
                    diagnostics[host] = {"status": direct.status_code, "body": direct.text}
                except httpx.HTTPError as exc:
                    diagnostics[host] = {"error": type(exc).__name__}
            (work / f"{stream_id}-hls-failure.json").write_text(redact(json.dumps(diagnostics, indent=2)))
            raise AssertionError("HLS master unavailable: " + redact(json.dumps(diagnostics))) from None

    def recording_item(key, stream_id="stream"):
        return next(item for item in client.get("/api/recordings", params={"stream_id": stream_id}).json()["items"]
                    if item["id"] == key)

    def start_recording(stream_id="stream"):
        response = client.post("/api/recordings/start", params={"stream_id": stream_id})
        require(response.status_code == 200, f"start HTTP {response.status_code}: {response.text}")
        return response.json()["id"]

    def validate_recording(key, expected, stream_id="stream", width=320):
        item = recording_item(key, stream_id)
        require(item["stream_id"] == stream_id, "recording ownership leaked")
        require(item["status"] == expected, f"status={item['status']}; error={item['error']}")
        require(item["duration_seconds"] > 0 and item["size_bytes"] > 1024,
                "missing duration or recording bytes")
        url = item["playback_url"]
        require(parse_qs(urlsplit(url).query).get("stream_id") == [stream_id],
                "playback URL lost ownership")
        require(anon.get(url).status_code == 401, "anonymous recording was not denied")
        full = client.get(url)
        require(full.status_code == 200, f"file HTTP {full.status_code}")
        target = work / f"{key}.mp4"
        target.write_bytes(full.content)
        partial = client.get(url, headers={"Range": "bytes=0-1023"})
        require(partial.status_code == 206 and partial.content == full.content[:1024]
                and partial.headers.get("content-range") == f"bytes 0-1023/{len(full.content)}",
                f"invalid Range response HTTP {partial.status_code}")
        download = client.get(item["download_url"])
        require("attachment" in download.headers.get("content-disposition", ""),
                "download not an attachment")
        probe = subprocess.run([binaries["FFPROBE"], "-v", "error", "-show_streams",
                                "-show_format", "-of", "json", str(target)],
                               capture_output=True, timeout=15)
        require(probe.returncode == 0, probe.stderr.decode(errors="replace"))
        metadata = json.loads(probe.stdout)
        codecs = [stream["codec_name"] for stream in metadata["streams"]]
        require("h264" in codecs and "aac" in codecs, f"unexpected codecs {codecs}")
        video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
        require((video["width"], video["height"]) == (width, 240),
                f"recording contains another feed: expected {width}x240")
        require(float(metadata["format"]["duration"]) > 0, "no playable duration")
        # Preserve RTSP clock precision: rounding to nominal FPS at the null
        # output can create duplicate DTS even when every input frame decodes.
        decode = subprocess.run([binaries["FFMPEG"], "-v", "error", "-i", str(target),
                                 "-fps_mode", "passthrough", "-enc_time_base", "demux",
                                  "-f", "null", "-"], capture_output=True, timeout=20)
        require(decode.returncode == 0 and not decode.stderr,
                "decode failed: " + decode.stderr.decode(errors="replace"))
        return {"status": item["status"], "bytes": len(full.content), "codecs": codecs,
                "duration": metadata["format"]["duration"], "range": partial.status_code,
                 "decoded": True, "stream_id": stream_id, "width": width, "id": key}

    def time_limit(signum, frame):
        raise TimeoutError("230-second suite deadline exceeded")

    previous_alarm = signal.signal(signal.SIGALRM, time_limit)
    signal.alarm(230)
    try:
        for name, binary in binaries.items():
            version = subprocess.run([binary, "--version" if name == "MEDIAMTX" else "-version"],
                                     capture_output=True, timeout=10)
            require(version.returncode == 0, f"{name} cannot execute")
            print(name + ": " + version.stdout.decode().splitlines()[0], flush=True)
        launch([sys.executable, str(Path(__file__).resolve()), "--serve"])
        wait_for(lambda: anon.get("/health").status_code == 200)
        launch([binaries["MEDIAMTX"], str(work / "mediamtx.yml")])
        login = client.post("/api/auth/login", json={"password": password})
        login.raise_for_status()
        csrf = login.json()["csrf_token"]
        private.extend([csrf, client.cookies.get("steamlab_session")])
        client.headers["X-CSRF-Token"] = csrf
        settings = client.get("/api/settings").json()
        key = parse_qs(urlsplit("rtmp://localhost/" + settings["stream_key"]).query)["pass"][0]
        private.append(key)
        url = settings["rtmp_url"] + "/" + settings["stream_key"]
        wait_for(lambda: status()["media_available"])

        def wrong_key():
            detail = rejected(publish(url.replace(key, "deliberately-invalid")))
            require(not status()["online"], "wrong key made stream online")
            return detail
        check("wrong publishing key rejected", wrong_key)

        def key_rotation():
            nonlocal url, key
            response = client.post("/api/stream/key")
            require(response.status_code == 200, f"rotation HTTP {response.status_code}")
            old_url = url
            settings = response.json()
            key = parse_qs(urlsplit("rtmp://localhost/" + settings["stream_key"]).query)["pass"][0]
            private.append(key)
            url = settings["rtmp_url"] + "/" + settings["stream_key"]
            rejected(publish(old_url))
            require(not status()["online"], "revoked key made stream online")
            return "rotation succeeded; revoked key rejected"
        check("offline key rotation revokes previous key", key_rotation)
        publisher = publish(url)
        wait_for(lambda: status()["online"])
        first_session = status()["session_id"]

        def second_publisher():
            detail = rejected(publish(url))
            require(publisher.poll() is None and status()["session_id"] == first_session,
                    "original publisher was replaced")
            return detail
        check("second publisher denied", second_publisher)

        def bitrate():
            current = wait_for(lambda: (s if s["bitrate_mbps"] > 0 else None)
                               if (s := status()) else None)
            return {"mbps": current["bitrate_mbps"], "tracks": current["tracks"]}
        check("positive bitrate", bitrate)

        def worker_capture():
            from worker.capture import Decoder, FRAME_BYTES, LatestFrame

            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = env["PATH"]
            slot = LatestFrame()
            decoder = Decoder(f"rtsp://reader:{token}@127.0.0.1:8554/live/stream", 2, 1, slot)
            try:
                decoder.start()
                frame = wait_for(slot.take, timeout=20)
                require(len(frame.data) == FRAME_BYTES, "incomplete sampled frame")
                return f"real RTSP sampled frame: {len(frame.data)} bytes, latest-frame decoder"
            finally:
                decoder.close()
                os.environ["PATH"] = original_path
                require(not decoder.is_alive(), "decoder did not shut down")
        check("worker RTSP frame sampling and shutdown", worker_capture)

        def hls():
            require(anon.get("/api/live/index.m3u8").status_code == 401,
                    "anonymous backend HLS was not denied")
            response = hls_master("/api/live/index.m3u8")
            playlist_url = "/api/live/index.m3u8"
            for _ in range(3):
                require(response.text.startswith("#EXTM3U"), "invalid HLS playlist")
                refs = [line for line in response.text.splitlines() if line and not line.startswith("#")]
                require(bool(refs), "empty HLS playlist")
                playlist = next((ref for ref in refs if ".m3u8" in ref), None)
                if playlist:
                    playlist_url = urljoin(playlist_url, playlist)
                    response = client.get(playlist_url)
                    require(response.status_code == 200, f"variant HTTP {response.status_code}")
                    continue
                refs.extend(re.findall(r'URI="([^"]+)"', response.text))
                for ref in refs:
                    media_url = urljoin(playlist_url, ref)
                    media = client.get(media_url)
                    require(media.status_code == 200 and len(media.content) > 0,
                            f"HLS asset HTTP {media.status_code}")
                    require(anon.get(media_url).status_code == 401, "anonymous HLS asset accessible")
                return f"master, variant and {len(refs)} assets HTTP 200; anonymous HTTP 401"
            raise AssertionError("no HLS media playlist")
        check("authenticated HLS and anonymous backend denial", hls)

        def anonymous_hls():
            response = anon.get("http://127.0.0.1:8888/live/stream/index.m3u8")
            require(response.status_code == 401, f"HTTP {response.status_code}")
            return "HTTP 401"
        check("anonymous MediaMTX HLS denied", anonymous_hls)
        def authenticated_rtmp():
            response = subprocess.run([
                binaries["FFMPEG"], "-v", "error", "-i",
                f"rtmp://127.0.0.1:1935/live/stream?user=reader&pass={token}",
                "-t", "1", "-f", "null", "-"], capture_output=True, timeout=15)
            require(response.returncode == 0, response.stderr.decode(errors="replace"))
            return "authenticated RTMP read and decode succeeded"
        check("authenticated RTMP read control", authenticated_rtmp)
        for protocol, media_url in (("RTSP", "rtsp://127.0.0.1:8554/live/stream"),
                                    ("RTMP", "rtmp://127.0.0.1:1935/live/stream")):
            def anonymous_read(protocol=protocol, media_url=media_url):
                options = ["-rtsp_transport", "tcp"] if protocol == "RTSP" else []
                return rejected(launch([binaries["FFMPEG"], "-v", "error", *options,
                                        "-i", media_url, "-t", "1", "-f", "null", "-"]))
            check(f"anonymous MediaMTX {protocol} denied", anonymous_read)

        def manual_recording():
            recording = start_recording()
            time.sleep(9)
            response = client.post("/api/recordings/stop")
            require(response.status_code == 204, f"stop HTTP {response.status_code}")
            return validate_recording(recording, "ready")
        check("manual recording, ffprobe, decode, metadata and Range", manual_recording)

        def interruption():
            recording = start_recording()
            time.sleep(9)
            require(status()["recording"] is not None,
                    f"recorder exited before disconnect: {recording_item(recording)}")
            stop(publisher)
            wait_for(lambda: not status()["online"] and status()["recording"] is None)
            return validate_recording(recording, "interrupted")
        check("disconnect marks real recording interrupted", interruption)
        stop(publisher)
        wait_for(lambda: not status()["online"])

        def no_resume():
            nonlocal publisher
            count = len(client.get("/api/recordings").json()["items"])
            publisher = publish(url)
            wait_for(lambda: status()["online"])
            for _ in range(6):
                time.sleep(1)
                require(status()["recording"] is None, "recording auto-resumed")
            require(status()["session_id"] != first_session, "session did not change")
            require(len(client.get("/api/recordings").json()["items"]) == count,
                    "new recording created automatically")
            return "new stream session; no active/new recording for 6 seconds"
        check("reconnect does not auto-resume", no_resume)

        feeds = {"stream": {"name": "Stream 1", "url": url, "key": key,
                            "width": 320, "publisher": publisher}}

        def four_publishers():
            base = settings["rtmp_url"]
            for index in range(1, 4):
                name = f"Native feed {index + 1}"
                response = client.post("/api/streams", json={"name": name})
                require(response.status_code == 201, f"create HTTP {response.status_code}")
                row = response.json()
                stream_id = row["id"]
                require(re.fullmatch(r"stream-[a-f0-9]{32}", stream_id) and row["name"] == name,
                        "invalid generated ID or display name")
                response = client.get("/api/settings", params={"stream_id": stream_id})
                response.raise_for_status()
                scoped = response.json()
                secret = parse_qs(urlsplit("rtmp://localhost/" + scoped["stream_key"]).query)["pass"][0]
                private.append(secret)
                require(scoped["rtmp_url"] == base and scoped["stream_key"].split("?")[0] == stream_id,
                        "publishing base changed or key lost stream path")
                feed_url = base + "/" + scoped["stream_key"]
                feeds[stream_id] = {"name": name, "url": feed_url, "key": secret,
                                    "width": 320 + index * 32}
                # Test a valid sibling credential on an offline path, not merely
                # duplicate-publisher rejection on an already occupied path.
                rejected(publish(feed_url.replace(secret, key), 320 + index * 32))
                require(not status(stream_id)["online"], "sibling key admitted on new path")
                feeds[stream_id]["publisher"] = publish(feed_url, feeds[stream_id]["width"])
            require(len({feed["key"] for feed in feeds.values()}) == 4, "keys are not distinct")
            wait_for(lambda: all(status(sid)["online"] for sid in feeds))
            listing = client.get("/api/streams").json()
            require(listing["active_count"] == listing["max_streams"] == 4,
                    "registry count/cap mismatch")
            require({row["id"] for row in listing["items"]} == set(feeds), "registry IDs mismatch")
            require(all("stream_key" not in row for row in listing["items"]), "registry exposes keys")
            response = client.post("/api/streams", json={"name": "Rejected fifth feed"})
            require(response.status_code == 409, f"fifth stream HTTP {response.status_code}")
            return "four named publishers on one RTMP base; three sibling keys denied; fifth HTTP 409"
        check("four publishers, scoped keys and capacity", four_publishers)

        def four_statuses():
            require(len(feeds) == 4, "four-stream setup incomplete")
            current = wait_for(lambda: (rows if all(row["online"] and row["bitrate_mbps"] > 0
                               for row in rows) else None) if (rows := [status(sid) for sid in feeds]) else None)
            for row in current:
                sid = row["stream_id"]
                require(row["stream_name"] == feeds[sid]["name"] and row["media_available"]
                        and row["session_id"] and row["tracks"] and not row["archived"],
                        "incomplete per-stream status")
                require(feeds[sid]["publisher"].poll() is None, "publisher exited")
                feeds[sid]["session"] = row["session_id"]
                sessions = client.get("/api/sessions", params={"stream_id": sid}).json()["items"]
                require(all(item["stream_id"] == sid for item in sessions)
                        and any(item["id"] == row["session_id"] for item in sessions),
                        "session history ownership mismatch")
            require(len({row["session_id"] for row in current}) == 4, "sessions shared across feeds")
            return [{"stream_id": row["stream_id"], "session": row["session_id"],
                     "mbps": row["bitrate_mbps"]} for row in current]
        check("four independent positive bitrates and sessions", four_statuses)

        def four_hls():
            evidence, failures = [], []
            for sid, feed in feeds.items():
                prefix = f"/api/streams/{sid}/live/"
                playlist_url = prefix + "index.m3u8"
                require(anon.get(playlist_url).status_code == 401, "anonymous scoped HLS accessible")
                try:
                    master = hls_master(playlist_url, sid)
                except AssertionError as exc:
                    failures.append(f"{sid}: {exc}")
                    continue
                require(master.text.startswith("#EXTM3U") and "#EXT-X-STREAM-INF" in master.text
                        and f"RESOLUTION={feed['width']}x240" in master.text,
                        "master playlist missing or routed to wrong feed")
                (work / f"{sid}-master.m3u8").write_text(master.text)
                variants = [line for line in master.text.splitlines() if line and not line.startswith("#")]
                assets = 0
                for ref in variants:
                    require(re.fullmatch(r"[A-Za-z0-9_-]+\.m3u8", ref), "unsafe/nonrelative HLS variant")
                    variant_url = urljoin(playlist_url, ref)
                    require(variant_url.startswith(prefix), "variant escaped stream directory")
                    variant = client.get(variant_url)
                    require(variant.status_code == 200 and variant.text.startswith("#EXTM3U"),
                            f"scoped variant HTTP {variant.status_code}")
                    require(anon.get(variant_url).status_code == 401, "anonymous variant accessible")
                    (work / f"{sid}-{ref}").write_text(variant.text)
                    refs = [line for line in variant.text.splitlines() if line and not line.startswith("#")]
                    refs.extend(re.findall(r'URI="([^"]+)"', variant.text))
                    require(refs and "#EXT-X-MAP" in variant.text, "missing HLS segments/init")
                    for asset in set(refs):
                        require(re.fullmatch(r"[A-Za-z0-9_-]+\.(mp4|m4s|ts)", asset),
                                "unsafe/nonrelative HLS asset")
                        asset_url = urljoin(variant_url, asset)
                        require(asset_url.startswith(prefix), "asset escaped stream directory")
                        media = client.get(asset_url)
                        require(media.status_code == 200 and media.content, f"asset HTTP {media.status_code}")
                        require(anon.get(asset_url).status_code == 401, "anonymous scoped asset accessible")
                        assets += 1
                require(variants and assets, "no variants or assets")
                evidence.append({"stream_id": sid, "variants": len(variants), "assets": assets})
            require(not failures, "; ".join(failures))
            return evidence
        check("four directory-scoped HLS masters variants and safe assets", four_hls)

        def four_worker_frames():
            from worker.capture import Decoder, FRAME_BYTES
            from worker.runtime import Backend, Config, Registry

            require(anon.get("/internal/worker/configs").status_code == 401,
                    "anonymous worker configs accessible")
            backend = Backend("http://127.0.0.1:8000", token)
            configs = [Config.parse(item) for item in backend.request("/internal/worker/configs")["streams"]]
            require(len(configs) == 4 and {cfg.stream_id for cfg in configs} == set(feeds),
                    "worker configs do not contain exactly four active streams")
            registry = Registry()
            registry.update_configs(configs)
            original_path = os.environ.get("PATH", "")
            os.environ["PATH"] = env["PATH"]
            decoders, frames = [], {}
            try:
                for state in registry.snapshot():
                    cfg, generation = state.snapshot()
                    require(cfg.session_id == feeds[cfg.stream_id]["session"]
                            and cfg.rtsp_url == f"rtsp://127.0.0.1:8554/live/{cfg.stream_id}",
                            "worker config lost session or localhost path prefix")
                    decoder = Decoder(cfg.stream_url(token), cfg.analysis_fps, generation, state.slot)
                    decoder.start()
                    decoders.append(decoder)
                def collect():
                    registry.update_configs(configs)
                    for state in registry.snapshot():
                        frame = state.slot.take()
                        if frame:
                            require(len(frame.data) == FRAME_BYTES and frame.generation == state.generation,
                                    "incomplete frame or wrong stream generation")
                            frames[state.stream_id] = frame
                    return len(frames) == 4
                wait_for(collect, timeout=20)
                require(all(decoder.is_alive() and not decoder.failed for decoder in decoders),
                        "four decoders were not running concurrently")
                require(len({hashlib.sha256(frame.data).hexdigest() for frame in frames.values()}) == 4,
                        "raw frame slots contain identical feeds")
                registry.update_configs(list(reversed(configs)))
                require(all(state.generation == frames[state.stream_id].generation for state in registry.snapshot()),
                        "registry reorder invalidated surviving stream generations")
                return "four API Config.parse entries, four registry slots and concurrent 691200-byte frames; no inference"
            finally:
                for decoder in decoders:
                    decoder.stop_event.set()
                for decoder in decoders:
                    decoder.close()
                os.environ["PATH"] = original_path
                require(all(not decoder.is_alive() for decoder in decoders), "four-decoder shutdown failed")
        check("four worker configs and concurrent native raw frame slots", four_worker_frames)

        def survivors(recording=False):
            for sid, feed in list(feeds.items())[:3]:
                current = status(sid)
                require(feed["publisher"].poll() is None and current["online"]
                        and current["session_id"] == feed["session"] and current["bitrate_mbps"] > 0,
                        f"sibling publisher/session disrupted: {sid}")
                if recording:
                    require(current["recording"] and current["recording"]["id"] == feed["recording"],
                            f"sibling recording stopped: {sid}")

        isolated = list(feeds)[-1]

        def simultaneous_recordings():
            for sid, feed in feeds.items():
                feed["recording"] = start_recording(sid)
            time.sleep(9)
            for sid, feed in feeds.items():
                require(status(sid)["recording"]["id"] == feed["recording"],
                        "not all four recordings remained active")
                item = recording_item(feed["recording"], sid)
                require(item["status"] == "recording" and item["size_bytes"] > 1024
                        and item["session_id"] == feed["session"], "recorder missing bytes/session")
            response = client.post("/api/recordings/stop", params={"stream_id": isolated})
            require(response.status_code == 204, f"isolated stop HTTP {response.status_code}")
            survivors(recording=True)
            feed = feeds[isolated]
            evidence = validate_recording(feed["recording"], "ready", isolated, feed["width"])
            feed["interrupted_recording"] = start_recording(isolated)
            time.sleep(9)
            stop(feed["publisher"])
            wait_for(lambda: not status(isolated)["online"] and status(isolated)["recording"] is None)
            survivors(recording=True)
            interrupted = validate_recording(feed["interrupted_recording"], "interrupted", isolated, feed["width"])
            return {"manual": evidence, "disconnect": interrupted, "other_recorders_active": 3}
        check("four simultaneous recorders and isolated stop disconnect", simultaneous_recordings)

        def rotated_offline():
            feed = feeds[isolated]
            old_url = feed["url"]
            response = client.post("/api/stream/key", params={"stream_id": isolated})
            require(response.status_code == 200, f"scoped rotation HTTP {response.status_code}")
            scoped = response.json()
            new_key = parse_qs(urlsplit("rtmp://localhost/" + scoped["stream_key"]).query)["pass"][0]
            private.append(new_key)
            require(new_key != feed["key"], "key did not rotate")
            feed["url"] = scoped["rtmp_url"] + "/" + scoped["stream_key"]
            feed["key"] = new_key
            rejected(publish(old_url, feed["width"]))
            survivors(recording=True)
            feed["publisher"] = publish(feed["url"], feed["width"])
            wait_for(lambda: status(isolated)["online"])
            require(status(isolated)["session_id"] != feed["session"]
                    and status(isolated)["recording"] is None, "rotated reconnect reused session/recording")
            survivors(recording=True)
            stop(feed["publisher"])
            wait_for(lambda: not status(isolated)["online"])
            return "revoked key rejected; new key publishes; other three sessions and recorders unchanged"
        check("offline scoped rotation leaves three publishers recording", rotated_offline)

        def recording_isolation():
            evidence = []
            for sid, feed in feeds.items():
                if sid != isolated:
                    response = client.post("/api/recordings/stop", params={"stream_id": sid})
                    require(response.status_code == 204, f"recording stop HTTP {response.status_code}")
                evidence.append(validate_recording(feed["recording"], "ready", sid, feed["width"]))
                items = client.get("/api/recordings", params={"stream_id": sid}).json()["items"]
                require(all(item["stream_id"] == sid for item in items), "recording list leaked another feed")
                for other in set(feeds) - {sid}:
                    require(client.get(f"/api/recordings/{feed['recording']}/file",
                                       params={"stream_id": other}).status_code == 404,
                            "wrong-owner recording playback accepted")
            survivors()
            return evidence
        check("four playable recordings distinct content and no ownership leakage", recording_isolation)

        def archive_history():
            feed = feeds[isolated]
            params = {"stream_id": isolated}
            histories = {kind: client.get(f"/api/{kind}", params=params).json()
                         for kind in ("sessions", "recordings", "faces")}
            require(histories["sessions"]["items"] and histories["recordings"]["items"],
                    "archive test needs existing history")
            response = client.delete(f"/api/streams/{isolated}")
            require(response.status_code == 204, f"offline archive HTTP {response.status_code}")
            for kind, history in histories.items():
                require(client.get(f"/api/{kind}", params=params).json() == history,
                        f"archive changed {kind} history")
            require(status(isolated)["archived"] and not status(isolated)["online"],
                    "archived status incorrect")
            require(client.get("/api/settings", params=params).json()["stream_key"] == "",
                    "archived key still exposed")
            rejected(publish(feed["url"], feed["width"]))
            survivors()
            listing = client.get("/api/streams").json()
            require(listing["active_count"] == 3
                    and any(row["id"] == isolated and row["archived_at"] for row in listing["items"]),
                    "archive did not free one slot and retain registry history")
            from worker.runtime import Backend, Config
            backend = Backend("http://127.0.0.1:8000", token)
            configs = [Config.parse(item) for item in backend.request("/internal/worker/configs")["streams"]]
            require({cfg.stream_id for cfg in configs} == set(feeds) - {isolated},
                    "archived feed still in worker configs")
            response = client.post("/api/streams", json={"name": "Replacement feed"})
            require(response.status_code == 201 and response.json()["id"] not in feeds,
                    "freed slot cannot be reused or archived ID was reused")
            require(client.post("/api/streams", json={"name": "Overflow again"}).status_code == 409,
                    "replacement did not restore four-stream cap")
            rejected(publish(feed["url"], feed["width"]))
            survivors()
            playback = validate_recording(feed["recording"], "ready", isolated, feed["width"])
            return {"history_preserved": list(histories), "archived_playback": playback,
                    "replacement_id": response.json()["id"], "surviving_publishers": 3}
        check("offline archive preserves history frees slot and denies old key", archive_history)

        (work / "results.json").write_text(json.dumps(results, indent=2))
        passed = sum(result["passed"] for result in results)
        print(f"RESULT: {passed}/{len(results)} passed; artifacts: {work}", flush=True)
        return 0 if passed == len(results) else 1
    except Exception as exc:
        print("BLOCKED: " + redact(str(exc)), flush=True)
        return 2
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        for process in reversed(processes):
            stop(process)
            logs[process].seek(0)
            diagnostic = redact(logs[process].read().decode(errors="replace"))
            (work / f"process-{processes.index(process):02d}.log").write_text(diagnostic)
            if diagnostic.strip():
                print("PROCESS DIAGNOSTIC: " + diagnostic[-1500:], flush=True)
            logs[process].close()
        (work / "results.json").write_text(redact(json.dumps(results, indent=2)))
        (work / "runtime.json").write_text(json.dumps({"seconds": round(time.monotonic() - started, 3),
            "suite_deadline_seconds": 230, "native_inference_tested": False, "shutdowns": shutdowns}, indent=2))
        print(f"RUNTIME: {time.monotonic() - started:.1f}s including cleanup; artifacts: {work}", flush=True)
        client.close()
        anon.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        raise SystemExit(main())
