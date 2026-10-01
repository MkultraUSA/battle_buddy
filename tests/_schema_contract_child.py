"""Scenario runner for tests/test_schema_contract.py. Not a test file.

Executed as a subprocess so `DB_PATH` can be set before anything imports
modules.config.

That ordering is not optional. modules/database.py does
`from modules.config import ... DB_PATH`, which freezes the path at *import*
time. A test that sets os.environ["DB_PATH"] in-process therefore does nothing
once another suite in the same pytest process has already imported
modules.config -- which is what the first version of this test did. It silently
tested whatever database the run happened to have, and could write into a
database other tests were using.

Usage: python _schema_contract_child.py <scenario.json> <result.json>
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
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "fresh.db")

        # Everything below must happen before the first project import.
        os.environ["DB_PATH"] = db_path
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
        os.environ["BB_RAW_AUDIO_QUEUE_DIR"] = os.path.join(tmp, "raw_queue")
        os.environ["TIPS_UPLOAD_DIR"] = os.path.join(tmp, "tips")
        os.environ["TGID_TSV"] = os.path.join(tmp, "none.tsv")
        os.environ["HOMICIDE_SEED_PATH"] = os.path.join(tmp, "none.json")
        os.environ.pop("BB_BACKLOG_ENABLED", None)

        from modules.config import DB_PATH as CONFIG_DB_PATH
        from modules.database import init_db

        created_at = CONFIG_DB_PATH
        init_db()

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description) "
            "VALUES (1.0, 1.0, 'Fire', 'test')"
        )
        conn.commit()

        # init_db() runs on every service start, so a second run must not fail.
        rerun_error = ""
        try:
            init_db()
        except Exception as exc:  # pragma: no cover - reported, not raised
            rerun_error = str(exc)

        cols = sorted(r[1] for r in conn.execute("PRAGMA table_info(incidents)"))

        # Every incident query audio_receiver issues, run against the fresh DB.
        queries = {
            "metrics_active": "SELECT COUNT(*) FROM incidents "
                              "WHERE (is_test IS NULL OR is_test=0)",
            "sitrep": "SELECT id FROM incidents "
                      "WHERE (is_test IS NULL OR is_test=0) LIMIT 1",
            "recent_window": "SELECT id FROM incidents WHERE ts_start > 0 "
                             "AND (is_test IS NULL OR is_test=0)",
            "flag_write": "UPDATE incidents SET flagged=1 WHERE id=1",
            "flag_read": "SELECT COUNT(*) FROM incidents WHERE flagged=1",
            "active_count": "SELECT COUNT(*) FROM incidents "
                            "WHERE ts_start > 0 AND (is_test IS NULL OR is_test=0)",
        }
        query_errors = {}
        for label, sql in queries.items():
            try:
                conn.execute(sql).fetchall()
            except sqlite3.Error as exc:
                query_errors[label] = str(exc)
        conn.close()

        result = {
            "db_path_used": created_at,
            "db_path_expected": db_path,
            "columns": cols,
            "query_errors": query_errors,
            "init_rerun_error": rerun_error,
        }

        if scenario.get("scrape_metrics"):
            import contextlib
            import io
            from unittest import mock

            sys.modules["stripe"] = mock.MagicMock()
            import audio_receiver

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                resp = audio_receiver.app.test_client().get("/metrics")
            body = resp.get_data(as_text=True)
            result["metrics"] = {
                "status": resp.status_code,
                "samples": len([
                    line for line in body.splitlines()
                    if line and not line.startswith("#")
                ]),
                "has_backlog": "battlebuddy_backlog_queue_depth" in body,
                "noise": buf.getvalue()[-2000:],
            }

        Path(result_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()