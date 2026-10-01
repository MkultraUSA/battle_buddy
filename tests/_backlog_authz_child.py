"""Scenario runner for tests/test_backlog_authz.py.

Not a test file. Executed as a subprocess by that suite so each scenario runs in
a clean interpreter, for the same reason as _receive_auth_child.py: importing
audio_receiver pulls in faster_whisper via modules.transcription, which other
suites stub in sys.modules at collection time and never remove, so a stub would
mean the route under test was not the real one.

This harness exists because the AST tests in test_backlog_authz.py verified the
shape of the wiring and let three real defects through to production:

  1. `if checked is None: return checked` -- inverted, since the guard returns
     None on SUCCESS. Unauthenticated callers had their refusal ignored and got
     200; valid callers got 500.
  2. The guard ended in `return data`, so Flask jsonified the parsed request
     body and echoed the caller's token back in the response.
  3. A later assertion checked `Return.value is None`, but `return None`
     parses to Constant(None), so it failed against correct code.

All three were found by probing the deployed endpoints, not by the suite. These
assertions execute the routes instead.

Unlike _receive_auth_child.py, the token is set BEFORE import: `_backlog_token`
is a module-level global read at import time (audio_receiver.py:160), not per
request, so setting it afterwards would not take effect.

Usage: python _backlog_authz_child.py <scenario.json> <result.json>

The result goes to a file because the app logs `[backlog] ...` lines to stdout
while the scenario runs. Exits non-zero with the traceback on stderr if the
scenario itself raises, so a broken scenario can never look like a pass.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> None:
    scenario_path, result_path = sys.argv[1], sys.argv[2]
    scenario = json.loads(Path(scenario_path).read_text())

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "calls.db")

        # The whole config layer must point at the temp dir BEFORE audio_receiver
        # imports, since modules.config freezes these at import time.
        os.environ["DB_PATH"] = db_path
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
        os.environ["TIPS_UPLOAD_DIR"] = str(Path(tmp) / "tips")
        os.environ["TGID_TSV"] = str(Path(tmp) / "no-such-tags.tsv")

        token = scenario.get("token")
        if token is None:
            os.environ.pop("BB_BACKLOG_AGENT_TOKEN", None)
        else:
            os.environ["BB_BACKLOG_AGENT_TOKEN"] = token

        import audio_receiver
        from modules.database import init_db

        init_db()

        # Seed one real queue item so `claim` has work to hand out and so the
        # refusal cases prove the queue was not drained.
        with audio_receiver._backlog_lock:
            audio_receiver._backlog_queue.append(
                {
                    "id": "seed-item",
                    "audio_b64": "SEVMTG8=",
                    "tgid": 12345,
                    "tag": "seeding",
                    "node": "pie3",
                    "duration": 1.0,
                    "received_ts": 1.0,
                }
            )

        client = audio_receiver.app.test_client()
        headers = {}
        for name, value in (scenario.get("headers") or {}).items():
            if value is not None:
                headers[name] = value

        route = scenario["route"]
        response = client.post(
            f"/api/backlog/{route}",
            json=scenario.get("body") or {},
            headers=headers,
        )

        rows = 0
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

        # _backlog_queue is a collections.deque (the handler uses popleft), not
        # a queue.Queue, so depth is len().
        with audio_receiver._backlog_lock:
            queued = len(audio_receiver._backlog_queue)

        Path(result_path).write_text(
            json.dumps(
                {
                    "status": response.status_code,
                    "raw_body": (response.get_data(as_text=True) or ""),
                    "body": response.get_json(silent=True),
                    "calls_rows": rows,
                    "queue_depth": queued,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()