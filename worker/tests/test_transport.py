"""Exercise curl against a real local HTTP server, without face-model dependencies."""

import json
import shutil
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from worker.runtime import Backend, HTTP_TIMEOUT

pytestmark = pytest.mark.skipif(not shutil.which("curl"), reason="Install curl for native worker transport tests")


def test_native_transport_auth_payload_status_and_deadline():
    token = "private-test-token-" + "x" * 32
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer " + token
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.path, json.loads(body) if body else None))
            if self.path == "/slow":
                time.sleep(HTTP_TIMEOUT + 1)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                return
            content = json.dumps({"received": json.loads(body) if body else None}).encode()
            self.send_response(409 if self.path == "/conflict" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = Backend(f"http://127.0.0.1:{server.server_port}", token)
    try:
        payload = {"stream_id": "stream", "label": 'quote" slash\\ newline\n unicode\u2603'}
        assert backend.request("/echo", payload) == {"received": payload}
        for path, code in (("/conflict", 409), ("/redirect", 302)):
            with pytest.raises(urllib.error.HTTPError) as error:
                backend.request(path, payload)
            assert error.value.code == code
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            backend.request("/slow")
        assert time.monotonic() - started < HTTP_TIMEOUT + 1
        assert backend.request("/next-stream", {"stream_id": "stream-" + "a" * 32})["received"]["stream_id"].endswith("a" * 32)
        assert not backend._processes
        assert all(path != "/must-not-follow" for path, _ in seen)
    finally:
        backend.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
