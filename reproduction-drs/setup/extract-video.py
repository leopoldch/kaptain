#!/usr/bin/env python3
"""Fetch the authors' input.mp4 from their image jolyonjian/apps:cpu-1.0 (amd64) on Docker Hub,
without ever running the image: download the layer that holds the file and read it with tarfile.

Usage: python3 -I setup/extract-video.py [reproduction-drs/input.mp4]
"""

import hashlib
import json
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPOSITORY = "jolyonjian/apps"
TAG = "cpu-1.0"
# The layer added on 2022-12-01 with input.mp4, output.* and run.sh (checked on 2026-10-07).
LAYER_PREFIX = "4b973b4d0475"
MEMBER = "tmp/workdir/input.mp4"
SHA256 = "079e25c4ef782df7f76bb0f0ef20631c7c5f5bbb433f3e78286a9e9ed95207c9"


def get(url, token, accept=None):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    if accept:
        request.add_header("Accept", accept)
    return urllib.request.urlopen(request, timeout=60)


def main(destination):
    token_url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPOSITORY}:pull"
    token = json.load(urllib.request.urlopen(token_url, timeout=30))["token"]
    manifest = json.load(get(f"https://registry-1.docker.io/v2/{REPOSITORY}/manifests/{TAG}", token,
                             "application/vnd.docker.distribution.manifest.v2+json"))
    digest = next(layer["digest"] for layer in manifest["layers"] if layer["digest"][7:19] == LAYER_PREFIX)

    with tempfile.TemporaryDirectory() as scratch:
        layer = Path(scratch) / "layer.tgz"
        with get(f"https://registry-1.docker.io/v2/{REPOSITORY}/blobs/{digest}", token) as response:
            layer.write_bytes(response.read())
        with tarfile.open(layer) as archive:
            data = archive.extractfile(MEMBER).read()

    found = hashlib.sha256(data).hexdigest()
    if found != SHA256:
        sys.exit(f"unexpected sha256: {found}")
    Path(destination).write_bytes(data)
    print(f"{destination}: {len(data)} bytes, sha256 {found} (as expected)")


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "input.mp4"
    main(sys.argv[1] if len(sys.argv) > 1 else default)
