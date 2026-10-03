"""The regression battery must be able to fail, loudly, in three distinct ways.

`bb-prod-regression.timer` runs hourly on the host and writes a results file.
`ops_verify.py` gates on it, and `_regression_metric_specs()` exports it. None of
that is exercised by an ordinary test run, which is exactly the condition under
which a check rots into a green check that never executed anything.

The three failure modes are deliberately separate gates, because they are three
different problems and collapsing them is how this project has been bitten:

  * **the battery ran and something failed** — a regression
  * **the battery did not run at all** — the alarm is broken
  * **the results are stale** — the timer stopped

The middle one is the important one. A broken alarm and a healthy system are
indistinguishable from outside, which is this project's own defect #2: the
Telegram watcher queried three metric names the app never emitted and
`metrics.get(key, 0.0)` turned each miss into a healthy zero. So freshness is
gated on its own, and "never ran" is a failure rather than an absence of data.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OPS = _ROOT / "scripts" / "ops_verify.py"


def _ops_gates(results_path: Path) -> list[dict]:
    """Run ops_verify with a given results file and return its JSON gates.

    A subprocess, because ops_verify reaches for the live database and the camera
    snapshot -- both of which are absent here, so most of its other gates will
    fail. That is fine and expected; this only reads the three regression gates.
    """
    proc = subprocess.run(
        [sys.executable, str(_OPS), "--json"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
        env={**os.environ, "BB_REGRESSION_RESULTS_PATH": str(results_path)},
    )
    text = proc.stdout
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise AssertionError(f"ops_verify produced no JSON:\n{proc.stdout[-800:]}")
    return json.loads(text[start:end + 1])["gates"]


def _gate(gates: list[dict], name: str) -> dict:
    for g in gates:
        if g["gate"].startswith(name):
            return g
    raise AssertionError(f"no gate named {name!r} in {[g['gate'] for g in gates]}")


def _results(path: Path, *, age_s: float = 0.0, failed: list[str] | None = None) -> Path:
    stamp = datetime.datetime.fromtimestamp(
        datetime.datetime.now().timestamp() - age_s,
        tz=datetime.timezone.utc,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "generated": stamp,
        "ran": 3,
        "failed": len(failed or []),
        "checks": [
            {"check": "http /public", "ok": True, "skipped": False, "detail": ""},
            {"check": "http /api/incidents", "ok": True, "skipped": False, "detail": ""},
            {"check": failed[0] if failed else "vps: ops_verify 19/19",
             "ok": not failed, "skipped": False, "detail": ""},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestTheRegressionGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_healthy_run_passes_all_three(self):
        gates = _ops_gates(_results(self.dir / "ok.json"))
        self.assertTrue(_gate(gates, "regression battery has run")["pass"])
        self.assertTrue(_gate(gates, "regression battery fresh")["pass"])
        self.assertTrue(_gate(gates, "regression battery all green")["pass"])

    def test_a_missing_results_file_fails(self):
        """The alarm being broken must not read as a healthy system."""
        gates = _ops_gates(self.dir / "absent.json")
        self.assertFalse(
            _gate(gates, "regression battery has run")["pass"],
            "no results file was accepted as 'has run'",
        )

    def test_a_stale_run_fails_freshness_but_not_existence(self):
        """Two missed hours is the threshold; four hours must not look fresh."""
        gates = _ops_gates(_results(self.dir / "stale.json", age_s=4 * 3600))
        self.assertTrue(_gate(gates, "regression battery has run")["pass"],
                        "a stale run did happen, so the existence gate is right")
        self.assertFalse(
            _gate(gates, "regression battery fresh")["pass"],
            "a four-hour-old run was accepted as fresh",
        )

    def test_a_real_regression_fails_and_is_named(self):
        gates = _ops_gates(_results(self.dir / "bad.json", failed=["xss /premium/commute"]))
        self.assertTrue(_gate(gates, "regression battery fresh")["pass"],
                        "a fresh run that failed is fresh; only the outcome is wrong")
        gate = _gate(gates, "regression battery all green")
        self.assertFalse(gate["pass"])
        self.assertIn("xss", gate["detail"], "the failing check was not named")

    def test_a_corrupt_results_file_fails_rather_than_raising(self):
        bad = self.dir / "corrupt.json"
        bad.write_text("{not json", encoding="utf-8")
        gates = _ops_gates(bad)
        self.assertFalse(_gate(gates, "regression battery has run")["pass"])

    def test_a_file_with_no_checks_key_fails(self):
        bad = self.dir / "empty.json"
        bad.write_text('{"ran": 0, "failed": 0}', encoding="utf-8")
        gates = _ops_gates(bad)
        self.assertFalse(_gate(gates, "regression battery has run")["pass"])

    def test_an_unparseable_timestamp_fails_freshness(self):
        path = _results(self.dir / "badstamp.json")
        payload = json.loads(path.read_text())
        payload["generated"] = "not-a-date"
        path.write_text(json.dumps(payload), encoding="utf-8")
        gates = _ops_gates(path)
        self.assertFalse(_gate(gates, "regression battery fresh")["pass"])


class TestTheCollectorReadsTheSameFile(unittest.TestCase):
    """The metrics path and the gate path must not disagree.

    Both read the one file, and that is the point: a second source of truth is
    how `REQUIRED_METRICS` came to cover 7 of 13 names, and how the app and the
    watcher each ended up with their own list.
    """

    def test_the_path_comes_from_one_environment_variable(self):
        src = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        self.assertIn('BB_REGRESSION_RESULTS_PATH', src)
        ops = _OPS.read_text(encoding="utf-8")
        self.assertIn('BB_REGRESSION_RESULTS_PATH', ops,
                      "the gate reads a different path than the collector")

    def test_the_default_path_is_the_same_in_both(self):
        default = "/opt/battlebuddy-data/regression/latest.json"
        self.assertIn(default, (_ROOT / "audio_receiver.py").read_text(encoding="utf-8"))
        self.assertIn(default, _OPS.read_text(encoding="utf-8"))


class TestTheUnitFilesAreShippedAndWired(unittest.TestCase):
    """Cheap guards on the wiring, so it cannot rot unnoticed."""

    def test_both_unit_files_exist(self):
        for name in ("bb-prod-regression.service", "bb-prod-regression.timer"):
            self.assertTrue((_ROOT / "systemd" / name).is_file(), f"{name} is missing")

    def test_the_timer_is_persistent_and_fires_after_a_reboot(self):
        timer = (_ROOT / "systemd" / "bb-prod-regression.timer").read_text(encoding="utf-8")
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnBootSec=", timer,
                      "a rebuilt host would show a stale green strip otherwise")
        self.assertIn("UTC", timer,
                      "this host runs Europe/Berlin; an unqualified OnCalendar "
                      "moves an hour twice a year")

    def test_the_service_runs_the_battery_against_itself(self):
        svc = (_ROOT / "systemd" / "bb-prod-regression.service").read_text(encoding="utf-8")
        self.assertIn("scripts/prod_regression.py", svc)
        self.assertIn("--host local", svc)
        self.assertIn("--write-json", svc)
        self.assertIn("NoNewPrivileges=true", svc)


if __name__ == "__main__":
    unittest.main()