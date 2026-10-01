#!/usr/bin/env python3
"""Remote transcription worker for the Battle Buddy backlog.

Polls the server's /api/backlog/claim endpoint, transcribes locally, and reports
results back through /api/backlog/complete. The server owns the queue, the
lease and the durable copy; this process is stateless and replaceable.

Two things in this file were wrong against the server's current contract and are
worth reading before changing anything:

1. An empty transcript must be reported as a *completion*, not a retry. The
   server discards a clip whose transcript is empty. Calling the retry action
   instead releases the lease, so the identical clip is claimed again, forever
   -- a poison item that starves every clip behind it. The previous version did
   exactly that.

2. /complete must echo the call's metadata back. The server takes `tgid`, `tag`,
   `category`, `node` and `duration` from the request body, defaulting to tgid 0,
   tag "backlog" and no coordinates. Sending only {item_id, transcript} therefore
   filed every backlogged call as category "Unknown" at default downtown
   coordinates -- which is precisely the coordinate-honesty problem the map work
   is trying to fix, reintroduced through the back door.

Configuration (environment):
  BB_BACKLOG_BASE_URL      default http://127.0.0.1:9001
  BB_BACKLOG_AGENT_TOKEN   required; the server refuses everything without it
  BB_BACKLOG_WORKER_ID     default hostname
  BB_BACKLOG_POLL_SECONDS  default 15
  BB_BACKLOG_LEASE_SECONDS default 900
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.transcription import transcribe  # noqa: E402

BB_BASE_URL = os.environ.get("BB_BACKLOG_BASE_URL", "http://127.0.0.1:9001")
BB_TOKEN = os.environ.get("BB_BACKLOG_AGENT_TOKEN", "")
WORKER_ID = os.environ.get("BB_BACKLOG_WORKER_ID", socket.gethostname())
POLL_SECONDS = int(os.environ.get("BB_BACKLOG_POLL_SECONDS", "15"))
LEASE_SECONDS = int(os.environ.get("BB_BACKLOG_LEASE_SECONDS", "900"))

_running = True


def _stop(_signum, _frame):
    """Finish the clip in hand, then exit. The lease returns it if we die hard."""
    global _running
    _running = False


class AuthFailure(RuntimeError):
    """The server refused us. Retrying cannot help; a worker that spins on this
    looks exactly like a healthy idle worker, which is how a misconfigured token
    goes unnoticed for days."""


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
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        # 401 = wrong or missing token, 503 = the server has no secret
        # configured. Both need a human, not another poll.
        if exc.code in (401, 503):
            raise AuthFailure(f"{path} -> HTTP {exc.code} {detail}") from exc
        raise


def claim_one() -> dict | None:
    """Return a claimed item, or None when the queue is genuinely empty."""
    response = api_request(
        "/api/backlog/claim",
        {"worker_id": WORKER_ID, "lease_seconds": LEASE_SECONDS},
    )
    if response.get("status") != "ok":
        return None
    item = response.get("item")
    if not item:
        return None
    return item


def complete_one(item: dict, transcript: str, accuracy: float = 0.0) -> None:
    """Report a finished transcription.

    An empty transcript is a legitimate completion: the server discards the
    clip. Most backlogged audio is non-speech, which is why #162 stopped queueing
    LLM work for it -- retrying it would burn the same CPU forever.
    """
    payload = {
        "item_id": item.get("id", ""),
        "action": "complete",
        "transcript": transcript,
        "accuracy": accuracy,
        # Echo the queue's metadata so the stored call is not filed as Unknown
        # at default coordinates.
        "tgid": item.get("tgid", 0),
        "tag": item.get("tag", "backlog"),
        "category": item.get("category", ""),
        "node": item.get("node", WORKER_ID),
        "duration": item.get("duration", 0.0),
    }
    api_request("/api/backlog/complete", payload)


def report_failure(item: dict, reason: str) -> None:
    """Give the clip back so its lease expires and another attempt can take it.

    This is for *our* failures only (a transcription crash, an unreachable
    server). It deliberately re-queues even an empty transcript, because at that
    point we never learned anything about the audio -- unlike a successful run
    that found it to be silence.
    """
    try:
        api_request(
            "/api/backlog/complete",
            {"item_id": item.get("id", ""), "action": "retry", "reason": reason},
        )
    except Exception as exc:  # best effort; the lease is the real safety net
        print(f"could not return {item.get('id')}: {exc}", file=sys.stderr, flush=True)


def handle_one(item: dict) -> None:
    wav_bytes = base64.b64decode(item["audio_b64"])
    started = time.time()
    transcript, accuracy = transcribe(wav_bytes)
    elapsed = time.time() - started
    transcript = (transcript or "").strip()

    if elapsed > LEASE_SECONDS:
        print(
            f"WARNING {item.get('id')} took {elapsed:.0f}s, longer than the "
            f"{LEASE_SECONDS}s lease; it may be claimed twice",
            file=sys.stderr, flush=True,
        )

    complete_one(item, transcript, float(accuracy or 0.0))
    if transcript:
        print(
            f"completed {item.get('id')} {item.get('tag', '')} "
            f"({len(transcript)} chars, {elapsed:.1f}s)",
            flush=True,
        )
    else:
        # Server-side this discards the clip. Log it rather than retrying, so a
        # silent feed is visible instead of looking like a stuck worker.
        print(
            f"empty {item.get('id')} {item.get('tag', '')} -- discarded "
            f"(non-speech, {elapsed:.1f}s)",
            flush=True,
        )


def main() -> int:
    if not BB_TOKEN:
        print("missing BB_BACKLOG_AGENT_TOKEN", file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(
        f"backlog worker {WORKER_ID} -> {BB_BASE_URL} "
        f"(poll {POLL_SECONDS}s, lease {LEASE_SECONDS}s)",
        flush=True,
    )

    idle = 0
    while _running:
        try:
            item = claim_one()
        except AuthFailure as exc:
            print(f"fatal: {exc}", file=sys.stderr, flush=True)
            return 3
        except Exception as exc:
            print(f"claim failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)
            continue

        if not item:
            idle += 1
            if idle % 20 == 1:
                print(f"idle ({idle * POLL_SECONDS}s)", flush=True)
            time.sleep(POLL_SECONDS)
            continue

        idle = 0
        try:
            handle_one(item)
        except Exception as exc:
            print(f"transcribe failed for {item.get('id')}: {exc}",
                  file=sys.stderr, flush=True)
            report_failure(item, str(exc)[:200])
            time.sleep(1)

    print("stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())