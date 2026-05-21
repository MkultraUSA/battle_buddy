#!/usr/bin/env python3
import base64
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.transcription import transcribe  # noqa: E402

BB_BASE_URL = os.environ.get("BB_BACKLOG_BASE_URL", "http://127.0.0.1:9001")
BB_TOKEN = os.environ.get("BB_BACKLOG_AGENT_TOKEN", "")
WORKER_ID = os.environ.get("BB_BACKLOG_WORKER_ID", socket.gethostname())
POLL_SECONDS = int(os.environ.get("BB_BACKLOG_POLL_SECONDS", "15"))
LEASE_SECONDS = int(os.environ.get("BB_BACKLOG_LEASE_SECONDS", "900"))


def api_request(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BB_BASE_URL}{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BB_TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def claim_one() -> dict | None:
    response = api_request(
        "/api/backlog/claim",
        {"worker_id": WORKER_ID, "lease_seconds": LEASE_SECONDS},
    )
    if response.get("status") != "ok":
        return None
    return response.get("item")


def complete_one(item_id: str, transcript: str) -> None:
    api_request(
        "/api/backlog/complete",
        {"item_id": item_id, "action": "complete", "transcript": transcript},
    )


def retry_one(item_id: str, retry_delay_seconds: int = 180) -> None:
    api_request(
        "/api/backlog/complete",
        {"item_id": item_id, "action": "retry", "retry_delay_seconds": retry_delay_seconds},
    )


def main() -> int:
    if not BB_TOKEN:
        print("missing BB_BACKLOG_AGENT_TOKEN", file=sys.stderr)
        return 2

    while True:
        try:
            item = claim_one()
            if not item:
                time.sleep(POLL_SECONDS)
                continue

            wav_bytes = base64.b64decode(item["audio_b64"])
            transcript, _accuracy = transcribe(wav_bytes)
            transcript = transcript.strip()
            if transcript:
                complete_one(item["id"], transcript)
                print(f"completed {item['id']} {item.get('tag', '')}", flush=True)
            else:
                retry_one(item["id"])
                print(f"retry {item['id']} {item.get('tag', '')}", flush=True)
        except urllib.error.HTTPError as exc:
            print(f"http error {exc.code}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)
        except Exception as exc:
            print(f"agent error: {exc}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
