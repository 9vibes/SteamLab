import hashlib
import json
import re

import httpx
import pytest

from scripts import verify_images


@pytest.mark.parametrize("failure", [None, "range", "checksum", "ignored", "retry"])
def test_bounded_anonymous_registry_download(monkeypatch, failure):
    monkeypatch.setattr(verify_images, "RANGE_BYTES", 4)
    monkeypatch.setattr(verify_images, "RANGE_WORKERS", 2)
    blobs = {}
    for content in (b"{}", b"a-complete-container-layer"):
        blobs["sha256:" + hashlib.sha256(content).hexdigest()] = content
    descriptors = [{"digest": key, "size": len(value)} for key, value in blobs.items()]
    manifest = json.dumps({"config": descriptors[0], "layers": descriptors[1:]}).encode()
    attempts = []

    def handler(request):
        if request.url.path == "/token":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"token": "anonymous-test-token"})
        assert request.headers["authorization"] == "Bearer anonymous-test-token"
        if "/manifests/" in request.url.path:
            return httpx.Response(200, content=manifest)
        content = blobs[request.url.path.rsplit("/", 1)[1]]
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"]).groups())
        assert end - start < 4
        if failure == "retry" and not attempts:
            attempts.append("retry")
            raise httpx.ReadError("temporary transfer failure")
        attempts.append((start, end))
        part = content[start:end + 1]
        if failure == "ignored":
            return httpx.Response(200, content=content)
        if failure == "checksum":
            part = b"x" * len(part)
        content_range = f"bytes {start}-{end}/{len(content)}" if failure != "range" else "bytes 1-2/999"
        return httpx.Response(206, headers={"Content-Range": content_range}, content=part)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        if failure in ("range", "checksum", "ignored"):
            with pytest.raises(ValueError):
                verify_images.verify(client, "ghcr.io/example/app:1.0.0", set(), True)
        else:
            checked = set()
            result = verify_images.verify(client, "ghcr.io/example/app:1.0.0", checked, True)
            assert result["anonymous_access"] and result["blobs_downloaded_and_verified"]
            assert result["downloaded_bytes"] == sum(map(len, blobs.values()))
            assert checked == set(blobs)
            again = verify_images.verify(client, "ghcr.io/example/app:1.0.0", checked, True)
            assert again["downloaded_bytes"] == 0
