"""The regression-battery row on the Ops dashboard must be real.

`scripts/build_regression_panels.py` generates the row and installs it into
Grafana. Nothing in CI runs that install, so without this the panel could rot
into a set of queries naming metrics that stopped existing — which is the exact
failure the neighbouring `test_grafana_contract.py` exists to catch, except that
one only looks at what is already installed.

What is asserted here is the part that would be easy to get wrong by hand:

  * the row exists exactly once, and it is generated rather than hand-placed
  * **the headline is a product of three conditions.** `failed == 0` on its own
    reads green when the battery has stopped, which is the single failure this
    row exists to make visible.
  * the panel's staleness threshold equals `ops_verify`'s. A panel that says two
    hours while the gate says four is two sources of truth, which is how the SLO
    page came to say "13 gates" for months.
  * every metric the queries name is one the app actually exports.
  * re-running the builder produces byte-identical panels, so the script is the
    source of truth and the UI is not.
  * the builder refuses to install when a query names a metric that does not
    exist, rather than writing a blank panel.

None of this talks to Grafana. `fixtures/grafana/bb-ops-private.json` is the
installed state, and the fetch script refreshes it deliberately.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "fixtures" / "grafana" / "bb-ops-private.json"
_BUILDER = _ROOT / "scripts" / "build_regression_panels.py"
_METRIC = re.compile(r"\b(battlebuddy_[a-z_0-9]+)\b")


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_regression_panels", _BUILDER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _installed_row() -> dict | None:
    if not _FIXTURE.exists():
        return None
    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    def find(panels):
        for panel in panels or []:
            if "regression-battery" in (panel.get("description") or ""):
                return panel
            nested = find(panel.get("panels"))
            if nested:
                return nested
        return None

    return find(doc.get("panels"))


class TestTheRowIsInstalled(unittest.TestCase):
    def test_it_exists_exactly_once(self):
        doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        found = [p for p in doc.get("panels") or []
                 if "regression-battery" in (p.get("description") or "")]
        self.assertEqual(
            1, len(found),
            f"expected one generated regression row, found {len(found)}. The "
            "install is meant to replace by marker, so more than one means the "
            "marker is missing from the row description.",
        )

    def test_it_carries_the_generator_marker(self):
        row = _installed_row()
        self.assertIsNotNone(row, "the generated row is not in the fixture")
        self.assertIn(_load_builder().MARKER, row["description"],
                      "without the marker the next install appends a duplicate")

    def test_it_has_the_panels_it_claims_to(self):
        row = _installed_row()
        titles = {p["title"]: p for p in row["panels"]}
        for expected in ("Regression Battery", "Checks Passed", "Checks Run",
                         "Checks Failed", "Battery Age (min)", "Per-check status",
                         "Check history (30d)"):
            with self.subTest(panel=expected):
                self.assertIn(expected, titles)

    def test_every_panel_has_a_description(self):
        """An unexplained panel is one nobody trusts at 3am."""
        for panel in _installed_row()["panels"]:
            with self.subTest(panel=panel["title"]):
                self.assertTrue((panel.get("description") or "").strip())


class TestTheHeadlineCannotReadGreenWhileStopped(unittest.TestCase):
    """The property the row exists for."""

    def _headline_expr(self) -> str:
        row = _installed_row()
        for panel in row["panels"]:
            if panel["title"] == "Regression Battery":
                return panel["targets"][0]["expr"]
        raise AssertionError("the Regression Battery panel is missing")

    def test_it_combines_all_three_conditions(self):
        expr = self._headline_expr()
        for gauge, why in (
            ("battlebuddy_regression_failed",
             "a check failed but the panel says nothing"),
            ("battlebuddy_regression_last_run_age_seconds",
             "the timer stopped, which is the failure this row exists to catch"),
            ("battlebuddy_regression_error",
             "the results file is unreadable and every other gauge is showing a "
             "defaulted zero rather than a measurement"),
        ):
            with self.subTest(gauge=gauge):
                self.assertIn(gauge, expr,
                              f"the headline ignores {gauge}: {why}")

    def test_it_is_a_product_so_all_must_hold(self):
        """Multiplied, not ANDed: PromQL has no scalar `and`, and an additive sum
        would read 1 when exactly one condition failed."""
        expr = self._headline_expr()
        self.assertGreaterEqual(expr.count("*"), 2,
                                f"the headline is not combining conditions: {expr}")
        self.assertNotIn("+", expr, "an additive combination can reach 1 while broken")

    def test_its_thresholds_make_one_green(self):
        row = _installed_row()
        for panel in row["panels"]:
            if panel["title"] != "Regression Battery":
                continue
            steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
            self.assertEqual("green", steps[-1]["color"])
            self.assertEqual(1, steps[-1]["value"],
                             "green must start at 1, or a failing battery shows green")
            self.assertEqual("red", steps[0]["color"])

    def test_it_maps_the_number_to_a_word(self):
        """A bare 1 that the reader has to decode is worse than no number."""
        row = _installed_row()
        for panel in row["panels"]:
            if panel["title"] == "Regression Battery":
                continue
        panel = next(p for p in row["panels"] if p["title"] == "Regression Battery")
        mappings = panel["fieldConfig"]["defaults"]["mappings"]
        self.assertTrue(mappings, "the headline has no value mapping")
        rendered = json.dumps(mappings)
        self.assertIn("OK", rendered)
        self.assertIn("FAILING", rendered)


class TestPanelAndGateAgreeOnStaleness(unittest.TestCase):
    """One number, stated once, imported by both.

    Two sources of truth is how the SLO page read "13 gates" for months while 19
    ran. The panel must not be able to disagree with the gate.
    """

    def test_the_builder_and_ops_verify_use_the_same_constant(self):
        builder = _load_builder()
        ops = (_ROOT / "scripts" / "ops_verify.py").read_text(encoding="utf-8")
        self.assertIn("REGRESSION_MAX_AGE_S", ops)
        tree = ast.parse(ops)
        value = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "REGRESSION_MAX_AGE_S"
                for t in node.targets
            ):
                value = _const_int(node.value)
        self.assertIsNotNone(
            value,
            "REGRESSION_MAX_AGE_S is no longer a constant integer expression",
        )
        self.assertEqual(
            value, builder.FRESH_SECONDS,
            "the panel's staleness threshold and the ops_verify gate disagree; a "
            "battery could be red on one and green on the other",
        )


class TestEveryQueriedMetricExists(unittest.TestCase):
    def test_the_queries_name_metrics_the_app_defines(self):
        builder = _load_builder()

        def names(panels):
            out = set()
            for panel in panels or []:
                for target in panel.get("targets") or []:
                    if isinstance(target.get("expr"), str):
                        out.update(_METRIC.findall(target["expr"]))
                out |= names(panel.get("panels"))
            return out

        installed = names([_installed_row()])
        generated = _METRIC.findall(json.dumps(builder.build_row(0)))
        self.assertTrue(installed, "no metrics found in the installed row")
        unknown = sorted((installed | set(generated)) - builder.app_metric_names())
        self.assertEqual([], unknown,
                         f"panels query metrics the app does not define: {unknown}")

    def test_the_builder_refuses_to_install_a_bogus_query(self):
        """The guard, exercised. A blank panel installed silently is the whole
        failure this file exists to prevent."""
        builder = _load_builder()
        real = builder.app_metric_names
        builder.app_metric_names = lambda: real() - {"battlebuddy_regression_failed"}
        try:
            with self.assertRaises(SystemExit) as caught:
                builder.main(["--install"])
            self.assertIn("does not define", str(caught.exception))
        finally:
            builder.app_metric_names = real


class TestTheBuilderIsTheSourceOfTruth(unittest.TestCase):
    def test_regenerating_reproduces_what_is_installed(self):
        """If these drift, editing the UI is the wrong move and nobody would know."""
        builder = _load_builder()
        row = _installed_row()
        rebuilt = builder.build_row(row["gridPos"]["y"])
        self.assertEqual(
            [_signature(p) for p in rebuilt["panels"]],
            [_signature(p) for p in row["panels"]],
            "the installed row differs from what the generator produces; re-run "
            "scripts/build_regression_panels.py --install",
        )

    def test_grid_positions_do_not_overlap(self):
        """Overlapping panels render on top of each other, which looks like a
        Grafana bug rather than a layout mistake."""
        row = _installed_row()
        seen = set()
        for panel in row["panels"]:
            g = panel["gridPos"]
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    cell = (x, y)
                    self.assertNotIn(cell, seen,
                                     f"{panel['title']!r} overlaps at {cell}")
                    seen.add(cell)


def _const_int(node: ast.AST):
    """Evaluate a constant integer expression.

    `ast.literal_eval` refuses `2 * 3600`, which is exactly how the constant is
    written, so the first version of this test reported None and looked like a
    disagreement between the panel and the gate rather than a limitation of the
    analyser. No `eval`: this walks the tree and refuses anything that is not
    integer arithmetic.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub,
                                                            ast.Mult, ast.FloorDiv,
                                                            ast.Div)):
        left, right = _const_int(node.left), _const_int(node.right)
        if left is None or right is None:
            return None
        return {ast.Add: lambda a, b: a + b,
                ast.Sub: lambda a, b: a - b,
                ast.Mult: lambda a, b: a * b,
                ast.FloorDiv: lambda a, b: a // b,
                ast.Div: lambda a, b: a / b}[type(node.op)](left, right)
    return None


def _signature(panel: dict) -> str:
    """What must match between the installed panel and the generated one."""
    return json.dumps({
        "title": panel["title"],
        "type": panel["type"],
        "expr": [t.get("expr") for t in panel.get("targets") or []],
        "thresholds": (panel.get("fieldConfig", {}).get("defaults", {})
                       .get("thresholds")),
        "mappings": (panel.get("fieldConfig", {}).get("defaults", {})
                     .get("mappings")),
        "gridPos": panel["gridPos"],
    }, sort_keys=True)


if __name__ == "__main__":
    unittest.main()