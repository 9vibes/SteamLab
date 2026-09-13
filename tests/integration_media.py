"""Opt-in native integration test; no Docker, mocks, or application changes.

Run with the backend dependencies installed:
  MEDIAMTX_BIN=/path/mediamtx FFMPEG_BIN=/path/ffmpeg FFPROBE_BIN=/path/ffprobe \
    python tests/integration_media.py

Requires unused localhost ports 8000, 1935, 8554, 8888, 9997. Binaries are
user-provided; this script does not download or install anything. Temporary
credentials are never printed. Artifacts are kept under /tmp/opencode/
steamlab-integration (override with INTEGRATION_ROOT). Exit 1 means a failed
check; exit 2 means prerequisites or startup failed.
"""

import json
import os
from pathlib import Path
import re
import secrets
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
    uvicorn.run(create_app(config), host="127.0.0.1", port=8000,
                access_log=False, log_level="error")


def main():
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
    processes, results = [], []
    client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False)
    anon = httpx.Client(base_url="http://127.0.0.1:8000", timeout=10, trust_env=False)

    def redact(text):
        for value in private:
            text = text.replace(value, "[REDACTED]")
        return re.sub(r"(?i)(pass=|reader:)[^&\s@]+", r"\1[REDACTED]", text)

    def launch(args):
        process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

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
            print(f"PASS {name}: {detail}", flush=True)
            return detail
        except Exception as exc:
            detail = redact(str(exc))
            results.append({"check": name, "passed": False, "detail": detail})
            print(f"FAIL {name}: {detail}", flush=True)

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def status():
        response = client.get("/api/status")
        response.raise_for_status()
        return response.json()

    def publish(url):
        return launch([binaries["FFMPEG"], "-hide_banner", "-loglevel", "error",
                       "-nostdin", "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                       "-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
                       "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                       "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac", "-b:a", "96k",
                       "-f", "flv", url])

    def rejected(process):
        try:
            code = process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            stop(process)
            raise AssertionError("media operation was not rejected within 12s")
        error = redact(process.stderr.read().decode(errors="replace"))
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

    def recording_item(key):
        return next(item for item in client.get("/api/recordings").json()["items"]
                    if item["id"] == key)

    def start_recording():
        response = client.post("/api/recordings/start")
        require(response.status_code == 200, f"start HTTP {response.status_code}: {response.text}")
        return response.json()["id"]

    def validate_recording(key, expected):
        item = recording_item(key)
        require(item["status"] == expected, f"status={item['status']}; error={item['error']}")
        require(item["duration_seconds"] > 0 and item["size_bytes"] > 1024,
                "missing duration or recording bytes")
        url = item["playback_url"]
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
        require(float(metadata["format"]["duration"]) > 0, "no playable duration")
        decode = subprocess.run([binaries["FFMPEG"], "-v", "error", "-i", str(target),
                                 "-f", "null", "-"], capture_output=True, timeout=20)
        require(decode.returncode == 0 and not decode.stderr,
                "decode failed: " + decode.stderr.decode(errors="replace"))
        return {"status": item["status"], "bytes": len(full.content), "codecs": codecs,
                "duration": metadata["format"]["duration"], "range": partial.status_code,
                "decoded": True}

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
            response = wait_for(lambda: (r if r.status_code == 200 else None)
                                if (r := client.get("/api/live/index.m3u8")) else None)
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
            count = len(client.get("/api/recordings").json()["items"])
            publish(url)
            wait_for(lambda: status()["online"])
            for _ in range(6):
                time.sleep(1)
                require(status()["recording"] is None, "recording auto-resumed")
            require(status()["session_id"] != first_session, "session did not change")
            require(len(client.get("/api/recordings").json()["items"]) == count,
                    "new recording created automatically")
            return "new stream session; no active/new recording for 6 seconds"
        check("reconnect does not auto-resume", no_resume)

        (work / "results.json").write_text(json.dumps(results, indent=2))
        passed = sum(result["passed"] for result in results)
        print(f"RESULT: {passed}/{len(results)} passed; artifacts: {work}", flush=True)
        return 0 if passed == len(results) else 1
    except Exception as exc:
        print("BLOCKED: " + redact(str(exc)), flush=True)
        return 2
    finally:
        for process in reversed(processes):
            stop(process)
            diagnostic = process.stderr.read().decode(errors="replace")
            if diagnostic.strip():
                print("PROCESS DIAGNOSTIC: " + redact(diagnostic)[-1500:], flush=True)
            process.stderr.close()
        client.close()
        anon.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        raise SystemExit(main())
