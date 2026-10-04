"""A log line something else depends on must not be silenced behind a default-off flag.

This exists because of MRR-13. One branch put the per-call transcript prints
behind `DEBUG_TRANSCRIPTS` (default false), which would have silently broken two
live consumers:

  * `scripts/build_error_panels.py` lists `"[recv] DROP"` in
    `EXPECTED_LOG_PHRASES` -- the non-speech panels are built by *excluding* that
    phrase from an error match. Silence it and the exclusion quietly matches
    nothing, so the panel looks healthy and says nothing.
  * `tests/_backlog_authz_child.py` captures stdout precisely because the app
    writes `[backlog] ...` lines there.

That is the same failure as the `psutil` metrics that emptied three Grafana
panels after a rebuild, and the same as the Telegram watcher querying three
metric names the app never emitted: **a consumer depends on a string, nobody
connects the two, and the result looks fine.**

WHY AN AST AND NOT `assertIn`
This project's own rule is that asserting on the text of a source file pins the
shape of what was shipped rather than the property we want (#16): rewriting the
guard into a comment passes a substring check while the behaviour is gone. So
this walks the AST, finds the `print(...)` that emits each phrase, and inspects
the guard it sits inside. A substring test cannot tell an unconditional print
from a `print` that only fires when a flag is true.

WHAT IT ENFORCES
For each phrase in EXPECTED_LOG_PHRASES, the emitting print must be
unconditional, or guarded by a flag that **defaults to true**. Guarding a
consumer-visible line behind a flag that defaults to false is the defect.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ERROR_PANELS = ROOT / "scripts" / "build_error_panels.py"
CONFIG = ROOT / "modules" / "config.py"

SOURCES = [ROOT / "audio_receiver.py", *sorted((ROOT / "modules").glob("*.py"))]


def _load_expected_phrases() -> tuple[str, ...]:
    """Read the consumer's list by importing it, not by regexing the file."""
    spec = importlib.util.spec_from_file_location("build_error_panels_probe", ERROR_PANELS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_error_panels_probe"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("build_error_panels_probe", None)
    return tuple(module.EXPECTED_LOG_PHRASES)


def _config_defaults() -> dict[str, object]:
    source = CONFIG.read_text(encoding="utf-8")
    tree = ast.parse(source)
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                try:
                    defaults[target.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    continue
    return defaults


def _emitting_prints(phrase: str):
    """Yield (source_path, lineno, guard_names) for prints that can emit `phrase`."""
    for path in SOURCES:
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "print"):
                continue
            rendered = ast.dump(node)
            # the phrase may be a literal or part of an f-string; compare on the
            # literal fragments the call actually carries
            fragments: list[str] = []
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    fragments.append(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    fragments.extend(
                        part.value for part in arg.values
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    )
            if not any(phrase in fragment for fragment in fragments):
                continue
            guards: list[str] = []
            # NOTE: walk enclosing `if` blocks by testing membership in `.body`,
            # not by comparing an iter_fields child to the node. `iter_fields`
            # yields the body as a LIST, so `child is node` never matches and the
            # guard check silently becomes dead code -- which is exactly what
            # happened the first time this was written. See the witness note below.
            for parent in ast.walk(tree):
                if not isinstance(parent, ast.If):
                    continue
                for block in (parent.body, parent.orelse):
                    if any(stmt is node for stmt in block):
                        test = parent.test
                        if isinstance(test, ast.Name):
                            guards.append(test.id)
                        elif isinstance(test, ast.Attribute):
                            guards.append(test.attr)
                        else:
                            guards.append(ast.dump(test)[:60])
            yield path, node.lineno, guards, rendered


@pytest.mark.parametrize("phrase", _load_expected_phrases())
def test_expected_log_phrase_is_still_emitted(phrase: str) -> None:
    """At least one live print still carries each phrase the panels depend on."""
    matches = list(_emitting_prints(phrase))
    assert matches, (
        f"no print in the tree emits {phrase!r}, but build_error_panels lists it in "
        f"EXPECTED_LOG_PHRASES. A panel that excludes a phrase nothing emits "
        f"matches everything and reports nothing."
    )


@pytest.mark.parametrize("phrase", _load_expected_phrases())
def test_expected_log_phrase_is_not_behind_a_default_off_flag(phrase: str) -> None:
    """The emitting print must not sit behind a flag that defaults to false.

    This is the MRR-13 defect, asserted as a property rather than as a patch.
    """
    defaults = _config_defaults()
    for path, lineno, guards, _rendered in _emitting_prints(phrase):
        for guard in guards:
            assert guard in defaults, (
                f"{path.name}:{lineno} guards {phrase!r} behind {guard!r}, which is "
                f"not a config constant -- if that is a runtime value, a consumer "
                f"depends on a line nobody can account for."
            )
            assert defaults[guard] is not False, (
                f"{path.name}:{lineno} silences {phrase!r} behind {guard}=False. "
                f"build_error_panels depends on this phrase, so the non-speech "
                f"panels would quietly stop excluding anything. Gate the "
                f"human-readable text, never the machine-readable line."
            )


def test_expectation_list_has_not_grown() -> None:
    """Ratchet: every new entry is a new obligation, so adding one must be deliberate.

    Fixing this class means removing an entry (by making the phrase unconditional
    again) or accepting the dependency on purpose -- not growing the list quietly.
    """
    assert len(_load_expected_phrases()) <= 2, (
        "EXPECTED_LOG_PHRASES grew. Each entry is a log line something depends on; "
        "adding one is fine, but do it deliberately and add the matching assertion "
        "test here rather than relying on the wildcard."
    )