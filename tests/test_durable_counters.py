"""Backlog counters must survive a process restart.

They did not. `battlebuddy_backlog_completed_total` and
`battlebuddy_ingest_outcomes_total` were module-level dicts in `audio_receiver`,
so every restart reset them to zero.

That matters because overflow is *rare* — often a handful of clips a day. The
counters read a plausible `0`, there was nothing to look wrong, and the only
evidence that the remote worker had ever transcribed anything was a systemd log
on a different host. A single deploy destroyed it. Four real talkgroups
(Lakeway PD 1, AFD FTAC205, TC CN COMM, TCEMS MedCom C) had gone through
Hostinger and the database could not say so, because remote and local calls were
both stored with the originating radio's `node`.

Every scenario runs in a clean interpreter, so "restart" is a genuine second
process against the same database rather than a reset of in-process state.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CHILD = _HERE / "_durable_counters_child.py"


def _phase(db: str, payload: dict) -> dict:
    """Run one interpreter against `db`.

    The caller owns the database path. An earlier version created a fresh
    TemporaryDirectory per call, so every phase got a *different* database and
    the "survives a restart" tests were really testing nothing.
    """
    tmp = tempfile.mkdtemp()
    try:
        script_path = Path(tmp) / "scenario.json"
        out_path = Path(tmp) / "result.json"
        scenario = dict(payload)
        scenario["db_path"] = db
        script_path.write_text(json.dumps(scenario), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(script_path), str(out_path)],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0 or not out_path.exists():
            raise AssertionError(
                f"child failed ({proc.returncode})\n"
                f"stdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _DurableCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "calls.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestCountersSurviveRestart(_DurableCase):
    def test_counter_written_before_a_restart_is_readable_after_it(self):
        first = _phase(self.db, {"do": "bump", "name": "backlog_completed", "labels": "hostinger"})
        self.assertEqual(1.0, first["value"])

        # A brand new interpreter, same database file: this is the restart.
        second = _phase(self.db, {"do": "read", "name": "backlog_completed", "labels": "hostinger"})
        self.assertEqual(
            1.0, second["value"],
            "the counter reset on restart -- a deploy erases the only evidence "
            "that the remote worker ever transcribed anything",
        )

    def test_counter_accumulates_across_restarts(self):
        _phase(self.db, {"do": "bump", "name": "backlog_completed", "labels": "hostinger"})
        _phase(self.db, {"do": "bump", "name": "backlog_completed", "labels": "hostinger"})
        # Each bump was a separate interpreter, so this only reaches 2 if the
        # value survived two restarts and accumulated rather than resetting.
        third = _phase(self.db, {"do": "read", "name": "backlog_completed", "labels": "hostinger"})
        self.assertEqual(2.0, third["value"])

    def test_labels_are_independent(self):
        _phase(self.db, {"do": "bump", "name": "backlog_completed", "labels": "hostinger"})
        _phase(self.db, {"do": "bump", "name": "backlog_completed", "labels": "mac"})
        host = _phase(self.db, {"do": "read", "name": "backlog_completed", "labels": "hostinger"})
        mac = _phase(self.db, {"do": "read", "name": "backlog_completed", "labels": "mac"})
        self.assertEqual(1.0, host["value"])
        self.assertEqual(1.0, mac["value"])

    def test_ingest_outcomes_persist_and_round_trip_as_pairs(self):
        _phase(self.db, {"do": "ingest", "reason": "backlogged", "node": "pi5"})
        result = _phase(self.db, {"do": "read_ingest"})
        self.assertEqual(
            {"backlogged|pi5": 1.0}, result["ingest"],
            "ingest outcomes must survive a restart too -- they are how a loss "
            "becomes measurable at all",
        )

    def test_unknown_counter_reads_zero_rather_than_raising(self):
        result = _phase(self.db, {"do": "read", "name": "never_written", "labels": "x"})
        self.assertEqual(0.0, result["value"])


class TestWorkerAttribution(_DurableCase):
    def test_call_records_the_worker_that_transcribed_it(self):
        _phase(self.db, {"do": "insert", "worker": "hostinger", "tag": "Lakeway PD 1"})
        result = _phase(self.db, {"do": "read_calls"})
        self.assertEqual(
            [{"tag": "Lakeway PD 1", "worker": "hostinger", "node": "pi5"}],
            result["calls"],
            "a remotely transcribed call must be distinguishable from a local "
            "one, or overflow is invisible after the fact",
        )

    def test_local_call_has_no_worker(self):
        _phase(self.db, {"do": "insert", "worker": None, "tag": "TCEMS Dispatch"})
        result = _phase(self.db, {"do": "read_calls"})
        self.assertIsNone(result["calls"][0]["worker"])


if __name__ == "__main__":
    unittest.main()