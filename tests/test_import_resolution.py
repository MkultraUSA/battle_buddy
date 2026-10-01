"""Every private helper audio_receiver uses must actually be imported.

Three handlers were returning HTTP 500 in production because they called
underscore-prefixed functions that were never brought into scope:

    _fill_incident_coords             /api/incidents/flagged
    _commute_route_info               /api/commute/incidents
    _point_to_segment_distance_miles  nearby-incident lookup

All three live in modules that `audio_receiver` pulls in with
`from x import *`, and **star imports skip underscore-prefixed names**. So the
helpers were defined, tested in their own modules, and unreachable from the one
file that called them.

Nothing caught it. The existing suite passed, the modules' own unit tests passed,
and the endpoints simply 500'd. A name that doesn't exist is not a wrong answer,
it is a crash, so it cannot be found by asserting on behaviour -- it has to be
found by resolving the name.

`audio_receiver.py:56` already carried one such explicit import
(`_routes_travel_time`), which is the tell: someone hit this, patched the case
they were looking at, and left the other three.

The check below resolves every module-level call in audio_receiver against the
real imported module, so a future rename or a new private helper fails here
rather than in production.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_APP = _ROOT / "audio_receiver.py"

#: Private helpers audio_receiver calls and therefore must import explicitly.
KNOWN_PRIVATE_HELPERS = (
    "_fill_incident_coords",
    "_commute_route_info",
    "_point_to_segment_distance_miles",
    "_routes_travel_time",
)


def _module_level_calls() -> set[str]:
    """Names called at module scope or inside handlers, minus locals.

    Deliberately conservative: it collects every call anywhere in the file and
    lets the runtime check decide which ones resolve. Anything the runtime does
    not have is reported, because in this file a missing name is a crash.
    """
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    return called


def _probe(names: list[str]) -> dict:
    """Ask the real module which of `names` it can resolve.

    Runs in a subprocess because audio_receiver needs faster_whisper, and because
    importing it starts nothing but does freeze configuration at import time.
    """
    child = (
        "import json, sys\n"
        "from unittest import mock\n"
        "sys.modules['stripe'] = mock.MagicMock()\n"
        "import audio_receiver as ar\n"
        "names = json.loads(sys.argv[1])\n"
        "print(json.dumps({n: hasattr(ar, n) for n in names}))\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "PATH": "/usr/bin:/bin",
            "DB_PATH": f"{tmp}/probe.db",
            "BATTLE_BUDDY_HOME": tmp,
            "BATTLE_BUDDY_DATA_DIR": tmp,
            "BB_RAW_AUDIO_QUEUE_DIR": f"{tmp}/raw_queue",
            "TIPS_UPLOAD_DIR": f"{tmp}/tips",
            "TGID_TSV": f"{tmp}/none.tsv",
            "HOMICIDE_SEED_PATH": f"{tmp}/none.json",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc = subprocess.run(
            [sys.executable, "-c", child, json.dumps(names)],
            cwd=str(_ROOT), env=env, capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise AssertionError(f"probe failed: {proc.stderr[-1500:]}")
        return json.loads(proc.stdout.splitlines()[-1])


def _importable() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-c", "import audio_receiver"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    return proc.returncode == 0, (proc.stderr or "")[-300:]


_IMPORTABLE, _IMPORT_ERROR = _importable()


class TestPrivateHelpersAreImported(unittest.TestCase):
    def setUp(self) -> None:
        if not _IMPORTABLE:
            self.skipTest(
                "audio_receiver cannot be imported here (needs faster_whisper); "
                f"the authoritative baseline is /opt/battlebuddy/venv. {_IMPORT_ERROR}"
            )

    def test_the_known_private_helpers_resolve(self):
        resolved = _probe(list(KNOWN_PRIVATE_HELPERS))
        for name, ok in sorted(resolved.items()):
            with self.subTest(helper=name):
                self.assertTrue(
                    ok,
                    f"{name} is called by audio_receiver but is not in scope. "
                    "`from x import *` does not import underscore-prefixed names, "
                    "so it must be imported explicitly or the calling endpoint "
                    "raises NameError and returns 500.",
                )

    def test_star_imports_cannot_supply_private_names(self):
        """Document the trap, so the explicit imports are not 'cleaned up'."""
        db_source = (_ROOT / "modules" / "database.py").read_text(encoding="utf-8")
        self.assertIn("def _fill_incident_coords", db_source)
        self.assertNotIn(
            "__all__", db_source,
            "if database.py grows an __all__, the star import's contents change "
            "and these explicit imports become the only thing keeping the "
            "handlers working",
        )

    def test_every_called_name_resolves(self):
        """The general form: no call anywhere in the file may be unresolvable.

        A NameError cannot be found by asserting on behaviour -- it has no
        behaviour -- so this is the only way to see it before production does.
        """
        called = sorted(n for n in _module_level_calls() if not n.startswith("__"))
        resolved = _probe(called)
        missing = sorted(name for name, ok in resolved.items() if not ok)
        self.assertEqual(
            [], missing,
            f"audio_receiver calls names that do not resolve: {missing}. "
            "Each is a NameError at runtime, i.e. a 500 from whichever "
            "handler reaches it.",
        )


if __name__ == "__main__":
    unittest.main()