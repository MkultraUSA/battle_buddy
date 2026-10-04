"""ops_verify and the panel generator must agree on which pollers exist.

Two lists, one truth. `ops_verify` derives its poller set from the live scrape;
`build_poller_panels.py` needs a list because it installs panels from a laptop with
no access to `/metrics`. The original defect lived exactly here: both files carried
a hardcoded list, they drifted from reality, and the comment claiming they matched
was the only thing keeping them honest.

This asserts they agree against a realistic scrape. It is deliberately a *drift*
test rather than a duplicate of either list, so it fails when either side changes
without the other -- which is the failure that mattered.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANELS = ROOT / "scripts" / "build_poller_panels.py"
OPS = ROOT / "scripts" / "ops_verify.py"

#: The seven pollers whose `.start()` is not commented out in audio_receiver.py,
#: verified against the live scrape on 2026-10-04 (MRR-15). `reddit-intel` is
#: absent because its `.start()` is commented out.
EXPECTED_RUNNING = {
    "adsb-air-asset",
    "afd",
    "apd-cad",
    "apd_news",
    "atxfloods",
    "austin-events",
    "traffic-open-data",
}


def _panel_poller_list() -> list[str]:
    """Read the list the panel script returns, by AST rather than by regex.

    A regex would keep passing if the literal moved into a variable or a
    comprehension -- which is how a source-text assertion ends up pinning the shape
    of what we shipped instead of the property.
    """
    tree = ast.parse(PANELS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "app_poller_names":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.List):
                    return [elt.value for elt in sub.value.elts if isinstance(elt, ast.Constant)]
    raise AssertionError("app_poller_names() has no list literal return; update this test")


def _load_ops():
    """Import ops_verify the way the rest of the suite does, so __file__ is real.

    The first version of this test `exec`'d the module and every test failed on
    `NameError: __file__` -- an import harness bug, reported as three unrelated
    test failures.
    """
    if "ops" in _cache:
        return _cache["ops"]
    spec = importlib.util.spec_from_file_location("ops_verify_drift_probe", OPS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ops_verify_drift_probe"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("ops_verify_drift_probe", None)
    _cache["ops"] = module
    return module


_cache: dict = {}


def _ops_derives(names: set[str]) -> set[str]:
    """What ops_verify derives from a scrape containing exactly `names`."""
    keys = [f'battlebuddy_poller_active{{poller="{name}"}}' for name in names]
    return set(_load_ops().poller_names(keys))


def test_panel_list_matches_live_scrape() -> None:
    assert set(_panel_poller_list()) == EXPECTED_RUNNING, (
        "build_poller_panels.py poller list disagrees with the live scrape. "
        "A row for a poller that does not exist reads as \"no data\" forever, which "
        "is indistinguishable from a stopped poller."
    )


def test_ops_verify_derives_the_same_set() -> None:
    assert _ops_derives(EXPECTED_RUNNING) == EXPECTED_RUNNING


def test_a_disabled_poller_does_not_leave_a_gate() -> None:
    """The behaviour the hardcoded list got wrong, asserted directly."""
    with_disabled = EXPECTED_RUNNING | {"reddit-intel"}
    derived = _ops_derives(with_disabled - {"reddit-intel"})
    assert "reddit-intel" not in derived, (
        "a poller with no metrics must not appear in the gate set, or three gates "
        "for it fail forever"
    )


def test_empty_scrape_derives_nothing_rather_than_everything() -> None:
    assert _ops_derives(set()) == set()


def test_reddit_intel_is_not_faked_healthy_anywhere() -> None:
    """Regression guard on the exact fabrication that hid the defect."""
    assert "reddit-intel" not in set(_panel_poller_list())
    fixture = (ROOT / "tests" / "test_poller_health_ops.py").read_text(encoding="utf-8")
    fabricating = [
        line.strip() for line in fixture.splitlines()
        if "reddit-intel" in line and "active" in line
    ]
    assert not fabricating, (
        f"the fixture fabricates reddit-intel as active again: {fabricating}"
    )
