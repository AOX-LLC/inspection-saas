#!/usr/bin/env python3
"""Upload a batch of synthetic photos to a running stack and time how long they take.

Everything goes through the API: log in, upload each photo with a presigned POST,
complete it, then poll the project's progress until the worker has finished. The
worker is whatever the stack runs (`docker compose up -d --wait`).

    cd api && uv run python ../scripts/batch_demo.py [--count 50]

Uses only the demo seed and generated images, all synthetic. Exits 1 if any photo
fails or the batch does not finish in time.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
from app.seed.batch import BATCH_SIZE, BatchPhoto, batch_photos

API = os.environ.get("API", "http://127.0.0.1:4701")
ORIGIN = os.environ.get("ORIGIN", "http://127.0.0.1:4700")
ALPHA_ORG = "8e35f604-8a87-4fba-b70e-e87a8e22efd1"
# The published demo credentials (see the README); they protect nothing.
EMAIL, PASSWORD = "alpha.inspector@alpha.example", "synthetic-demo-password"


def upload(client: httpx.Client, project: str, photo: BatchPhoto) -> None:
    base = f"/orgs/{ALPHA_ORG}/projects/{project}/files"
    start = client.post(
        base,
        json={
            "filename": photo.filename,
            "content_type": "image/jpeg",
            "size_bytes": len(photo.data),
        },
    )
    start.raise_for_status()
    body = start.json()
    stored = httpx.post(
        body["upload"]["url"],
        data=body["upload"]["fields"],
        files={"file": ("upload", photo.data)},
        timeout=30,
    )
    stored.raise_for_status()
    client.post(f"{base}/{body['file_id']}/complete").raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=BATCH_SIZE)
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for the worker")
    args = parser.parse_args()

    started = time.monotonic()
    photos = list(batch_photos(args.count))
    generated = time.monotonic()
    print(f"generated {len(photos)} synthetic photos in {generated - started:.1f}s")

    with httpx.Client(base_url=API, headers={"Origin": ORIGIN}, timeout=30) as client:
        client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).raise_for_status()
        projects = client.get(f"/orgs/{ALPHA_ORG}/projects").json()["items"]
        project = projects[0]["id"]
        progress_url = f"/orgs/{ALPHA_ORG}/projects/{project}/photos/progress"
        before = client.get(progress_url).json()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda photo: upload(client, project, photo), photos))
        uploaded = time.monotonic()
        print(f"uploaded and completed {len(photos)} photos in {uploaded - generated:.1f}s")

        deadline = uploaded + args.timeout
        while True:
            now = client.get(progress_url).json()
            counts = {k: now["counts"][k] - before["counts"][k] for k in now["counts"]}
            if now["finished"] or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        finished = time.monotonic()

    print(f"progress: {counts}")
    print(f"worker finished the batch {finished - uploaded:.1f}s after the last upload completed")
    print(f"total, upload start to last tile: {finished - generated:.1f}s")
    ok = now["finished"] and counts["tiled"] == len(photos) and counts["failed"] == 0
    print("batch: ok" if ok else "batch: FAILED or timed out")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
