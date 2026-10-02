"""Scenario runner for tests/test_test_call_gate.py. Not a test file.

Executed as a subprocess so `BB_TEST_CALL_TOKEN` and `BB_TEST_CALL_ENABLED` are
read from a controlled environment before audio_receiver is imported. Both are
module-level globals read at import time, so setting them in-process after the
import does nothing.

Usage: python _test_call_gate_child.py <scenario.json> <result.json>
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> None:
    scenario_path, result_path = sys.argv[1], sys.argv[2]
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "calls.db")

        # Everything before the first project import.
        os.environ["DB_PATH"] = db_path
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
        os.environ["BB_RAW_AUDIO_QUEUE_DIR"] = str(Path(tmp) / "raw_audio_queue")
        os.environ["TIPS_UPLOAD_DIR"] = str(Path(tmp) / "tips")
        os.environ["TGID_TSV"] = str(Path(tmp) / "no-such-tags.tsv")
        os.environ["HOMICIDE_SEED_PATH"] = str(Path(tmp) / "none.json")

        enabled = scenario.get("enabled")
        token = scenario.get("token")
        if enabled is None:
            os.environ.pop("BB_TEST_CALL_ENABLED", None)
        else:
            os.environ["BB_TEST_CALL_ENABLED"] = "1" if enabled else "0"
        if token is None:
            os.environ.pop("BB_TEST_CALL_TOKEN", None)
        else:
            os.environ["BB_TEST_CALL_TOKEN"] = token

        sys.modules["stripe"] = mock.MagicMock()
        import audio_receiver
        from modules.database import init_db

        init_db()

        # Stub everything that makes outbound network calls.
        #
        # The route under test calls post_to_talk, and analyze_for_incident calls
        # create_deck_card / post_banner / send_dm_alert. Those reach Nextcloud
        # and Talk over the network, and DNS intermittently fails in this
        # environment -- "[banner] failed: No address associated with hostname".
        # That made this suite flaky at roughly 1 run in 20, which is worse than
        # no test: a check that passes most of the time verifies nothing.
        #
        # The property under test is whether a fabricated incident is marked
        # is_test. How the banner is delivered afterwards is irrelevant to it.
        import modules.alerts as alerts_mod

        audio_receiver.post_to_talk = lambda *_a, **_kw: None
        for _name in ("create_deck_card", "post_banner", "send_dm_alert"):
            if hasattr(alerts_mod, _name):
                setattr(alerts_mod, _name, lambda *_a, **_kw: None)

        client = audio_receiver.app.test_client()
        headers = {}
        for name, value in (scenario.get("headers") or {}).items():
            if value is not None:
                headers[name] = value

        body = dict(scenario.get("body") or {})
        if scenario.get("use_real_seeded_id"):
            pass  # body supplied verbatim by the caller

        response = client.post("/test_call", json=body, headers=headers)

        rows = test_rows = incident_rows = 0
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            test_rows = conn.execute(
                "SELECT COUNT(*) FROM calls WHERE is_test = 1"
            ).fetchone()[0]
            # Count what the APPLICATION can see. Every map, sitrep and public
            # query filters on (is_test IS NULL OR is_test = 0), so this is the
            # number that matters: a fabricated incident must not appear here.
            try:
                incident_rows = conn.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE (is_test IS NULL OR is_test = 0)"
                ).fetchone()[0]
                marked = conn.execute(
                    "SELECT COUNT(*) FROM incidents WHERE is_test = 1"
                ).fetchone()[0]
            except sqlite3.Error:
                incident_rows, marked = -1, -1

        Path(result_path).write_text(
            json.dumps({
                "status": response.status_code,
                "body": response.get_json(silent=True),
                "calls_rows": rows,
                "test_rows": test_rows,
                "incidents_visible_to_app": incident_rows,
                "incidents_marked_test": marked,
            }),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()