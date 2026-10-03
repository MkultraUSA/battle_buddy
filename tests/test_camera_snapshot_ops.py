"""The camera snapshot has to refresh itself, and something has to notice if it stops.

Three things are pinned here, and they are the three ways this could silently
rot:

  * **the snapshot write is atomic.** The file is served straight off disk to
    every map load, and it now gets rewritten by a timer while people are
    looking at the map. A non-atomic write means a reader can catch it
    half-written and the entire layer vanishes -- map still draws, legend still
    claims cameras, no error anywhere.
  * **ops_verify actually fails when the snapshot goes bad.** Not "the gate
    function exists". These run `main()` and read the gate results, because the
    project has already shipped two silent failures of exactly this shape: a
    watcher querying three metric names the app never emitted, and an
    `init_db()` that left `/metrics` serving 200 lines of nothing.
  * **the refresh period and the staleness budget agree.** A weekly timer with
    a 30-hour freshness gate would be born failing; a daily timer with a
    10-hour gate would never fire. Nothing else in the repo relates those two
    numbers, so this does.

The systemd units are asserted structurally (a unit file is config, not code,
but a typo in `OnCalendar` or a missing `Persistent=true` is as invisible as a
bug and as expensive).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FETCHER = ROOT / "scripts" / "fetch_austin_cameras.py"
OPS_VERIFY = ROOT / "scripts" / "ops_verify.py"
SYSTEMD = ROOT / "systemd"


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("_fetcher_under_test", FETCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_ops_verify(monkeypatch, repo_root):
    """Import ops_verify.py with a faked systemctl so the env load is inert."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else "", 0, "", ""),
    )
    spec = importlib.util.spec_from_file_location("ops_verify_cam_under_test", OPS_VERIFY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ops_verify_cam_under_test"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("ops_verify_cam_under_test", None)
    mod._REPO_ROOT = str(repo_root)
    return mod


# ---------------------------------------------------------------------------
# Atomic snapshot write
# ---------------------------------------------------------------------------


class TestSnapshotWriteIsAtomic:
    """The file is served live to browsers, so a torn read empties the map."""

    def _snapshot(self, n=3, image=True):
        return {
            "type": "FeatureCollection",
            "generated": "2026-10-02T16:33:29Z",
            "source": "City of Austin Open Data b4k4-adkb",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "id": str(i),
                        "name": f"CAMERA {i}",
                        **({"image": f"https://cctv.austinmobility.io/image/{i}.jpg"}
                           if image else {}),
                    },
                    "geometry": {"type": "Point",
                                 "coordinates": [-97.7 + i / 1000, 30.26]},
                }
                for i in range(n)
            ],
        }

    def test_it_writes_parseable_json(self, tmp_path, monkeypatch):
        mod = _load_fetcher()
        out = tmp_path / "austin_cameras.json"
        monkeypatch.setattr(mod, "OUT_PATH", out)

        mod.write_snapshot(self._snapshot(n=5))

        data = json.loads(out.read_text())
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 5

    def test_no_temp_file_is_left_behind(self, tmp_path, monkeypatch):
        """A stray .tmp in static/data is a file nginx could later serve."""
        mod = _load_fetcher()
        monkeypatch.setattr(mod, "OUT_PATH", tmp_path / "austin_cameras.json")

        mod.write_snapshot(self._snapshot())

        assert sorted(p.name for p in tmp_path.iterdir()) == ["austin_cameras.json"]

    def test_the_previous_snapshot_survives_a_failed_write(self, tmp_path, monkeypatch):
        """The property that matters: a bad run must not destroy a good file.

        Every one of the fetcher's refusals exists to keep the last good
        snapshot in place. An atomic-replace write preserves that property even
        if the failure happens *during* the write rather than before it, which a
        truncate-then-write `write_text` does not.
        """
        mod = _load_fetcher()
        out = tmp_path / "austin_cameras.json"
        monkeypatch.setattr(mod, "OUT_PATH", out)

        good = self._snapshot(n=7)
        mod.write_snapshot(good)

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            mod.write_snapshot(self._snapshot(n=1))

        # The old file is still there, still complete, still the old one.
        assert json.loads(out.read_text())["features"] == good["features"]
        # And no debris.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["austin_cameras.json"]

    def test_the_file_is_world_readable(self, tmp_path, monkeypatch):
        """mkstemp creates 0600; this is public static data served to everyone."""
        mod = _load_fetcher()
        out = tmp_path / "austin_cameras.json"
        monkeypatch.setattr(mod, "OUT_PATH", out)

        mod.write_snapshot(self._snapshot())

        assert (out.stat().st_mode & 0o777) == 0o644

    def test_the_writer_is_actually_used_by_main(self):
        """Guard against the atomic writer being added and never called."""
        source = FETCHER.read_text(encoding="utf-8")
        main_body = source[source.index("def main("):]
        assert "write_snapshot(snapshot)" in main_body
        assert "OUT_PATH.write_text" not in main_body, (
            "main() writes the snapshot directly again, which reintroduces the "
            "torn-read window"
        )


# ---------------------------------------------------------------------------
# ops_verify camera gates
# ---------------------------------------------------------------------------


def _write_snapshot(root: Path, *, generated: str, n: int = 820, image: bool = True):
    snap_dir = root / "static" / "data"
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / "austin_cameras.json"
    features = []
    for i in range(n):
        props = {"id": str(i), "name": f"CAMERA {i}"}
        if image:
            props["image"] = f"https://cctv.austinmobility.io/image/{i}.jpg"
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [-97.8, 30.26]},
        })
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "generated": generated,
        "source": "City of Austin Open Data b4k4-adkb",
        "features": features,
    }))
    return path


def _fresh_stamp():
    return (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _run_ops_verify(mod, monkeypatch, *, frame_status=200):
    """Run main() with every non-camera input stubbed out.

    The camera gates are the subject, so the HTTP surface, /metrics, the
    database and the journal are all faked to healthy. Any FAIL in the result
    is then attributable to the snapshot.
    """
    monkeypatch.setattr(mod, "http_ok", lambda path: 200)
    monkeypatch.setattr(mod, "metrics", lambda: {
        "battlebuddy_backlog_queue_depth": 0.0,
        "battlebuddy_active_incidents": 1.0,
        "battlebuddy_homicides_seed_error": 0.0,
        "battlebuddy_homicides_seed_newest_ts": time.time() - 86400,
    })
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

    def fake_urlopen(req, timeout=None):
        if getattr(req, "method", None) == "HEAD":
            return _FrameResp(frame_status)
        raise AssertionError(f"unexpected network call: {req}")

    class _FrameResp:
        def __init__(self, status):
            self.status = status
            self.headers = {"Content-Type": "image/jpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod.main()
    return {r["gate"]: r for r in mod.RESULTS}


def _camera_gates(results):
    # Match on the word anywhere, not a prefix: the frame-rule gate is named
    # "every plotted camera has a published frame", and a prefix filter would
    # have quietly dropped the very gate these tests exist to prove fires.
    return {name: r for name, r in results.items() if "camera" in name}


class TestCameraGatesPassOnAGoodSnapshot:
    def test_a_fresh_complete_snapshot_passes_every_camera_gate(self, tmp_path,
                                                                  monkeypatch):
        _write_snapshot(tmp_path, generated=_fresh_stamp())
        mod = _load_ops_verify(monkeypatch, tmp_path)

        gates = _camera_gates(_run_ops_verify(mod, monkeypatch))

        assert gates, "no camera gates ran at all"
        failed = {n: r["detail"] for n, r in gates.items() if not r["pass"]}
        assert failed == {}, f"a healthy snapshot failed: {failed}"

    def test_the_frame_probe_uses_a_few_cameras_not_all_of_them(self, tmp_path,
                                                                monkeypatch):
        """820 third-party requests per ops run is not a health check."""
        _write_snapshot(tmp_path, generated=_fresh_stamp())
        mod = _load_ops_verify(monkeypatch, tmp_path)

        seen = []
        real = urllib.request.urlopen

        def counting(req, timeout=None):
            if getattr(req, "method", None) == "HEAD":
                seen.append(req.full_url)
                return _Head200()
            return real(req, timeout=timeout)

        class _Head200:
            status = 200
            headers = {"Content-Type": "image/jpeg"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(mod.urllib.request, "urlopen", counting)
        monkeypatch.setattr(mod, "http_ok", lambda p: 200)
        monkeypatch.setattr(mod, "journal_tracebacks", lambda minutes=15: [])
        monkeypatch.setattr(mod, "metrics", lambda: {})
        mod.main()

        assert 0 < len(seen) <= 5, f"probed {len(seen)} cameras; this is a spot check"


class TestCameraGatesFailWhenTheSnapshotGoesBad:
    """Each of these is a way the layer rots with nothing else complaining."""

    def test_a_missing_snapshot_fails(self, tmp_path, monkeypatch):
        mod = _load_ops_verify(monkeypatch, tmp_path)
        gates = _camera_gates(_run_ops_verify(mod, monkeypatch))

        present = gates["camera snapshot present"]
        assert not present["pass"]
        assert "missing" in present["detail"], present["detail"]

    def test_a_frozen_snapshot_fails_the_freshness_gate(self, tmp_path, monkeypatch):
        """The exact failure the timer is meant to prevent, asserted directly."""
        frozen = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        _write_snapshot(tmp_path, generated=frozen)
        mod = _load_ops_verify(monkeypatch, tmp_path)

        gates = _camera_gates(_run_ops_verify(mod, monkeypatch))

        fresh = [r for n, r in gates.items() if "fresh" in n]
        assert fresh, "there is no freshness gate"
        assert not fresh[0]["pass"]
        assert "age=" in fresh[0]["detail"]

    def test_a_collapsed_camera_list_fails_the_count_gate(self, tmp_path, monkeypatch):
        """A renamed column or moved portal would empty the map, silently."""
        _write_snapshot(tmp_path, generated=_fresh_stamp(), n=12)
        mod = _load_ops_verify(monkeypatch, tmp_path)

        gates = _camera_gates(_run_ops_verify(mod, monkeypatch))

        count = [r for n, r in gates.items() if "count" in n]
        assert count and not count[0]["pass"], count

    def test_a_frameless_camera_fails_kevins_rule(self, tmp_path, monkeypatch):
        """'If it doesn't have video or a picture, there is no reason to have it
        on the map' -- the gate has to hold the fetcher to that, not just the
        fetcher's own unit test."""
        _write_snapshot(tmp_path, generated=_fresh_stamp(), n=600, image=False)
        mod = _load_ops_verify(monkeypatch, tmp_path)

        gates = _camera_gates(_run_ops_verify(mod, monkeypatch))

        frame_rule = [r for n, r in gates.items() if "published frame" in n]
        assert frame_rule and not frame_rule[0]["pass"], frame_rule

    def test_a_dead_frame_host_is_reported(self, tmp_path, monkeypatch):
        """The snapshot can be perfect while the images 404 -- the popups break
        and the file never changes, so only an outbound probe catches it."""
        _write_snapshot(tmp_path, generated=_fresh_stamp())
        mod = _load_ops_verify(monkeypatch, tmp_path)

        gates = _camera_gates(_run_ops_verify(mod, monkeypatch, frame_status=404))

        reach = [r for n, r in gates.items() if "reachable" in n]
        assert reach and not reach[0]["pass"], reach
        assert "404" in reach[0]["detail"], reach[0]["detail"]


class TestSnapshotReaderReportsWhyItFailed:
    def test_malformed_json_is_reported_not_raised(self, tmp_path, monkeypatch):
        d = tmp_path / "static" / "data"
        d.mkdir(parents=True)
        (d / "austin_cameras.json").write_text("{not json")
        mod = _load_ops_verify(monkeypatch, tmp_path)

        data, why = mod.camera_snapshot()

        assert data is None
        assert "unreadable" in why

    def test_a_snapshot_without_features_is_reported(self, tmp_path, monkeypatch):
        d = tmp_path / "static" / "data"
        d.mkdir(parents=True)
        (d / "austin_cameras.json").write_text('{"type": "FeatureCollection"}')
        mod = _load_ops_verify(monkeypatch, tmp_path)

        data, why = mod.camera_snapshot()

        assert data is None
        assert "features" in why

    def test_an_explicit_null_does_not_crash_the_gates(self, tmp_path, monkeypatch):
        """`properties: null` is valid JSON and `.get(k, {})` does not cover it.

        The gate walks every feature, so an explicit null would otherwise raise
        TypeError and take the whole ops verification down -- turning a
        malformed snapshot into a missing health report.
        """
        d = tmp_path / "static" / "data"
        d.mkdir(parents=True)
        (d / "austin_cameras.json").write_text(json.dumps({
            "type": "FeatureCollection",
            "generated": _fresh_stamp(),
            "features": [{"type": "Feature", "properties": None,
                          "geometry": {"type": "Point",
                                       "coordinates": [-97.7, 30.26]}}] * 600,
        }))
        mod = _load_ops_verify(monkeypatch, tmp_path)

        gates = _camera_gates(_run_ops_verify(mod, monkeypatch))

        rule = [r for n, r in gates.items() if "published frame" in n]
        assert rule, "the frame-rule gate never ran"
        assert not rule[0]["pass"], "null properties should read as frameless"

    def test_an_unparseable_generated_stamp_is_reported(self, tmp_path, monkeypatch):
        _write_snapshot(tmp_path, generated="yesterday")
        mod = _load_ops_verify(monkeypatch, tmp_path)

        gates = _camera_gates(_run_ops_verify(mod, monkeypatch))

        fresh = [r for n, r in gates.items() if "fresh" in n][0]
        assert not fresh["pass"]
        assert "unparseable" in fresh["detail"]


class TestFrameProbeUsesHead:
    def test_a_200_image_passes(self):
        mod = _load_fetcher()  # noqa: F841 - just proving imports stay clean
        ops = importlib.util.spec_from_file_location("_ov", OPS_VERIFY)
        m = importlib.util.module_from_spec(ops)
        ops.loader.exec_module(m)

        class _Resp:
            status = 200
            headers = {"Content-Type": "image/jpeg"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: _Resp()
        try:
            assert m.camera_frame_ok("https://cctv.austinmobility.io/image/1.jpg") == ""
        finally:
            urllib.request.urlopen = original

    def test_a_non_image_content_type_fails(self):
        ops = importlib.util.spec_from_file_location("_ov2", OPS_VERIFY)
        m = importlib.util.module_from_spec(ops)
        ops.loader.exec_module(m)

        class _Resp:
            status = 200
            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: _Resp()
        try:
            assert "content-type" in m.camera_frame_ok("https://x/image/1.jpg")
        finally:
            urllib.request.urlopen = original


# ---------------------------------------------------------------------------
# The units themselves
# ---------------------------------------------------------------------------


class TestTheSLOAlertCountMatchesReality:
    """ops_verify emits 19 gates; the Telegram SLO page says a hardcoded number.

    It said 13 for months after the camera gates landed, so an operator reading
    a breach page was told a smaller number than the run actually produced.
    Counted by running main(), not by counting gate() call sites: one of those
    sites is inside the HTTP-surface loop and emits five gates, so the static
    count comes out at 18 and would be its own quiet lie.
    """

    def test_the_number_in_the_alert_is_the_number_of_gates_that_run(
        self, tmp_path, monkeypatch
    ):
        _write_snapshot(tmp_path, generated=_fresh_stamp())
        mod = _load_ops_verify(monkeypatch, tmp_path)
        _run_ops_verify(mod, monkeypatch)

        ran = len(mod.RESULTS)
        assert ran >= 19, f"expected the camera gates to be present, got {ran}"

        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        m = re.search(r"SLOs green \((\d+) gates\)", workflow)
        assert m, "the SLO notifier no longer states a gate count"
        assert int(m.group(1)) == ran, (
            f"the SLO page claims {m.group(1)} gates but ops_verify.py ran "
            f"{ran}"
        )


class TestTheRefreshIsScheduled:
    SERVICE = SYSTEMD / "bb-camera-snapshot.service"
    TIMER = SYSTEMD / "bb-camera-snapshot.timer"

    def _read(self, path):
        assert path.exists(), f"{path} is missing; the snapshot never refreshes"
        return path.read_text(encoding="utf-8")

    def test_both_units_are_present_and_named_as_a_pair(self):
        assert self.SERVICE.exists()
        assert self.TIMER.exists()
        assert self.SERVICE.stem == self.TIMER.stem

    def test_the_service_runs_the_fetcher(self):
        unit = self._read(self.SERVICE)
        assert "scripts/fetch_austin_cameras.py" in unit
        assert "Type=oneshot" in unit
        assert "network-online.target" in unit, (
            "without waiting for the network the first fetch of every boot "
            "fails and the timer just retries tomorrow"
        )

    def test_the_service_uses_a_python_that_is_not_the_app_venv(self):
        """The fetcher is stdlib-only; tying it to the venv means a dependency
        upgrade can silently stop the camera layer from refreshing."""
        unit = self._read(self.SERVICE)
        # Only the command matters -- the unit explains in a comment why the
        # venv is wrong, and reading that back as a violation is noise.
        execstart = [ln for ln in unit.splitlines()
                     if ln.strip().startswith("ExecStart=")]
        assert execstart, "the service has no ExecStart"
        assert all("/venv" not in ln for ln in execstart), (
            f"ExecStart uses the app venv: {execstart}"
        )
        assert re.search(r"ExecStart=/usr/bin/python\d*\s", unit), (
            "expected the system interpreter on ExecStart"
        )

    def test_the_timer_runs_daily(self):
        unit = self._read(self.TIMER)
        assert "OnCalendar=" in unit
        assert re.search(r"OnCalendar=\*-\*-\*\s+\d{2}:\d{2}:\d{2}", unit), (
            "the daily marker is gone, or it fires more than once a day"
        )

    def test_the_timer_states_its_timezone(self):
        """The host runs Europe/Berlin. An unqualified OnCalendar moves an hour
        twice a year and quietly drifts; nobody would notice for months."""
        unit = self._read(self.TIMER)
        oncalendar = [ln for ln in unit.splitlines()
                      if ln.strip().startswith("OnCalendar=")]
        assert oncalendar, "no OnCalendar line"
        assert all("UTC" in ln for ln in oncalendar), (
            f"OnCalendar must pin UTC explicitly, got: {oncalendar}"
        )

    def test_the_timer_survives_a_host_that_was_off(self):
        unit = self._read(self.TIMER)
        # Match real directives, not substrings: commenting a line out of a unit
        # file is the easiest way to disable it, and `"OnBootSec=" in unit`
        # happily accepts `# OnBootSec=10min`. That is an assertion that pins
        # the text rather than the behaviour.
        directives = {ln.split("=", 1)[0].strip() for ln in unit.splitlines()
                      if ln.strip() and not ln.strip().startswith(("#", "["))}
        assert "Persistent" in directives, (
            "a missed run would leave the snapshot frozen until the next "
            "scheduled tick, and nothing would say so"
        )
        assert any(d.startswith("Persistent") and "true" in ln
                   for d, ln in ((ln.split("=", 1)[0].strip(), ln)
                                 for ln in unit.splitlines()
                                 if ln.strip().startswith("Persistent="))), (
            "Persistent must be true, not just present"
        )
        assert "OnBootSec" in directives, (
            "a rebuilt host would show an empty camera layer until the next "
            "daily tick"
        )

    def test_the_timer_is_jittered(self):
        """Every deployment of this service would otherwise hit the city's
        portal on the same second."""
        assert "RandomizedDelaySec=" in self._read(self.TIMER)


class TestTheScheduleAndTheStalenessBudgetAgree:
    """Nothing else relates these two numbers, so nothing else would catch it."""

    def _ops_max_age_hours(self):
        source = OPS_VERIFY.read_text(encoding="utf-8")
        m = re.search(r"CAMERA_SNAPSHOT_MAX_AGE_S\s*=\s*(\d+)\s*\*\s*(\d+)", source)
        assert m, "CAMERA_SNAPSHOT_MAX_AGE_S is gone; the freshness gate is gone"
        return int(m.group(1)) * int(m.group(2)) / 3600

    def _timer_period_hours(self):
        unit = (SYSTEMD / "bb-camera-snapshot.timer").read_text(encoding="utf-8")
        m = re.search(r"OnCalendar=\*-\*-\*", unit)
        assert m, "expected a daily OnCalendar"
        return 24.0

    def test_the_gate_tolerates_one_scheduled_run_plus_jitter(self):
        budget = self._ops_max_age_hours()
        period = self._timer_period_hours()
        unit = (SYSTEMD / "bb-camera-snapshot.timer").read_text(encoding="utf-8")
        jitter = re.search(r"RandomizedDelaySec=(\d+)m", unit)
        jitter_h = int(jitter.group(1)) / 60 if jitter else 0.0

        worst_case = period + jitter_h
        assert budget > worst_case, (
            f"freshness budget {budget}h cannot survive a normal run "
            f"({period}h + {jitter_h}h jitter), so the gate would fire on a "
            "perfectly healthy snapshot"
        )

    def test_the_gate_still_catches_a_missed_run(self):
        budget = self._ops_max_age_hours()
        assert budget < 48, (
            f"a {budget}h budget would stay green through an entire missed "
            "day, which is the failure this gate exists for"
        )

    def test_the_count_floor_matches_the_test_suite(self):
        """ops_verify and the camera tests must agree on 'implausibly few'."""
        ops = OPS_VERIFY.read_text(encoding="utf-8")
        suite = (ROOT / "tests" / "test_camera_layer.py").read_text(encoding="utf-8")
        ops_floor = int(re.search(r"CAMERA_MIN_COUNT\s*=\s*(\d+)", ops).group(1))
        suite_floor = int(re.search(r"self\.assertGreater\(\s*\n?\s*len\([^)]*\),\s*(\d+)",
                                    suite).group(1))
        assert ops_floor == suite_floor, (
            f"ops_verify wants >{ops_floor} cameras, the suite wants "
            f">{suite_floor}; they should not drift apart"
        )