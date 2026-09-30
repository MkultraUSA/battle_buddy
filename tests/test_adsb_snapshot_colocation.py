"""The ADS-B feeder must post to the process that serves the aircraft map.

`/api/adsb/live` does not read a database. It returns `modules.aircraft._snapshot`,
a module-level dict held in ONE process's memory, and `POST /api/adsb/ingest` is
the only thing that writes it. So the map renders only when the feeder posts to
the same process that nginx proxies the live endpoint to.

This was false. `adsb_selfeed.py` hardcoded `http://127.0.0.1:5000/api/adsb/ingest`,
and port 5000 was a second Flask app (`app.py`, the retired Overwatch dashboard)
that also did `app.register_blueprint(aircraft_bp)`. nginx proxied public
`/api/adsb/live` to that same port, so the map rendered 1,391 fine requests from
Overwatch's memory while the main app on :9001 answered
`{"aircraft": [], "stale": true}` every time. That is the "ADS-B degraded / air
asset tracking effectively dark" item in the 2026-09-30 handoff, filed as its own
unexplained mystery. Root cause: never an upstream feed problem.

## What these tests do and do not claim

An earlier draft of this file asserted "ingest and live share a blueprint, so any
process serving one serves the other, so co-location holds by construction". That
was a non-sequitur and review caught it: `app.py` registered the *same* blueprint,
so two processes each served both endpoints from two different `_snapshot` dicts.
Sharing a blueprint is a refactoring guard, not a co-location guard.

The guard that actually addresses the failure is `test_only_one_module_registers_
the_aircraft_blueprint` -- the bug was a second registrant. It is enforced from the
moment Overwatch's `app.py` is removed; until then it asserts the current, known-
imperfect state so the remaining duplication is visible rather than silent.

The other thing that makes co-location true -- nginx proxying `/api/adsb/` to the
main app -- is a VPS-only config with no representation in this repository, so no
test here can assert it. It is verified by the deploy procedure instead.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import unittest
from unittest import mock

_ROOT = pathlib.Path(__file__).parent.parent

# audio_receiver.py's `--port` default, and therefore the port nginx proxies
# /api/adsb/ to. scripts/backlog_agent.py uses the same house pattern.
MAIN_APP_PORT = 9001

# The retired second process, named only so failure messages can identify it.
RETIRED_PORT = 5000

# File suffixes worth sweeping for a port reference: not just Python. The one
# real out-of-tree instance was supervisor/battlebuddy.conf, and that file also
# spelled the port `--port=5000`, which a host:port pattern cannot see.
_SWEEP_SUFFIXES = {".py", ".conf", ".sh", ".service", ".md", ".example", ".env", ".cfg"}

_SOURCES = sorted(
    p for p in _ROOT.rglob("*")
    if p.is_file() and p.suffix in _SWEEP_SUFFIXES
    and not any(part in {".git", "venv", "__pycache__", "node_modules"} for part in p.parts)
)

# URL form requires `://` before the authority, which is what keeps a slice
# bound like `rows[1:5000]` or `[:5000]` from matching -- those have no scheme.
# The flag form catches `--port=5000`, `--port 5000` and `port=5000`, including
# supervisor configs and `app.run(port=...)`, which a host:port pattern misses.
_PORT_RE = re.compile(
    rf"://[^/\s]*:{RETIRED_PORT}\b"
    rf"|(?:--port[= ]|port=){RETIRED_PORT}\b"
)


def _blueprint_rules(blueprint: str) -> set[str]:
    """Route rules declared on `blueprint` in modules/aircraft.py."""
    tree = ast.parse((_ROOT / "modules" / "aircraft.py").read_text())
    rules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "route"
                and isinstance(deco.func.value, ast.Name)
                and deco.func.value.id == blueprint
                and deco.args
                and isinstance(deco.args[0], ast.Constant)
            ):
                rules.add(deco.args[0].value)
    return rules


def _blueprint_registrants(blueprint: str) -> set[str]:
    """Modules that call register_blueprint(<blueprint>)."""
    found: set[str] = set()
    for path in _SOURCES:
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_blueprint"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == blueprint
            ):
                found.add(str(path.relative_to(_ROOT)))
    return found


class TestFeederTargetsTheMainApp(unittest.TestCase):
    """The feeder's default must be the main app's ingest endpoint."""

    def test_default_ingest_url_points_at_the_main_app(self):
        import adsb_selfeed

        # Hermetic: assert the DEFAULT, not whatever this environment exports.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                adsb_selfeed.ingest_url(),
                f"http://127.0.0.1:{MAIN_APP_PORT}/api/adsb/ingest",
            )

    def test_ingest_url_reads_the_environment_at_call_time(self):
        """load_env() runs inside main(); an import-time constant would ignore it.

        Verified by mutation -- freezing the read at import fails exactly here.
        """
        import adsb_selfeed

        with mock.patch.dict(os.environ, {"BB_ADSB_INGEST_URL": "http://elsewhere/ingest"}):
            self.assertEqual(adsb_selfeed.ingest_url(), "http://elsewhere/ingest")

    def test_main_app_default_port_matches_the_feeder_target(self):
        """The feeder's port and the app's --port default must not drift apart.

        This is the repo-side half of the deployment contract; nginx is the
        other half and lives only on the VPS.
        """
        import adsb_selfeed

        receiver = (_ROOT / "audio_receiver.py").read_text()
        match = re.search(r'"--port"\s*,\s*type=int\s*,\s*default=(\d+)', receiver)
        self.assertIsNotNone(match, "could not find audio_receiver.py --port default")
        self.assertEqual(
            int(match.group(1)), MAIN_APP_PORT,
            "audio_receiver.py's default port moved; update MAIN_APP_PORT and the "
            "nginx location /api/adsb/ proxy_pass together",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn(f":{match.group(1)}", adsb_selfeed.ingest_url())


class TestAircraftRoutesShareOneBlueprint(unittest.TestCase):
    """Refactoring guard. This alone does NOT prove co-location -- see module docstring."""

    def test_ingest_and_live_are_both_declared_on_the_aircraft_blueprint(self):
        rules = _blueprint_rules("aircraft_bp")
        for rule in ("/api/adsb/ingest", "/api/adsb/live"):
            self.assertIn(rule, rules, f"{rule} is not declared on aircraft_bp")


class TestOnlyOneModuleRegistersTheBlueprint(unittest.TestCase):
    """Two registrants means two _snapshot dicts, which is how this bug happened."""

    def test_audio_receiver_registers_the_aircraft_blueprint(self):
        self.assertIn("audio_receiver.py", _blueprint_registrants("aircraft_bp"))

    def test_only_one_module_registers_the_aircraft_blueprint(self):
        registrants = _blueprint_registrants("aircraft_bp")
        self.assertEqual(
            registrants, {"audio_receiver.py"},
            "more than one module registers aircraft_bp, so more than one process "
            f"holds its own _snapshot and the map may be served by one process "
            f"while another is fed: {sorted(registrants)}",
        )


class TestNothingPointsAtTheRetiredProcess(unittest.TestCase):
    """A surviving :5000 reference is a latent outage or an unintended listener."""

    def test_no_tracked_file_references_the_retired_port(self):
        this_file = pathlib.Path(__file__).resolve()
        offenders = []
        for path in _SOURCES:
            if path.resolve() == this_file:
                continue  # this file names the port on purpose, to document it
            text = path.read_text(errors="replace")
            for match in _PORT_RE.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(_ROOT)}:{line}")
        self.assertEqual(
            offenders, [],
            "still references the retired :5000 process: " + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()