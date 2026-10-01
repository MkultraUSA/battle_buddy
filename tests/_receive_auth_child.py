"""Scenario runner for tests/test_receive_auth.py.

Not a test file. Executed as a subprocess by that suite so each scenario runs in
a clean interpreter.

That isolation is not optional. Importing `audio_receiver` pulls in
`faster_whisper` via `modules.transcription`, which several suites stub in
`sys.modules` at COLLECTION time and never remove. A stubbed transcription
module would mean the route under test was never the real one. The same problem
in the other direction: `audio_receiver` cannot even be imported where
faster_whisper is absent, so the parent skips rather than reporting a green run
it did not earn.

Importing audio_receiver is otherwise safe here -- its startup block (model
warm, pollers, cleanup threads) lives under `if __name__ == "__main__"`, so a
plain import starts nothing.

Usage: python _receive_auth_child.py <scenario.json> <result.json>

Writes one JSON object to the result path. Exits non-zero with the traceback on
stderr if the scenario itself raises, so a broken scenario can never look like a
passing assertion. The result goes to a file rather than stdout because the app
logs `[ingest] ...` lines while the scenario runs.
"""

from __future__ import annotations

import contextlib
import io
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

        # Point the whole config layer at the temp dir BEFORE audio_receiver
        # imports, since modules.config freezes these at import time.
        os.environ["DB_PATH"] = db_path
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
        os.environ["TIPS_UPLOAD_DIR"] = str(Path(tmp) / "tips")
        os.environ["TGID_TSV"] = str(Path(tmp) / "no-such-tags.tsv")
        os.environ.pop("BB_RECEIVE_TOKEN", None)

        import audio_receiver
        from modules.database import init_db

        init_db()

        if scenario.get("token") is not None:
            os.environ["BB_RECEIVE_TOKEN"] = scenario["token"]

        client = audio_receiver.app.test_client()
        headers = {}
        for name, value in (scenario.get("headers") or {}).items():
            if value is not None:
                headers[name] = value

        # A syntactically valid but semantically empty call: enough to prove the
        # request got past authentication and reached body validation, with no
        # audio, no Whisper, no LLM call and no row written.
        #
        # stdout is captured so a test can assert on what the AUTH FAIL log line
        # actually reports.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            response = client.post("/receive", json=scenario.get("body") or {}, headers=headers)

        rows = 0
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

        Path(result_path).write_text(json.dumps({
            "status": response.status_code,
            "body": response.get_json(silent=True),
            "calls_rows": rows,
            "stdout": captured.getvalue(),
        }), encoding="utf-8")


if __name__ == "__main__":
    main()