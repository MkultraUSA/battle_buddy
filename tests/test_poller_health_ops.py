"""The poller health metrics must be watched, and the gates must fail when a poller stops.

This is the exact failure class the project has been bitten by twice:
- a watcher querying three metric names the app never emitted, where
  `metrics.get(key, 0.0)` turned each miss into a healthy zero
- an `init_db()` that left `/metrics` serving 200 lines of nothing

A poller that stops on a public-safety feed is silent loss of coverage. The three
metrics (`battlebuddy_poller_active`, `battlebuddy_poller_consecutive_failures`,
`battlebuddy_poller_last_success_age_seconds`) are exported and read by nothing
outside a test, so a poller that stops produces NO signal anywhere - no panel, no
gate, no alert.

These tests pin the three properties that matter:

  * **the gates exist and run on every ops verification.** Not "the function
    exists" - these run `main()` and read the gate results.
  * **ops_verify actually fails when a poller goes bad.** A stopped poller
    (active=0), a repeatedly failing poller (consecutive_failures > 3), or a
    poller that hasn't succeeded in too long (last_success_age > 18h) must all
    fail their respective gates.
  * **the freshness budget and the poll intervals agree.** The longest poll
    interval is 6h (austin-events, apd-cad), so 3 missed cycles = 18h. Nothing
    else in the repo relates those two numbers, so this does.

The tests import ops_verify.py and stub everything except the poller metrics,
so any FAIL is attributable to the poller gates.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPS_VERIFY = ROOT / "scripts" / "ops_verify.py"


def _load_ops_verify(monkeypatch, *, poller_metrics: dict | None = None):
    """Import ops_verify.py with stubbed dependencies and custom poller metrics.

    The poller_metrics dict maps poller name to {"active": 0/1, "failures": int,
    "age_s": float}. Missing pollers default to healthy.
    """
    # Stub systemctl so env load is inert
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else "", 0, "", ""),
    )

    # Only pollers that are actually started appear here. `reddit-intel` was
    # fabricated here as active=1.0, which cannot happen in production because
    # its `.start()` is commented out -- so the suite was green about an invented
    # world while three gates would have been permanently red. If a poller is
    # disabled, REMOVE it. Do not fake it healthy.
    default_pollers = {
        "adsb-air-asset": {"active": 1.0, "failures": 0.0, "age_s": 100.0},
        "afd": {"active": 1.0, "failures": 0.0, "age_s": 100.0},
        "apd-cad": {"active": 1.0, "failures": 0.0, "age_s": 100.0},
        "apd_news": {"active": 1.0, "failures": 0.0, "age_s": 100.0},
        "atxfloods": {"active": 1.0, "failures": 0.0, "age_s": 100.0},
        "austin-events": {"active": 1.0, "failures": 0.0, "age_s": 100.0},
        "traffic-open-data": {"active": 1.0, "failures": 0.0, "age_s": 100.0},
    }

    if poller_metrics:
        for name, vals in poller_metrics.items():
            # Raise rather than ignore. This used to skip unknown names silently,
            # so a test could configure a poller that was not in the list and
            # nothing at all happened -- the test then failed for a reason unrelated
            # to what it was testing. A fixture that cannot fail cannot tell you
            # anything.
            if name not in default_pollers:
                raise KeyError(
                    f"fixture has no poller {name!r}; known: {sorted(default_pollers)}. "
                    f"If it is genuinely started, add it. If it is disabled, remove "
                    f"it -- do not fabricate it as healthy."
                )
            default_pollers[name].update(vals)

    metrics_dict = {
        "battlebuddy_backlog_queue_depth": 0.0,
        "battlebuddy_active_incidents": 1.0,
        "battlebuddy_homicides_seed_error": 0.0,
        "battlebuddy_homicides_seed_newest_ts": time.time() - 86400,
    }

    for name, vals in default_pollers.items():
        metrics_dict[f'battlebuddy_poller_active{{poller="{name}"}}'] = vals["active"]
        metrics_dict[f'battlebuddy_poller_consecutive_failures{{poller="{name}"}}'] = vals["failures"]
        metrics_dict[f'battlebuddy_poller_last_success_age_seconds{{poller="{name}"}}'] = vals["age_s"]

    spec = importlib.util.spec_from_file_location("ops_verify_poller_under_test", OPS_VERIFY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ops_verify_poller_under_test"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("ops_verify_poller_under_test", None)

    # Stub all non-poller gates to healthy
    monkeypatch.setattr(mod, "http_ok", lambda path: 200)
    monkeypatch.setattr(mod, "metrics", lambda: metrics_dict)
    monkeypatch.setattr(mod, "journal_tracebacks", lambda minutes=15: [])

    class _Cursor:
        def __init__(self):
            self.rows = [7, 2, 0, 0]
            self._i = 0

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            value = self.rows[self._i]
            self._i += 1
            return (value,)

        def close(self):
            pass

    class _Con:
        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    monkeypatch.setattr(mod.sqlite3, "connect", lambda *a, **k: _Con())

    # Stub regression results to not exist (so that gate fails, which is expected
    # and tested elsewhere; we only care about poller gates here)
    mod.REGRESSION_RESULTS_PATH = "/nonexistent/path"

    return mod


def _poller_gates(results):
    """Extract poller-related gates from results."""
    return {name: r for name, r in results.items() if name.startswith("poller ")}


def _fresh_gate_key(gates, poller_name):
    """Find the freshness gate key for a poller (includes threshold in name)."""
    matches = [k for k in gates if k.startswith(f"poller {poller_name} fresh")]
    assert matches, f"no fresh gate for {poller_name} in {list(gates.keys())}"
    return matches[0]


def _run_ops_verify(mod):
    """Run main() and return gates by name."""
    mod.main()
    return {r["gate"]: r for r in mod.RESULTS}


class TestPollerGatesPassWhenAllHealthy:
    """All poller gates pass when every poller is active, fresh, and failure-free."""

    def test_all_poller_gates_pass_on_healthy_metrics(self, monkeypatch):
        mod = _load_ops_verify(monkeypatch)
        gates = _poller_gates(_run_ops_verify(mod))

        assert gates, "no poller gates ran at all"
        failed = {n: r["detail"] for n, r in gates.items() if not r["pass"]}
        assert failed == {}, f"healthy pollers failed gates: {failed}"

        # Three gates per poller. The count is derived from the gate names rather
        # than asserted as a literal: the old comment read "8 pollers * 3 gates =
        # 24" and both halves were stale, because seven pollers start and the set is
        # now derived from the scrape. A hardcoded count is how a test and reality
        # drift apart without anyone noticing.
        gated = [n for n in gates if n.startswith("poller ")]
        poller_names = {n.split(" ", 2)[1] for n in gated}
        assert len(gates) == 3 * len(poller_names), (
            f"expected exactly 3 gates per poller for {sorted(poller_names)}, "
            f"got {len(gates)} gates: {sorted(gates)}"
        )

        # Check gate naming pattern
        for name in [
            "adsb-air-asset", "afd", "apd-cad", "apd_news",
            "atxfloods", "austin-events", "traffic-open-data",
        ]:
            assert f"poller {name} active" in gates
            # Freshness gate includes threshold in name
            fresh_gate = [k for k in gates if k.startswith(f"poller {name} fresh")]
            assert fresh_gate, f"missing fresh gate for {name}"
            assert f"poller {name} zero failures" in gates


class TestPollerGatesFailWhenPollerStops:
    """A stopped poller (active=0) is silent loss of coverage - the gate must fire."""

    def test_stopped_poller_fails_active_gate(self, monkeypatch):
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "apd_news": {"active": 0.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        active_gate = gates["poller apd_news active"]
        assert not active_gate["pass"], "stopped poller passed active gate"
        assert "STOPPED" in active_gate["detail"], active_gate["detail"]

    def test_stopped_poller_passes_other_gates_but_marked_unhealthy(self, monkeypatch):
        """Other gates for a stopped poller may pass (age=0, failures=0) but
        the overall health signal is captured by the active gate failing.
        """
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "apd_news": {"active": 0.0, "failures": 0.0, "age_s": 100.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        assert not gates["poller apd_news active"]["pass"]
        # Fresh and zero-failures gates can pass even if stopped - the active
        # gate is the one that catches the stopped condition
        fresh_gate = [k for k in gates if k.startswith("poller apd_news fresh")]
        assert fresh_gate and gates[fresh_gate[0]]["pass"]
        assert gates["poller apd_news zero failures"]["pass"]


class TestPollerGatesFailWhenPollerFailsRepeatedly:
    """Consecutive failures above threshold must fail the gate."""

    def test_high_consecutive_failures_fails_gate(self, monkeypatch):
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "traffic-open-data": {"failures": 5.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        fail_gate = gates["poller traffic-open-data zero failures"]
        assert not fail_gate["pass"], "high failure count passed zero-failures gate"
        assert "failures=5" in fail_gate["detail"], fail_gate["detail"]

    def test_threshold_boundary_at_3(self, monkeypatch):
        """Threshold is 3; 3 is amber in panels, but gate fails at >0."""
        # The gate is "zero failures" - any non-zero fails
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "afd": {"failures": 1.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        fail_gate = gates["poller afd zero failures"]
        assert not fail_gate["pass"], "1 failure passed zero-failures gate"


class TestPollerGatesFailWhenPollerStale:
    """Last success age exceeding threshold must fail the freshness gate."""

    def test_stale_poller_fails_freshness_gate(self, monkeypatch):
        """18h threshold - a poller that hasn't succeeded in 19h fails."""
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "austin-events": {"age_s": 19 * 3600.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        fresh_key = _fresh_gate_key(gates, "austin-events")
        fresh_gate = gates[fresh_key]
        assert not fresh_gate["pass"], "stale poller passed freshness gate"
        assert "age=" in fresh_gate["detail"], fresh_gate["detail"]

    def test_never_succeeded_age_minus_1_fails(self, monkeypatch):
        """age_s = -1 means never succeeded - must fail freshness gate."""
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "apd-cad": {"age_s": -1.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        fresh_key = _fresh_gate_key(gates, "apd-cad")
        fresh_gate = gates[fresh_key]
        assert not fresh_gate["pass"], "never-succeeded poller passed freshness gate"
        assert "age=-1" in fresh_gate["detail"], fresh_gate["detail"]

    def test_boundary_at_threshold(self, monkeypatch):
        """Exactly at 18h should pass (strictly less than threshold)."""
        # The gate checks: age_s >= 0 and age_s < POLLER_MAX_AGE_S
        # So 18h - 1s should pass, 18h should fail
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "atxfloods": {"age_s": 18 * 3600 - 1},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        fresh_key = _fresh_gate_key(gates, "atxfloods")
        fresh_gate = gates[fresh_key]
        assert fresh_gate["pass"], f"just-under-threshold failed: {fresh_gate['detail']}"

        mod2 = _load_ops_verify(monkeypatch, poller_metrics={
            "atxfloods": {"age_s": 18 * 3600},
        })
        gates2 = _poller_gates(_run_ops_verify(mod2))
        fresh_key2 = _fresh_gate_key(gates2, "atxfloods")
        fresh_gate2 = gates2[fresh_key2]
        assert not fresh_gate2["pass"], f"at-threshold passed: {fresh_gate2['detail']}"


class TestPollerGateThresholdsAgreeWithIntervals:
    """The freshness threshold (18h) must be >= 3x the longest poll interval.

    This is the property that prevents a poller from being born failing.
    """

    def test_freshness_threshold_exceeds_three_longest_intervals(self):
        """18h >= 3 * 6h (longest interval)."""
        from scripts.ops_verify import POLLER_MAX_AGE_S
        longest_interval = 6 * 3600  # austin-events, apd-cad
        assert POLLER_MAX_AGE_S >= 3 * longest_interval, (
            f"POLLER_MAX_AGE_S={POLLER_MAX_AGE_S} < 3*{longest_interval}="
            f"{3 * longest_interval}. A poller could be born failing."
        )


class TestPollerGateWitness:
    """Witnesses. A guard never shown failing might have stopped guarding.

    These tests prove the gates CAN fail - a check that cannot fail looks
    exactly like a check that passed, and that is the defect class this whole
    project is about.
    """

    def test_active_gate_can_fail(self, monkeypatch):
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "adsb-air-asset": {"active": 0.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))
        assert not gates["poller adsb-air-asset active"]["pass"]

    def test_zero_failures_gate_can_fail(self, monkeypatch):
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "afd": {"failures": 1.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))
        assert not gates["poller afd zero failures"]["pass"]

    def test_freshness_gate_can_fail(self, monkeypatch):
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "apd-cad": {"age_s": 100 * 3600},  # way over threshold
        })
        gates = _poller_gates(_run_ops_verify(mod))
        fresh_key = _fresh_gate_key(gates, "apd-cad")
        assert not gates[fresh_key]["pass"]

    def test_all_three_gates_can_fail_simultaneously(self, monkeypatch):
        """A completely dead poller fails all three gates."""
        mod = _load_ops_verify(monkeypatch, poller_metrics={
            "afd": {"active": 0.0, "failures": 10.0, "age_s": -1.0},
        })
        gates = _poller_gates(_run_ops_verify(mod))

        # Which poller went silent is not the point; the point is that a poller we
        # know about and cannot see must FAIL rather than pass. `afd` is used
        # because it genuinely starts in production. The previous version of this
        # test used reddit-intel, whose .start() is commented out -- with the set
        # derived it is simply not gated, which is the correct behaviour and made
        # the old assertion meaningless.
        assert not gates["poller afd active"]["pass"]
        assert not gates["poller afd zero failures"]["pass"]
        fresh_key = _fresh_gate_key(gates, "afd")
        assert not gates[fresh_key]["pass"]


class TestPollerHealthFunction:
    """Unit tests for the poller_health extractor function."""

    def test_missing_metric_is_reported_unhealthy(self, monkeypatch):
        """A poller whose metrics are absent from /metrics is reported as unhealthy."""
        # This test used to call `_load_ops_verify(...)` and bind the result to
        # `mod`, then never use it -- the assertions go through a direct
        # `import scripts.ops_verify`. Dead setup is worse than none: it reads as
        # though the stubbed loader mattered here.

        # Manually construct a metrics dict WITHOUT one poller's metrics
        import scripts.ops_verify as ov
        metrics = {
            "battlebuddy_backlog_queue_depth": 0.0,
            "battlebuddy_active_incidents": 1.0,
            "battlebuddy_homicides_seed_error": 0.0,
            "battlebuddy_homicides_seed_newest_ts": time.time() - 86400,
        }
        # Only add some pollers
        for name in ["adsb-air-asset", "afd"]:
            metrics[f'battlebuddy_poller_active{{poller="{name}"}}'] = 1.0
            metrics[f'battlebuddy_poller_consecutive_failures{{poller="{name}"}}'] = 0.0
            metrics[f'battlebuddy_poller_last_success_age_seconds{{poller="{name}"}}'] = 100.0

        # Names passed explicitly: this models a poller we KNEW existed whose
        # metrics have vanished mid-run, which is a failure. Deriving from the
        # metrics alone would simply not know about apd_news -- the other case,
        # covered by the test below.
        health = ov.poller_health(
            metrics, names=["adsb-air-asset", "afd", "apd_news"])

        # The missing poller should be reported as unhealthy
        assert not health["apd_news"]["active"]
        assert health["apd_news"]["failures"] > ov.POLLER_MAX_FAILURES
        assert health["apd_news"]["age_s"] == float("inf")

    def test_poller_absent_from_scrape_is_not_invented(self, monkeypatch):
        """A poller that was never started is absent, not failing.

        Deriving the set from the scrape is what stops a disabled poller leaving a
        permanently red gate behind. The deliberate cost: a poller that never
        started is invisible -- correct, because it is not failing, it is not
        running.
        """
        ov = _load_ops_verify(monkeypatch)
        metrics = {
            'battlebuddy_poller_active{poller="afd"}': 1.0,
            'battlebuddy_poller_consecutive_failures{poller="afd"}': 0.0,
            'battlebuddy_poller_last_success_age_seconds{poller="afd"}': 100.0,
        }
        assert ov.poller_names(metrics.keys()) == ["afd"]
        assert set(ov.poller_health(metrics)) == {"afd"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])