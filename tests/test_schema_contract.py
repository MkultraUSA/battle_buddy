"""A from-scratch database must satisfy every query the application makes.

`init_db()` did not create `incidents.is_test` or `incidents.flagged`, both of
which `audio_receiver.py` reads and writes. They existed in production only
because the ALTER had once been run by hand and was never codified. So:

  * a fresh deploy produced a database with neither column;
  * `/metrics` then aborted with "no such column: is_test" and returned an EMPTY
    body under HTTP 200 — every Grafana panel blank, ops_verify's metric gates
    blind, and no error surfaced anywhere to say so;
  * `UPDATE incidents SET flagged=1`, the flag endpoint, raised a 500.

The failure is silent and only appears during a rebuild, which is the worst time
to discover it. The check is deliberately blunt: build a database from nothing
but `init_db()`, then run the application's own queries against it. Any column the
app needs and the schema lacks fails here rather than during an incident.

The scrape case matters most, because the collector catches exceptions and
streams what it already has — so a missing column degrades monitoring instead of
breaking it, and nothing looks wrong.

Everything runs in a subprocess (`_schema_contract_child.py`) because
`modules.database` does `from modules.config import DB_PATH`, which freezes the
path at *import* time. Setting `os.environ["DB_PATH"]` in-process does nothing
once another suite has imported `modules.config` — an earlier version of this test
silently tested whatever database the run happened to have, and could write into a
database other tests were using.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CHILD = _HERE / "_schema_contract_child.py"


def _importable() -> tuple[bool, str]:
    """Can this interpreter import audio_receiver at all?

    The scrape scenario needs it, and it requires faster_whisper. Skip rather than
    fail where it is absent: an environment limitation reported as a red test
    looks like a regression, and this suite has a whole point about not confusing
    the two. Mirrors test_receive_auth.py.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import audio_receiver"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    return proc.returncode == 0, (proc.stderr or "")[-300:]


_IMPORTABLE, _IMPORT_ERROR = _importable()


def _run(*, scrape_metrics: bool = False) -> dict:
    """Run the child in a clean interpreter; returns its JSON result."""
    with tempfile.TemporaryDirectory() as tmp:
        scenario_path = Path(tmp) / "scenario.json"
        result_path = Path(tmp) / "result.json"
        scenario_path.write_text(
            json.dumps({"scrape_metrics": scrape_metrics}), encoding="utf-8"
        )
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(scenario_path), str(result_path)],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0 or not result_path.exists():
            raise AssertionError(
                f"schema child failed ({proc.returncode})\n"
                f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
            )
        return json.loads(result_path.read_text(encoding="utf-8"))


def _created_columns(db_source: str, table: str) -> set[str]:
    block = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\s*\)", db_source, re.S
    )
    assert block, f"no CREATE TABLE for {table} in modules/database.py"
    return set(re.findall(r"^\s*([a-z_]+)\s", block.group(1), re.M))


class TestInitDbCreatesWhatTheAppUses(unittest.TestCase):
    def test_init_db_creates_is_test(self):
        source = (_ROOT / "modules" / "database.py").read_text(encoding="utf-8")
        self.assertIn(
            "is_test", _created_columns(source, "incidents"),
            "audio_receiver filters on incidents.is_test in a dozen places",
        )

    def test_init_db_creates_flagged(self):
        source = (_ROOT / "modules" / "database.py").read_text(encoding="utf-8")
        self.assertIn(
            "flagged", _created_columns(source, "incidents"),
            "audio_receiver executes UPDATE incidents SET flagged=1",
        )


class TestFromScratchSchemaRunsTheAppsQueries(unittest.TestCase):
    def test_child_actually_used_a_fresh_database(self):
        """Guards the harness itself.

        If DB_PATH were not set before the first project import, this suite would
        cheerfully test whatever database the run happened to have — which is how
        the first version of this file passed while proving nothing.
        """
        result = _run()
        self.assertEqual(
            result["db_path_expected"], result["db_path_used"],
            "the child did not get its own database; DB_PATH was frozen by an "
            "earlier import and this test was inspecting someone else's data",
        )

    def test_every_incident_query_runs_on_a_from_scratch_database(self):
        result = _run()
        self.assertEqual(
            {}, result["query_errors"],
            "init_db() does not create a column the application requires: "
            f"{result['query_errors']}",
        )

    def test_columns_present_after_init(self):
        result = _run()
        for column in ("is_test", "flagged"):
            self.assertIn(column, result["columns"])

    def test_init_db_is_idempotent(self):
        """The service calls init_db() on every start, so a rerun must not fail."""
        result = _run()
        self.assertEqual(
            "", result["init_rerun_error"],
            f"a second init_db() raised: {result['init_rerun_error']}",
        )


class TestMetricsAreFullyServedOnAFreshDatabase(unittest.TestCase):
    def setUp(self) -> None:
        if not _IMPORTABLE:
            self.skipTest(
                "audio_receiver cannot be imported here (needs faster_whisper); "
                f"the authoritative baseline is /opt/battlebuddy/venv. "
                f"{_IMPORT_ERROR}"
            )

    def test_scrape_has_no_collector_error_and_is_not_blank(self):
        result = _run(scrape_metrics=True)
        m = result.get("metrics") or {}
        self.assertEqual(200, m.get("status"))
        self.assertNotIn(
            "collector error", m.get("noise", ""),
            "the collector caught an error, so /metrics is partial and every "
            "panel downstream is quietly wrong",
        )
        self.assertGreater(
            m.get("samples", 0), 20,
            f"/metrics served only {m.get('samples')} samples from a fresh "
            "database; monitoring would be effectively blank",
        )
        self.assertTrue(m.get("has_backlog"), "backlog gauges missing on a fresh DB")


if __name__ == "__main__":
    unittest.main()