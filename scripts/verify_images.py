"""Verify released GHCR images using anonymous registry access only."""

import argparse
import hashlib
import json

import httpx

ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


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
            with client.stream("GET", target, headers=headers) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    checksum.update(chunk)
                    size += len(chunk)
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
