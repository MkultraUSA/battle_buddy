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
import builtins
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


def _star_import_names(module: str) -> set[str]:
    """Public names a `from <module> import *` brings into scope.

    audio_receiver leans on star imports heavily, so ignoring them would report
    every helper it legitimately gets that way -- insert_call, calls_since,
    llm_analyze and friends -- as unresolvable.

    Only public names, because that is exactly the star-import rule: underscore
    names are excluded. That exclusion is the bug this file exists to catch.
    """
    if not module:
        return set()
    path = _ROOT / (module.replace(".", "/") + ".py")
    if not path.exists():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    return {n for n in names if not n.startswith("_")}


def _unresolvable_calls() -> set[str]:
    """Names called but not resolvable in their own scope.

    A NameError has no behaviour to assert on, so this is the only way to see it
    before production does. Scope matters: the file imports helpers *inside*
    functions in several places, and defines nested helpers like `respond` and
    `summarize`, so a flat "is it a module attribute" check produces a wall of
    false positives.

    For each function, collect what is in scope (parameters, assignments, nested
    defs, imports) and treat a call as resolvable if the name is a builtin, a
    module-level global, or local to that function.
    """
    tree = ast.parse(_APP.read_text(encoding="utf-8"))

    module_globals: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_globals.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                module_globals.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    module_globals |= _star_import_names(node.module or "")
                else:
                    module_globals.add(a.asname or a.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            module_globals.add(node.id)
        elif isinstance(node, ast.arg):
            module_globals.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            module_globals.add(node.name)
        elif isinstance(node, ast.Global):
            module_globals.update(node.names)

    def locals_in(node) -> set[str]:
        names: set[str] = set()
        args = getattr(node, "args", None)
        if args is not None:
            for a in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
                names.add(a.arg)
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                names.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.arg):
                names.add(n.arg)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    names.add(a.asname or a.name)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
            elif isinstance(n, ast.Global):
                names.update(n.names)
        return names

    unresolvable: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = module_globals | locals_in(node)
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and not call.func.id.startswith("__")
                and call.func.id not in scope
            ):
                unresolvable.add(call.func.id)
    return unresolvable


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




class TestScopeAnalysisNeedsNoDependencies(unittest.TestCase):
    """Static check, so it runs everywhere rather than only on the prod venv.

    The runtime probe needs faster_whisper to import audio_receiver. The scope
    analysis needs nothing, and it is the check that actually generalises, so it
    must not be skipped in every ordinary development environment.
    """

    def test_scope_analysis_finds_no_unresolvable_calls(self):
        """The general form, statically.

        Catches the bug class without needing a runtime import, so it runs
        everywhere rather than only where faster_whisper is installed.
        """
        missing = sorted(_unresolvable_calls())
        self.assertEqual(
            [], missing,
            f"audio_receiver calls names that are not in scope: {missing}. "
            "Each is a NameError at runtime, i.e. a 500 from whichever handler "
            "reaches it.",
        )


if __name__ == "__main__":
    unittest.main()