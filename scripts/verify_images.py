"""Verify released GHCR images using anonymous registry access only."""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx

ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
RANGE_BYTES = 1024 * 1024
RANGE_WORKERS = 16


def verify(client, image, checked, pull):
    if not image.startswith("ghcr.io/") or ":" not in image:
        raise ValueError("Use a versioned ghcr.io image reference")
    repository, tag = image.removeprefix("ghcr.io/").rsplit(":", 1)
    token = client.get("https://ghcr.io/token", params={
        "service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    token.raise_for_status()
    headers = {"Authorization": f"Bearer {token.json()['token']}", "Accept": ACCEPT}
    base = f"https://ghcr.io/v2/{repository}"

    def manifest(reference):
        response = client.get(f"{base}/manifests/{reference}", headers=headers)
        response.raise_for_status()
        digest = "sha256:" + hashlib.sha256(response.content).hexdigest()
        if response.headers.get("docker-content-digest", digest) != digest:
            raise ValueError("Registry manifest digest mismatch")
        if reference.startswith("sha256:") and reference != digest:
            raise ValueError("Referenced manifest digest mismatch")
        return response.json(), digest

    document, digest = manifest(tag)
    if "manifests" in document:
        platform = next(item for item in document["manifests"] if
                        item.get("platform", {}).get("os") == "linux" and
                        item.get("platform", {}).get("architecture") == "amd64")
        document, _ = manifest(platform["digest"])
    total = 0
    for blob in [document["config"], *document["layers"]]:
        target = f"{base}/blobs/{blob['digest']}"
        response = client.head(target, headers=headers)
        response.raise_for_status()
        if pull and blob["digest"] not in checked:
            checksum, size = hashlib.sha256(), 0
            def read_range(start):
                end = min(start + RANGE_BYTES, blob["size"]) - 1
                for attempt in range(3):
                    try:
                        with client.stream("GET", target, headers={**headers, "Range": f"bytes={start}-{end}"}, timeout=30) as response:
                            response.raise_for_status()
                            if response.status_code == 206:
                                if response.headers.get("content-range") != f"bytes {start}-{end}/{blob['size']}":
                                    raise ValueError("Registry returned an unexpected byte range")
                            elif response.status_code != 200 or start != 0 or blob["size"] > RANGE_BYTES:
                                raise ValueError("Registry did not honor the bounded byte range")
                            content = bytearray()
                            for chunk in response.iter_bytes(chunk_size=RANGE_BYTES):
                                content.extend(chunk)
                                if len(content) > end - start + 1:
                                    raise ValueError("Registry byte range exceeded expected size")
                            if len(content) != end - start + 1:
                                raise ValueError("Registry byte range was incomplete")
                            return bytes(content)
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise

            # Bounded waves avoid single-response proxy limits and keep memory
            # bounded even when the first request is slower than its siblings.
            with ThreadPoolExecutor(max_workers=RANGE_WORKERS) as pool:
                while size < blob["size"]:
                    starts = range(size, min(size + RANGE_BYTES * RANGE_WORKERS, blob["size"]), RANGE_BYTES)
                    for content in pool.map(read_range, starts):
                        checksum.update(content)
                        size += len(content)
            if "sha256:" + checksum.hexdigest() != blob["digest"] or size != blob["size"]:
                raise ValueError("Registry blob checksum or size mismatch")
            checked.add(blob["digest"])
            total += size
    return {"image": image, "digest": digest, "platform": "linux/amd64",
            "anonymous_access": True, "blobs_downloaded_and_verified": pull,
            "downloaded_bytes": total}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+")
    parser.add_argument("--pull", action="store_true", help="Download and checksum every config/layer without retaining files")
    args = parser.parse_args()
    with httpx.Client(timeout=120, trust_env=False, follow_redirects=True) as client:
        checked = set()
        for image in args.images:
            try:
                print(json.dumps(verify(client, image, checked, args.pull)), flush=True)
            except Exception as error:
                # HTTP exception representations can contain signed blob URLs.
                print(json.dumps({"image": image, "error": type(error).__name__, "anonymous_access": False}), flush=True)
                raise SystemExit(1) from None
