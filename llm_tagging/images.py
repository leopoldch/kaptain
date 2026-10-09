#!/usr/bin/env python3
"""Read image metadata from the registry, like a scheduler could before any pull.

Nothing is downloaded except the small manifest and config JSON. Writes images.json.
"""

import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def split_ref(ref):
    # "busybox@sha256:..." -> ("registry-1.docker.io", "library/busybox", "sha256:...")
    name, digest = ref.split("@")
    first = name.split("/")[0]
    if "." in first or ":" in first:
        host, repo = first, name[len(first) + 1:]
    else:
        host, repo = "registry-1.docker.io", name if "/" in name else "library/" + name
    return host, repo, digest


def get(url, token=None, accept=ACCEPT):
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def anonymous_token(host, repo):
    # The registry answers 401 with the address of its token service.
    try:
        urlopen(f"https://{host}/v2/", timeout=30)
        return None
    except HTTPError as error:
        header = error.headers.get("WWW-Authenticate", "")
    fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
    url = f"{fields['realm']}?service={fields['service']}&scope=repository:{repo}:pull"
    return get(url, accept="application/json")["token"]


def collect(ref):
    host, repo, digest = split_ref(ref)
    base = f"https://{host}/v2/{repo}"
    token = anonymous_token(host, repo)
    manifest = get(f"{base}/manifests/{digest}", token)
    if "manifests" in manifest:  # multi-platform index: take linux/amd64
        entry = next(m for m in manifest["manifests"]
                     if m["platform"]["os"] == "linux" and m["platform"]["architecture"] == "amd64")
        manifest = get(f"{base}/manifests/{entry['digest']}", token)
    config = get(f"{base}/blobs/{manifest['config']['digest']}", token, accept="*/*")
    image = config.get("config", {})
    history = [h.get("created_by", "") for h in config.get("history", [])][-20:]
    return {
        "architecture": config.get("architecture"),
        "os": config.get("os"),
        "compressed_size_bytes": sum(layer["size"] for layer in manifest["layers"]),
        "layer_count": len(manifest["layers"]),
        "entrypoint": image.get("Entrypoint"),
        "cmd": image.get("Cmd"),
        "env": image.get("Env") or [],
        "exposed_ports": sorted(image.get("ExposedPorts") or {}),
        "working_dir": image.get("WorkingDir") or None,
        "user": image.get("User") or None,
        "history": [line[:200] for line in history],
    }


def main():
    cases = json.loads((HERE / "cases.json").read_text())
    refs = sorted({c["image"] for case in cases for c in case["pod"]["spec"]["containers"]})
    images = {}
    for ref in refs:
        start = time.perf_counter()
        metadata = collect(ref)
        seconds = time.perf_counter() - start
        images[ref] = {"metadata": metadata, "collection_ms": round(seconds * 1000, 1)}
        print(f"{seconds * 1000:7.1f} ms  {ref}")
    (HERE / "images.json").write_text(json.dumps(images, indent=2) + "\n")


if __name__ == "__main__":
    main()
