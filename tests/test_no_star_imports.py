"""`audio_receiver.py` must not use `from x import *`.

Nine modules were opened with a wildcard. It cost three separate ways, and all
three were realised in this one file rather than theorised:

  * **A private helper was unreachable.** Star imports skip underscore-prefixed
    names, so `_fill_incident_coords`, `_commute_route_info` and
    `_point_to_segment_distance_miles` were defined, unit-tested in their own
    modules, and not in scope here. Three handlers returned HTTP 500 (#175).
  * **A crash became a wrong answer.** The `!status` Talk command read
    `globals().get("_current_hold_tgid")`. That name lives in
    `modules/incident_engine.py` and no wildcard could ever have brought it here,
    so the `.get()` returned its default every time and the command reported
    "No hold active" no matter what the Pi was doing. A missing name is a crash;
    `.get()` with a default is how a crash gets converted into a quiet lie.
  * **The file's real dependency surface was unreadable.** A wildcard delivers
    the module's imports as well as its definitions. `modules.llm` imports json,
    os, re, sqlite3, threading and time for itself, and this file was reading all
    of them through that stranger's namespace. The genuine API was 45 names; the
    rest was somebody else's stdlib, so a reader could not tell which was which.

`tests/test_import_resolution.py` already resolves every call site, which is what
made the conversion safe to attempt — it reports a name that has gone missing
rather than letting a 500 find it. This file stops them coming back, and checks
the second point above directly, because that one is a *value* read rather than a
call and so sits outside what a call-graph resolver can see.
"""

from __future__ import annotations

import ast
import builtins
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "audio_receiver.py"
_SRC = _APP.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _module_source(dotted: str) -> str:
    as_file = _ROOT / (dotted.replace(".", "/") + ".py")
    path = as_file if as_file.exists() else _ROOT / dotted.replace(".", "/") / "__init__.py"
    return path.read_text(encoding="utf-8")


class TestNoWildcardImports(unittest.TestCase):
    def test_audio_receiver_opens_no_module_with_a_star(self):
        offenders = []
        for node in ast.walk(_TREE):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        offenders.append(f"line {node.lineno}: from {node.module} import *")
        self.assertEqual([], offenders, "\n".join(offenders))

    def test_nothing_else_in_the_repo_imports_audio_receiver_by_star(self):
        """A wildcard on *this* file would re-expose every name it binds."""
        for path in sorted((_ROOT / "modules").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "") in (
                    "audio_receiver", "modules.audio_receiver",
                ):
                    self.assertNotIn(
                        "*", [a.name for a in node.names],
                        f"{path.relative_to(_ROOT)} star-imports audio_receiver",
                    )


class TestEveryNameIsAccountedFor(unittest.TestCase):
    """Not just calls — every name the file reads.

    `test_import_resolution.py` resolves calls, which is what a NameError breaks.
    A name read as a *value* breaks just as silently: the `!status` hold read was
    a subscript-shaped lookup that returned None instead of raising. So this walks
    every `Name` load and every attribute base and requires each one to be a
    builtin, a parameter, a local, a module-level binding, or an explicit import.
    """

    def test_no_name_is_read_that_nothing_provides(self):
        provided: set[str] = set(dir(builtins))

        def imports_in(node) -> None:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        provided.add((a.asname or a.name).split(".")[0])
                elif isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        if a.name != "*":
                            provided.add(a.asname or a.name)

        imports_in(_TREE)

        bound: set[str] = set()
        for node in ast.walk(_TREE):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.Global):
                bound.update(node.names)

        read: set[str] = set()
        for node in ast.walk(_TREE):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                read.add(node.id)
            elif isinstance(node, ast.Attribute):
                base = node
                while isinstance(base, ast.Attribute):
                    base = base.value
                if isinstance(base, ast.Name):
                    read.add(base.id)

        missing = sorted(read - provided - bound)
        self.assertEqual(
            [], missing,
            f"audio_receiver reads names nothing provides: {missing}. Each would be "
            "a NameError, or — if reached through globals().get() or getattr — a "
            "silent wrong answer instead.",
        )


class TestTheHoldStateIsReadFromItsOwner(unittest.TestCase):
    """The bug this change was found while making.

    `_current_hold_tgid` is a module-level global in `modules/incident_engine.py`
    that incident_engine rebinds as holds are taken and released. Two wrong ways
    to read it from here, both of which look right:

      * `globals().get("_current_hold_tgid")` — reads *this* file's globals,
        where the name has never existed, and returns the default. This is what
        shipped; the command always said "No hold active".
      * `from modules.incident_engine import _current_hold_tgid` — binds the value
        at import time and never sees a later rebind, so it reads stale forever.

    Only reading the attribute off the module is correct, and it is also the only
    form that fails loudly if the global is ever renamed.
    """

    def test_it_is_not_read_from_this_files_own_globals(self):
        """Checked over the AST, not the text.

        The string `globals().get("_current_hold_tgid")` now appears in a comment
        in audio_receiver.py explaining why it was wrong, so a substring assertion
        would fail on the explanation. That is the same mistake as asserting on
        the shape of code: what matters is whether the call is *made*.
        """
        offenders = []
        for node in ast.walk(_TREE):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "globals"):
                offenders.append(node.lineno)
        self.assertEqual(
            [], offenders,
            f"audio_receiver calls globals() at line(s) {offenders}. Reading a "
            "module-level name out of this file's own globals is how the !status "
            "hold read silently returned None forever; a name that is not there "
            "should raise, not default.",
        )

    def test_it_is_not_imported_by_name(self):
        """A from-import binds the value once, at import time."""
        for node in ast.walk(_TREE):
            if isinstance(node, ast.ImportFrom) and node.module == "modules.incident_engine":
                self.assertNotIn(
                    "_current_hold_tgid", [a.name for a in node.names],
                    "importing the hold global by name captures it at import time "
                    "and it will read stale after the first hold change",
                )

    def test_it_is_read_off_the_module(self):
        self.assertIn(
            "incident_engine_mod._current_hold_tgid", _SRC,
            "the hold state must be read from the module that owns and rebinds it",
        )

    def test_the_module_alias_is_imported(self):
        self.assertRegex(
            _SRC, r"(?m)^import modules\.incident_engine as incident_engine_mod\b")

    def test_the_name_still_exists_where_it_is_read_from(self):
        """The cheap half of the AttributeError guard: if the global is renamed,
        this reads the module's real namespace rather than trusting a comment."""
        engine = ast.parse(_module_source("modules.incident_engine"))
        top_level = {
            n.target.id for n in engine.body
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
        } | {
            t.id for n in engine.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)
        }
        self.assertIn("_current_hold_tgid", top_level)


class TestTheHoldReadActuallyTracksTheHold(unittest.TestCase):
    """A mutation was meant to apply — check that it did.

    The static tests above can only confirm the *form* of the read. This confirms
    the behaviour, and pins down why the two tempting alternatives are wrong:

      * `from modules.incident_engine import _current_hold_tgid` binds the value at
        import time. The owning module rebinds it with `global` as holds are taken
        and released, so a from-import reads a snapshot and goes stale on the very
        first hold.
      * `globals().get("_current_hold_tgid")` reads audio_receiver's namespace,
        where the name has never existed, so it returns its default forever. That
        is what shipped.

    Demonstrated on a synthetic module rather than the real
    `modules.incident_engine`, because the test suite permanently replaces that one
    in `sys.modules`: `tests/test_adsb_air_asset_poller.py` calls `_stub_leaf` on
    `modules.config`, `modules.incident_engine`, `modules.pollers` and
    `modules.pollers_legacy` at import time, with no cleanup, so by the time any
    later test runs, `sys.modules["modules.incident_engine"]` is a fake with
    `__file__ = None` and no `_current_hold_tgid` on it. Loading the real module
    here would either get the fake or execute module-level code against a stubbed
    `modules.config`. The language semantics being relied on are the same either
    way, so they are shown directly and the real module's rebinding is asserted
    statically below.
    """

    def _fixture_module(self):
        import types

        mod = types.ModuleType("synthetic_owner")
        exec(
            "_current_hold_tgid = None\n"
            "def take(tgid):\n"
            "    global _current_hold_tgid\n"
            "    _current_hold_tgid = tgid\n"
            "def release():\n"
            "    global _current_hold_tgid\n"
            "    _current_hold_tgid = None\n",
            mod.__dict__,
        )
        return mod

    def test_reading_the_module_sees_a_later_rebind(self):
        owner = self._fixture_module()
        owner.take(4242)
        self.assertEqual(4242, owner._current_hold_tgid)

    def test_the_from_import_form_would_have_been_stale(self):
        """The wrong-but-plausible fix, shown to be wrong."""
        owner = self._fixture_module()
        snapshot = owner._current_hold_tgid          # what `from x import y` binds
        owner.take(4242)
        self.assertIsNone(
            snapshot,
            "if this ever becomes non-None the semantics this test relies on have "
            "changed and the reasoning behind reading the attribute needs revisiting",
        )
        self.assertEqual(4242, owner._current_hold_tgid)

    def test_the_real_global_is_rebound_and_not_merely_declared(self):
        """Without this the two tests above would hold for a module that never
        changes the value, and the distinction between the forms would be untested
        against the thing that actually ships."""
        source = _module_source("modules.incident_engine")
        self.assertIn("global _current_hold_tgid", source)
        self.assertIn("_current_hold_tgid = tgid", source)
        self.assertIn("_current_hold_tgid = None", source)


class TestTheSuiteDoesNotPoisonModulesForEveryoneElse(unittest.TestCase):
    """A test-isolation defect found while doing the import work.

    `tests/test_adsb_air_asset_poller.py` replaces four real modules in
    `sys.modules` at *import* time and never restores them, so for the rest of the
    pytest process `modules.config`, `modules.incident_engine`, `modules.pollers`
    and `modules.pollers_legacy` are fakes — `modules.config.DB_PATH` reads
    `":memory:"` and `__file__` is `None`. Every later test that imports one of
    them tests the fake.

    That is the same shape as everything else in this repo's list of what nothing
    caught: a green test that proved nothing, because the object under test was a
    stand-in nobody declared. It has not yet caused a wrong result — the tests that
    touch those modules today either stub them themselves or do not import them —
    but it is a live trap for the next person, and the four names are load-bearing.

    Fixing it properly means loading the poller module *inside* a fixture with the
    fakes scoped to it, which is a restructure of that file rather than a fix to
    this one. So this asserts the hazard is still visible instead of quietly living
    with it, and it fails the moment somebody adds a fifth poisoned module.
    """

    KNOWN_POISONED = {
        "modules.config",
        "modules.incident_engine",
        "modules.pollers",
        "modules.pollers_legacy",
    }

    def test_no_other_module_has_started_poisoning_sys_modules(self):
        """Run against whatever has been imported so far in this process.

        The stub modules installed by test_adsb_air_asset_poller are expected and
        excluded; anything else with `__file__ = None` is new and unexplained.
        """
        suspicious = set()
        for name, module in list(sys.modules.items()):
            if not name.startswith("modules"):
                continue
            if getattr(module, "__file__", "missing") is None:
                if name not in self.KNOWN_POISONED:
                    suspicious.add(name)
        self.assertEqual(
            set(), suspicious,
            f"these modules are in sys.modules with no __file__, so they are "
            f"stubs, not code: {sorted(suspicious)}. A test module is installing "
            "fakes at import time with no cleanup. See the class docstring.",
        )

    def test_the_known_poisoning_test_is_still_the_only_offender(self):
        """If this ever passes because the offender was fixed, the guard above has
        silently become a check of nothing."""
        source = (_ROOT / "tests" / "test_adsb_air_asset_poller.py").read_text(
            encoding="utf-8")
        self.assertIn("_stub_leaf(", source)
        self.assertIn(
            'sys.modules[name] = mod', source,
            "the stub helper changed shape; this guard needs updating, and so does "
            "the KNOWN_POISONED set above",
        )


#: The API this file actually uses, mapped to the module that defines it. Built
#: from what the wildcards used to supply, cross-checked against the modules.
NEEDED = {
    "modules.config": [
        "ANTHROPIC_API_KEY", "ANTHROPIC_ENABLED", "DB_PATH", "GOOGLE_MAPS_JS_KEY",
        "GOOGLE_ROUTES_KEY", "NC_PASS", "NC_REPORT_DIR", "NC_USER", "NC_WEBDAV",
        "OPENROUTER_API_KEY", "TALK_BOT_SECRET", "TALK_ROOM", "TALK_USER",
        "TGID_TSV",
    ],
    "modules.database": [
        "ACTIVE_INCIDENT_POPULATION_SQL", "active_incidents", "add_subscription",
        "bump_counter", "calls_since", "get_all_incidents", "init_db",
        "insert_call", "public_active_incidents", "read_counters",
        "recent_calls", "remove_subscription",
    ],
    "modules.geocoding": ["extract_location"],
    "modules.incident_engine": [
        "analyze_for_incident", "clear_stale_incidents", "hold_watchdog_thread",
        "incident_cleanup_thread",
    ],
    "modules.llm": ["llm_analyze", "llm_identify_tgid"],
    "modules.talkgroups": [
        "CAT_COLORS", "IGNORE_TGIDS", "TGID_META", "load_talkgroups",
    ],
    "modules.transcription": ["transcribe"],
    # Deliberate exception to "imported from the module that defines it":
    # modules/pollers/__init__.py is a facade that exists so this file can start
    # pollers without knowing which impl module each class lives in. Its
    # docstring says so. The seven classes below are re-exported, not defined,
    # and that is the point of it.
    "modules.pollers": [
        "ADSBAirAssetPoller", "AFDOpenDataPoller", "APDCADPoller",
        "APDNewsPoller", "ATXFloodsPoller", "AustinEventsPoller",
        "TrafficOpenDataPoller",
    ],
}

#: Re-export-only modules this file is allowed to reach through.
FACADES = {"modules.pollers"}


def _explicit_import_sources() -> dict[str, set[str]]:
    """{name: set(modules it is explicitly imported from)}"""
    out: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                out.setdefault(alias.asname or alias.name, set()).add(node.module)
    return out


def _defines(dotted: str) -> set[str]:
    """Top-level names a module defines, as opposed to importing."""
    tree = ast.parse(_module_source(dotted))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    names.update(
                        t.id for t in sub.targets if isinstance(t, ast.Name)
                    )
    return names


class TestEachNameComesFromTheModuleThatDefinesIt(unittest.TestCase):
    """Right name, right home.

    A wildcard made this unaskable: `DB_PATH` could arrive from any of four
    modules that happen to import it, and nothing recorded which. `calls_since`
    and `DB_PATH` were reachable through three and four paths respectively, so a
    reader could not tell where a name lived — and neither could a rename.

    Importing through a module that merely re-exports is the same mistake one
    level down, so this asserts the source, not just the presence. The one
    permitted exception is the pollers facade, which exists for this file.
    """

    def test_every_name_is_imported_from_its_defining_module(self):
        sources = _explicit_import_sources()
        for module, names in sorted(NEEDED.items()):
            defined = _defines(module)
            for name in names:
                with self.subTest(name=name):
                    got = sources.get(name, set())
                    self.assertIn(
                        module, got,
                        f"{name} is not imported from {module} (found: "
                        f"{sorted(got) or 'nowhere'})",
                    )
                    if module not in FACADES:
                        self.assertIn(
                            name, defined,
                            f"{name} is claimed to come from {module}, which does "
                            "not define it — the expectation list has drifted",
                        )

    def test_the_expectation_list_is_not_stale(self):
        """Every name listed must really be used, and every used name listed.

        Without this the list could quietly stop describing the file, which is
        the failure mode of every hand-maintained inventory.
        """
        listed = {n for names in NEEDED.values() for n in names}
        read: set[str] = set()
        for node in ast.walk(_TREE):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                read.add(node.id)
        # A listed name that appears nowhere in the file is the stale case.
        never_used = sorted(n for n in listed if n not in read)
        self.assertEqual(
            [], never_used,
            "these are listed as needed but are not referenced in audio_receiver: "
            f"{never_used}. Either drop them or the file has changed.",
        )


if __name__ == "__main__":
    unittest.main()
