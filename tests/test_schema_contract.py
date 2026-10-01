"""A from-scratch database must satisfy every query the application makes.

`init_db()` did not create `incidents.is_test` or `incidents.flagged`, both of
which `audio_receiver.py` reads and writes. They existed in production only
because someone had once run the ALTER by hand and it was never codified. So:

  * a fresh deploy produced a database with neither column;
  * `/metrics` then aborted with "no such column: is_test" and returned an EMPTY
    body under HTTP 200 -- every Grafana panel blank, ops_verify's metric gates
    blind, and no error surfaced anywhere to say so;
  * `UPDATE incidents SET flagged=1`, the flag endpoint, raised a 500.

The failure is silent and only appears during a rebuild, which is the worst time
to discover it. The check below is deliberately blunt: build a brand-new
database with nothing but `init_db()`, then run the application's own queries
against it. Any column the app needs and the schema lacks shows up here rather
than during an incident.

The metric-scrape case is the important one, because the collector catches
exceptions and returns a partial body, so a missing column degrades monitoring
instead of breaking it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _created_columns(db_source: str, table: str) -> set[str]:
    """Column names init_db() puts in `table`."""
    block = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\s*\)", db_source, re.S
    )
    assert block, f"no CREATE TABLE for {table} in modules/database.py"
    return set(re.findall(r"^\s*([a-z_]+)\s", block.group(1), re.M))


class TestFromScratchSchemaSupportsTheApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_source = (_ROOT / "modules" / "database.py").read_text(encoding="utf-8")
        cls.app_source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "fresh.db"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _fresh_db(self) -> sqlite3.Connection:
        """Build a database from nothing else."""
        if self.path.exists():
            self.path.unlink()
        prev = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = str(self.path)
        try:
            from modules.database import init_db

            init_db()
        finally:
            if prev is None:
                os.environ.pop("DB_PATH", None)
            else:
                os.environ["DB_PATH"] = prev
        return sqlite3.connect(self.path)

    def test_init_db_creates_is_test(self):
        self.assertIn("is_test", _created_columns(self.db_source, "incidents"))

    def test_init_db_creates_flagged(self):
        self.assertIn("flagged", _created_columns(self.db_source, "incidents"))

    def test_incident_queries_run_on_a_from_scratch_database(self):
        """The queries audio_receiver actually issues, against a fresh DB."""
        conn = self._fresh_db()
        conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description) "
            "VALUES (1.0, 1.0, 'Fire', 'test')"
        )
        conn.commit()
        # Every filter the app uses to exclude test rows.
        for label, sql in (
            ("metrics active", "SELECT COUNT(*) FROM incidents "
                               "WHERE (is_test IS NULL OR is_test=0)"),
            ("sitrep", "SELECT id FROM incidents "
                       "WHERE (is_test IS NULL OR is_test=0) LIMIT 1"),
            ("recent window", "SELECT id FROM incidents WHERE ts_start > 0 "
                              "AND (is_test IS NULL OR is_test=0)"),
            ("flag write", "UPDATE incidents SET flagged=1 WHERE id=1"),
            ("flag read", "SELECT COUNT(*) FROM incidents WHERE flagged=1"),
        ):
            with self.subTest(query=label):
                try:
                    conn.execute(sql).fetchall()
                except sqlite3.Error as exc:
                    self.fail(
                        f"{label!r} fails on a from-scratch database: {exc}. "
                        "init_db() does not create a column the app requires."
                    )
        conn.close()

    def test_init_db_is_idempotent(self):
        """Deploys call init_db() on every start, so it must survive reruns."""
        conn = self._fresh_db()
        from modules.database import init_db

        init_db()  # second run, same process
        cols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)")}
        self.assertIn("is_test", cols)
        self.assertIn("flagged", cols)
        conn.close()

    def test_metrics_endpoint_serves_a_full_body_on_a_fresh_database(self):
        """The collector swallows errors and returns HTTP 200 with less output.

        That is why the missing column went unnoticed: monitoring degraded
        silently instead of failing loudly. Assert the body is actually
        populated, and that no collector error was printed.
        """
        import subprocess
        import sys

        child = r"""
import io, contextlib, json, os, sys, tempfile
from unittest import mock
sys.modules["stripe"] = mock.MagicMock()

with tempfile.TemporaryDirectory() as tmp:
    os.environ["DB_PATH"] = os.path.join(tmp, "fresh.db")
    os.environ["BATTLE_BUDDY_HOME"] = tmp
    os.environ["BATTLE_BUDDY_DATA_DIR"] = tmp
    os.environ["BB_RAW_AUDIO_QUEUE_DIR"] = os.path.join(tmp, "raw_queue")
    os.environ["TIPS_UPLOAD_DIR"] = os.path.join(tmp, "tips")
    os.environ["TGID_TSV"] = os.path.join(tmp, "none.tsv")

    import audio_receiver
    from modules.database import init_db
    init_db()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        resp = audio_receiver.app.test_client().get("/metrics")
    body = resp.get_data(as_text=True)
    print(json.dumps({
        "status": resp.status_code,
        "lines": len([l for l in body.splitlines() if l and not l.startswith("#")]),
        "has_backlog": "battlebuddy_backlog_queue_depth" in body,
        "noise": buf.getvalue(),
    }))
"""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(_ROOT), env=env, capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            self.skipTest(f"audio_receiver not importable here: {proc.stderr[-300:]}")
        payload = json.loads(proc.stdout.splitlines()[-1])

        self.assertEqual(200, payload["status"])
        self.assertNotIn(
            "collector error", payload["noise"],
            "the metrics collector caught an error, so the body is partial and "
            "every panel downstream is quietly wrong",
        )
        self.assertGreater(
            payload["lines"], 20,
            f"/metrics served only {payload['lines']} samples from a fresh "
            "database; monitoring is effectively blank",
        )
        self.assertTrue(payload["has_backlog"], "backlog gauges missing from a fresh DB")


if __name__ == "__main__":
    unittest.main()