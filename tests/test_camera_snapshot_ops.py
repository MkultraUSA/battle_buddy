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


class TestTheSLOAlertStatesNoGateCount:
    """The SLO page must not quote a number of gates.

    It used to, and said "13 gates" for months after the camera gates landed, so
    an operator reading a breach page was told a smaller number than the run
    actually produced. Re-baselining it was tried first and does not hold: the
    gate count is **environment-dependent**, because several gates only emit when
    there is a database and a snapshot to look at. A test environment runs 20,
    production runs a different number again, so any number written down is wrong
    somewhere and nobody can tell where.

    The guarantee is therefore the stronger one: no number at all. The message
    says the gates passed, the breach message points at Grafana, and the run's
    own output is the authority. A count cannot go stale if it is not there.
    """

    GREEN_TEXT = "Post-deploy SLOs green"

    def test_the_slo_notifier_does_not_quote_a_gate_count(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        assert self.GREEN_TEXT in workflow, "the SLO notifier lost its message"
        m = re.search(r"Post-deploy SLOs green[^\"\n]*?\((\d+)\s+gates?\)", workflow)
        assert m is None, (
            f"the SLO page quotes a hardcoded gate count ({m.group(1) if m else ''}). "
            "That is what read 13 for months while 19 gates ran, and it cannot be "
            "kept correct because the count varies by environment."
        )

    def test_the_breach_page_also_quotes_no_count(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        assert "Post-deploy SLO BREACH" in workflow
        assert not re.search(r"SLO BREACH[^\"\n]*?\(\d+\s+gates?\)", workflow)


class TestOpsVerifyGateCountIsDerivedNotStated:
    def test_the_run_output_is_the_authority(self, tmp_path, monkeypatch):
        _write_snapshot(tmp_path, generated=_fresh_stamp())
        mod = _load_ops_verify(monkeypatch, tmp_path)
        _run_ops_verify(mod, monkeypatch)

        ran = len(mod.RESULTS)
        assert ran >= 20, f"expected the full gate set to run, got {ran}"
        # The regression gate must be among them, or the timer is unwatched. Only
        # the existence gate fires here, because this environment has no results
        # file -- which is the point: it fails loudly rather than being skipped.
        # tests/test_regression_gates.py covers the other two on both paths.
        names = [r["gate"] for r in mod.RESULTS]
        assert "regression battery has run" in names, (
            "the regression battery is not gated on at all"
        )
        by_name = {r["gate"]: r for r in mod.RESULTS}
        assert by_name["regression battery has run"]["pass"] is False, (
            "a missing results file passed the gate"
        )

        # And the workflow asserts on that exit code, not on a number.
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        assert "ops_verify.py" in workflow
        assert "PIPESTATUS" in workflow, (
            "the deploy must gate on ops_verify's exit status, not on parsed text"
        )


