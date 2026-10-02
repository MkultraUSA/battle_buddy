"""Every LLM outcome must be counted, and only the one that actually happened.

There was no counter at all. `llm_analyze()` returns `None` both when it
deliberately declines to spend money and when the provider call fails, so "we
skipped this call" was indistinguishable from "we paid for it and lost it". The
703 LLM calls/day figure was hand-inferred and could not be checked.

Counting is only worth anything if the labels are *trustworthy*, so this drives
each path deliberately and asserts that path increments exactly one outcome and
no other. A mislabelled counter is worse than none: it would be believed.

Each scenario is a separate interpreter, because several paths are only reachable
once module state in `modules.llm` (the routine tracker, the backoff deadline)
has been primed, and that state would otherwise leak between scenarios.

These run in ordinary environments -- `modules.llm` does not need faster_whisper.
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
_CHILD = _HERE / "_llm_outcome_child.py"

#: Every outcome llm_analyze can report. If the implementation adds one, this
#: list must gain one too -- an outcome nobody asserts is an outcome nobody checks.
ALL_OUTCOMES = (
    "disabled",
    "no_tgid",
    "skipped_short",
    "skipped_nonspeech",
    "skipped_short_duration",
    "skipped_cooldown",
    "skipped_backoff",
    "analyzed",
    "error",
)


def _drive(outcome: str, **extra) -> dict:
    tmp = tempfile.mkdtemp()
    try:
        scenario_path = Path(tmp) / "scenario.json"
        out_path = Path(tmp) / "result.json"
        scenario = dict(extra)
        scenario["outcome"] = outcome
        # `db_path` is how a caller shares one database across two runs.
        scenario.setdefault("db_path", None)
        scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(scenario_path), str(out_path)],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0 or not out_path.exists():
            raise AssertionError(
                f"child failed for outcome={outcome} ({proc.returncode})\n"
                f"stdout:\n{proc.stdout[-1500:]}\nstderr:\n{proc.stderr[-1500:]}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestEachPathCountsExactlyItsOwnOutcome(unittest.TestCase):
    def test_every_outcome_is_reachable_and_counted_alone(self):
        for outcome in ALL_OUTCOMES:
            with self.subTest(outcome=outcome):
                extra = {"tgid_zero": True} if outcome == "no_tgid" else {}
                result = _drive(outcome, **extra)
                counted = {k: v for k, v in result["outcomes"].items() if v}
                self.assertEqual(
                    {outcome: 1.0}, counted,
                    f"driving the {outcome!r} path counted {counted!r}; exactly "
                    f"one outcome should move",
                )

    def test_skipped_and_error_paths_return_none_but_analyzed_does_not(self):
        for outcome in ("no_tgid", "skipped_short", "skipped_nonspeech",
                        "skipped_cooldown", "skipped_backoff", "error"):
            with self.subTest(outcome=outcome):
                extra = {"tgid_zero": True} if outcome == "no_tgid" else {}
                self.assertTrue(
                    _drive(outcome, **extra)["returned_none"],
                    f"{outcome} is a skip or a failure and must still return None; "
                    "counting must not change the return contract",
                )
        self.assertFalse(
            _drive("analyzed")["returned_none"],
            "a successful analysis must not return None",
        )

    def test_counting_does_not_suppress_the_provider_call(self):
        """`analyzed` must mean a call actually happened, not just that we got there."""
        self.assertFalse(
            _drive("analyzed")["returned_none"],
            "a real result must come back",
        )


class TestCounterIsDurableNotInProcess(unittest.TestCase):
    """The whole reason for using bump_counter rather than a dict."""

    def test_outcome_survives_a_process_restart(self):
        """One interpreter writes, a second reads from the same database.

        This is the property the whole feature exists for. If the implementation
        regressed to a module-level dict the second read would be 0.0 -- exactly
        the failure that made the previous generation of counters untrustworthy
        and that forced the ingest counters to be rewritten in PR #177.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "calls.db")
            first = _drive("analyzed", db_path=db)
            self.assertEqual(1.0, first["outcomes"].get("analyzed", 0.0))

            second = _drive("skipped_short", db_path=db)
            self.assertEqual(
                1.0, second["outcomes"].get("analyzed", 0.0),
                "the earlier outcome was erased by a restart",
            )
            self.assertEqual(
                1.0, second["outcomes"].get("skipped_short", 0.0),
                "the second process must also record its own outcome",
            )

    def test_outcomes_accumulate_rather_than_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "calls.db")
            _drive("analyzed", db_path=db)
            _drive("analyzed", db_path=db)
            result = _drive("skipped_short", db_path=db)
            self.assertEqual(
                2.0, result["outcomes"].get("analyzed", 0.0),
                "two analyses across three restarts must total 2, not 1",
            )

    def test_counters_table_is_the_store(self):
        """Pin the storage choice, which is the property the feature exists for."""
        source = (Path(_ROOT) / "modules" / "llm.py").read_text(encoding="utf-8")
        self.assertIn("bump_counter", source)
        self.assertNotIn(
            "_llm_outcomes: dict", source,
            "an in-process dict resets on restart; use bump_counter",
        )


if __name__ == "__main__":
    unittest.main()