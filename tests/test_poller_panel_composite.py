"""The composite health expression must never filter, and this test is why.

`build_poller_panels.py` builds a "Poller Health" stat whose whole purpose is to
render FAILING when a poller is unhealthy. Its expression originally read:

    active == bool 1
      * (last_success_age >= 0 AND last_success_age < 18h)
      * (consecutive_failures == bool 0)

In PromQL a comparison **without** `bool` is a filter: it drops every sample that
does not match and keeps the ones that do. So the `AND` clause yielded a sample
only while the age was inside the window; the moment a poller went stale its sample
was dropped, the product became an empty vector, and the panel rendered "No data"
instead of FAILING.

"No data" is also what a poller that does not exist at all renders. So the panel
could not distinguish a stopped poller from an absent one -- which is the one
distinction it exists to make.

This asserts the property on the expression that actually ships, taken from the
generated panel rather than from the file's text. Asserting on file text would pin
the shape of what we wrote instead of the property, and could be satisfied by a
comment that mentions the right answer (#16).

No Prometheus is available locally, so this is structural on the built artifact
rather than an evaluation. The witness in
`~/.local/share/paperclip-diagnostics/witness_poller_fixes.py` confirms this test
fails when the `bool` qualifiers are removed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PANELS = ROOT / "scripts" / "build_poller_panels.py"

#: A comparison operator NOT followed by `bool` is a filter, not a value.
#:
#: The `(?!=)` matters: without it the regex backtracks from `>=` to `>` on the very
#: text it should accept, and every correct expression fails. That was the first
#: version, and it reported `['>']` against an expression that was entirely `bool`.
_FILTERING = re.compile(r"(?<![=!<>])(>=|<=|==|!=|>|<)(?!=)(?!\s*bool\b)")


def _health_expressions() -> list[str]:
    """The composite expression the generator actually ships.

    Read from the built panel rather than the source text, so what is checked is
    what reaches Grafana. Calling `_poller_row` is the whole builder: it returns
    the row dict containing the stat targets.
    """
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("build_poller_panels_probe", PANELS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_poller_panels_probe"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("build_poller_panels_probe", None)

    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            targets = node.get("targets")
            if isinstance(targets, list):
                for target in targets:
                    expr = target.get("expr") if isinstance(target, dict) else None
                    if isinstance(expr, str) and "poller_active" in expr and "*" in expr:
                        found.append(expr)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(module._poller_row("afd", 0))  # noqa: SLF001 - testing the builder
    return found


def test_at_least_one_composite_health_expression_is_built() -> None:
    """Guards against the test quietly testing nothing."""
    assert _health_expressions(), (
        "no composite health expression could be built -- the generator's shape "
        "changed and this test would otherwise pass vacuously"
    )


@pytest.mark.parametrize("expr", _health_expressions())
def test_no_comparison_in_the_composite_filters(expr: str) -> None:
    offenders = [m.group(0) for m in _FILTERING.finditer(expr)]
    assert not offenders, (
        f"comparison(s) {offenders} without `bool` will FILTER samples out of the "
        f"product. A stale poller then renders 'No data' instead of FAILING, which "
        f"is indistinguishable from a poller that does not exist.\n"
        f"expression was: {expr}"
    )


@pytest.mark.parametrize("expr", _health_expressions())
def test_health_expression_covers_all_three_conditions(expr: str) -> None:
    for metric in (
        "battlebuddy_poller_active",
        "battlebuddy_poller_last_success_age_seconds",
        "battlebuddy_poller_consecutive_failures",
    ):
        assert metric in expr, f"composite does not consider {metric}"
    assert "*" in expr, "conditions must be multiplied, so each contributes 0 or 1"


def test_panel_generator_declares_no_filtering_idiom() -> None:
    """Guard the specific shape that caused it, on the source as a backstop.

    The expression assertions above are the real check. This is a belt-and-braces
    guard on the literal idiom, because it reads better in a failure message than a
    regex over generated strings does.
    """
    text = PANELS.read_text(encoding="utf-8")
    idiom = re.compile(r"last_success_age_seconds\}[^\n]*?\}\s*>=\s*0(?!_)")
    assert not idiom.search(text), (
        "the filtering idiom is back: a bare `>= 0` on last_success_age_seconds "
        "drops samples instead of yielding a value"
    )