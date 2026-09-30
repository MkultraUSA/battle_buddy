"""The ADS-B feeder and the aircraft map must live in the same process.

`/api/adsb/live` does not read a database. It returns `modules.aircraft._snapshot`,
a module-level dict held in ONE process's memory, and the only thing that writes
it is `POST /api/adsb/ingest`. So the map shows aircraft exactly when the feeder
happens to be posting to the same process that serves the map.

This used to be false. `adsb_selfeed.py` had `http://127.0.0.1:5000/api/adsb/ingest`
hardcoded -- port 5000 was a second Flask app (`app.py`, the retired Overwatch
tactical dashboard) that registered `aircraft_bp` as well. nginx proxied public
`/api/adsb/live` to that same port, so the map looked fine for 1,391 requests
while the main app on :9001 returned `{"aircraft": [], "stale": true}` forever.
That is the "ADS-B degraded / air-asset tracking effectively dark" item in the
2026-09-30 handoff, filed as its own unexplained mystery.

The invariant these tests pin, in order of how much it actually prevents:

  1. ingest and live are on the SAME blueprint, so any process serving one serves
     the other -- co-location holds by construction, not by configuration luck;
  2. the feeder's default target is the main app's port;
  3. nothing in the tree still points at the retired :5000 process.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import unittest
from unittest import mock

_ROOT = pathlib.Path(__file__).parent.parent

# The main app's port. audio_receiver.py `--port` default; the house pattern for
# loopback callers is scripts/backlog_agent.py BB_BACKLOG_BASE_URL.
MAIN_APP_PORT = 9001

# The retired second process. Named only so the failure message can identify it.
RETIRED_PORT = 5000

# Python sources that talk to the local Battle Buddy. app.py is included on
# purpose: it was the process that used to own the snapshot.
_SOURCES = sorted(
    p for p in _ROOT.rglob("*.py")
    if ".git" not in p.parts and "venv" not in p.parts and "__pycache__" not in p.parts
)


def _blueprint_routes(blueprint: str) -> dict[str, str]:
    """Every route declared on `blueprint` in modules/aircraft.py, rule -> handler."""
    tree = ast.parse((_ROOT / "modules" / "aircraft.py").read_text())
    routes: dict[str, str] = {}
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
                routes[deco.args[0].value] = node.name
    return routes


class TestIngestAndLiveAreCoLocated(unittest.TestCase):
    """Ingest and live share a blueprint, so the snapshot is always readable."""

    def test_ingest_and_live_are_both_on_the_aircraft_blueprint(self):
        routes = _blueprint_routes("aircraft_bp")
        for rule in ("/api/adsb/ingest", "/api/adsb/live"):
            self.assertIn(
                rule, routes,
                f"{rule} is not declared on aircraft_bp. If ingest and live ever "
                "sit on different blueprints they can land in different processes, "
                "and the map will read a _snapshot nobody writes.",
            )


class TestFeederTargetsTheMainApp(unittest.TestCase):
    """The self-feeder must post to the process that serves the map."""

    def test_default_ingest_url_points_at_the_main_app(self):
        import adsb_selfeed

        self.assertEqual(
            adsb_selfeed.ingest_url(),
            f"http://127.0.0.1:{MAIN_APP_PORT}/api/adsb/ingest",
            "the feeder must default to the main app's ingest endpoint",
        )

    def test_ingest_url_reads_the_environment_at_call_time(self):
        """load_env() runs inside main(), so an import-time constant ignores .env."""
        import adsb_selfeed

        self.assertIn("BB_ADSB_INGEST_URL", (_ROOT / "adsb_selfeed.py").read_text())
        with mock.patch.dict(os.environ, {"BB_ADSB_INGEST_URL": "http://elsewhere/ingest"}):
            self.assertEqual(adsb_selfeed.ingest_url(), "http://elsewhere/ingest")


class TestNothingPointsAtTheRetiredProcess(unittest.TestCase):
    """The :5000 process is gone; a reference to it is a latent outage."""

    def test_no_source_references_the_retired_port(self):
        # Require a digit before the colon so `[:5000]` (a slice bound) does not
        # read as a port. In 127.0.0.1:5000 the preceding character is a digit.
        pattern = re.compile(rf"(?<=\d):{RETIRED_PORT}\b")
        this_file = pathlib.Path(__file__).resolve()
        offenders = []
        for path in _SOURCES:
            if path.resolve() == this_file:
                continue  # this file names the port on purpose, to document it
            text = path.read_text(errors="replace")
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(_ROOT)}:{line}")
        self.assertEqual(
            offenders, [],
            "still references the retired :5000 process: " + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()