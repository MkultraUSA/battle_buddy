"""The commute-alert block must have exactly one definition in the tree.

Five symbols used to exist twice, verbatim, in `modules/alerts.py` and
`modules/commute.py`: `_COMMUTE_ALERT_ITYPES`, `_COMMUTE_CORRIDOR_MILES`,
`_point_to_segment_distance_miles`, `_routes_travel_time` and
`_check_commute_alerts`. They now live in `modules/commute_alerts.py`, which
depends on neither caller, and both callers re-export them.

Duplication of that kind is not caught by any behavioural test. Both copies
answered every question identically, which is exactly why nobody noticed, and the
failure mode is a fix landing in one copy and shipping in the other — the
commute corridor silently becoming 3.0 miles for one code path and something else
for the other, with the suite green either way. So the property has to be
asserted directly: one definition, and both callers get *that* object.

`_point_to_segment_distance_miles` also had no direct test at all before this
move, despite deciding whether a premium user gets told about an incident near
their route. It gets one here, aimed at the part that is easy to get wrong: the
projection is clamped to the segment, not to the infinite line through it.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MODULES = _ROOT / "modules"
_APP = _ROOT / "audio_receiver.py"
_HOME = _ROOT / "modules" / "commute_alerts.py"

#: The block that was duplicated. The first two are constants; the rest are
#: functions. Both kinds are counted the same way, because a constant copied
#: twice drifts exactly as quietly as a function does.
SHARED_SYMBOLS = (
    "_COMMUTE_ALERT_ITYPES",
    "_COMMUTE_CORRIDOR_MILES",
    "_point_to_segment_distance_miles",
    "_routes_travel_time",
    "_check_commute_alerts",
)


def _sources() -> dict[str, str]:
    """Every first-party module as {path: text}.

    `audio_receiver.py` is in the list on purpose. It star-imports half the tree,
    so a definition that drifted back in there would be picked up by every caller
    at runtime and would be very hard to see by reading.
    """
    out = {}
    for path in sorted(_MODULES.rglob("*.py")) + [_APP]:
        if "__pycache__" in path.parts:
            continue
        out[str(path.relative_to(_ROOT))] = path.read_text(encoding="utf-8")
    return out


def _defining_files(name: str, sources: dict[str, str]) -> list[str]:
    """Which of `sources` define `name` at module level.

    Module level only. A nested `def` of the same name is a different thing, and
    a test helper that happens to share a name should not read as a second
    definition of production code.

    Takes a mapping rather than reading the filesystem so the witness below can
    feed it a known-broken arrangement. An earlier witness in this repo wrote its
    misspelt call into the real source file, and when the assertion failed part
    way the restore never ran, leaving a typo in the working tree — the same
    route by which unrelated drift blocks a deploy.
    """
    found = []
    for path, text in sources.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    found.append(path)
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    found.append(path)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    found.append(path)
    return sorted(found)


def _real_module(dotted: str):
    """Import by name and insist it is the file in this tree.

    The `__file__` check is not ceremony. Other tests stub modules into
    sys.modules to keep heavy imports out of the way, and a stub would sail
    through an identity comparison and quietly prove nothing.
    """
    import importlib

    module = importlib.import_module(dotted)
    path = getattr(module, "__file__", None)
    if not path or _ROOT not in Path(path).resolve().parents:
        raise AssertionError(
            f"{dotted} resolved to {module!r} at {path!r}, not the module in this "
            "repo. Something in the suite has stubbed it; this test would "
            "otherwise pass against a mock and prove nothing."
        )
    return module


class TestOneDefinition(unittest.TestCase):
    def test_each_symbol_is_defined_exactly_once_in_the_whole_tree(self):
        for name in SHARED_SYMBOLS:
            with self.subTest(symbol=name):
                where = _defining_files(name, _sources())
                self.assertEqual(
                    [str(_HOME.relative_to(_ROOT))], where,
                    f"{name} is defined in {where or 'nowhere'}. Two copies of "
                    "this block were the reason it drifted: a fix lands in one "
                    "and ships from the other with a green suite.",
                )

    def test_the_counter_notices_a_second_definition(self):
        """Regression witness: prove the guard above can fail.

        A guard never shown failing might have stopped guarding. Two synthetic
        modules, one definition each, and the counter must report both — not
        quietly collapse them, which is what would happen if it compared
        contents instead of counting sites.
        """
        doubled = {
            "modules/alerts.py": "def _point_to_segment_distance_miles(a):\n    return a\n",
            "modules/commute.py": "def _point_to_segment_distance_miles(a):\n    return a\n",
        }
        self.assertEqual(
            ["modules/alerts.py", "modules/commute.py"],
            _defining_files("_point_to_segment_distance_miles", doubled),
            "the duplicate detector no longer detects a duplicate",
        )

    def test_a_nested_definition_is_not_a_second_definition(self):
        nested = {
            "modules/alerts.py": (
                "def outer():\n"
                "    def _point_to_segment_distance_miles(a):\n"
                "        return a\n"
            ),
        }
        self.assertEqual([], _defining_files("_point_to_segment_distance_miles", nested))


class TestBothCallersGetTheSameObject(unittest.TestCase):
    """Re-exported, not reimplemented.

    Identity rather than equality of behaviour: two byte-identical copies would
    pass any behavioural comparison, and two byte-identical copies are the whole
    problem.
    """

    @classmethod
    def setUpClass(cls):
        # importlib rather than `import modules.alerts`, because the plain form
        # leaves the name bound only as an attribute of the package. Some other
        # test in the suite installs modules into sys.modules directly, so
        # `modules.alerts` can be present and still not be reachable that way —
        # which is a confusing AttributeError rather than a real failure.
        cls.home = _real_module("modules.commute_alerts")
        cls.alerts = _real_module("modules.alerts")
        cls.commute = _real_module("modules.commute")

    def test_the_alerts_re_exports_are_the_same_objects(self):
        for name in SHARED_SYMBOLS:
            with self.subTest(symbol=name):
                self.assertIs(
                    getattr(self.alerts, name), getattr(self.home, name),
                    f"modules.alerts.{name} is not the one definition; a caller "
                    "importing from alerts would get a second copy",
                )

    def test_the_commute_re_exports_are_the_same_objects(self):
        for name in SHARED_SYMBOLS:
            with self.subTest(symbol=name):
                self.assertIs(
                    getattr(self.commute, name), getattr(self.home, name),
                    f"modules.commute.{name} is not the one definition",
                )

    def test_the_explicit_imports_audio_receiver_relies_on_still_work(self):
        """The two import lines that exist because star imports skip underscores.

        `from modules.alerts import _point_to_segment_distance_miles` and
        `from modules.commute import _routes_travel_time` are not redundancy —
        they are the only reason two nearby-incident lookups did not 500. If the
        move had deleted them instead of re-exporting, the name would resolve for
        nobody and the endpoints would fail the same way they did in #175.
        """
        source = _APP.read_text(encoding="utf-8")
        self.assertIn(
            "from modules.alerts import _point_to_segment_distance_miles", source)
        self.assertIn("from modules.commute import _routes_travel_time", source)


class TestPointToSegmentDistance(unittest.TestCase):
    """The one function in the block that decides whether a user gets a DM.

    Distances are in degrees internally and converted with a flat 69.0 miles per
    degree, which is rough but deliberate and matches what both copies did. The
    property worth protecting is the clamp: a route is a *segment*, and an
    incident past the end of it must measure from the end.
    """

    @classmethod
    def setUpClass(cls):
        cls.dist = staticmethod(
            _real_module("modules.commute_alerts")._point_to_segment_distance_miles
        )

    def test_a_point_on_the_segment_is_zero(self):
        self.assertAlmostEqual(
            0.0, self.dist(30.00, -97.75, 30.00, -97.80, 30.00, -97.70), places=9)

    def test_a_point_beside_the_middle_measures_the_offset(self):
        # 0.01 degrees of latitude at 69 miles per degree.
        self.assertAlmostEqual(
            0.69, self.dist(30.01, -97.75, 30.00, -97.80, 30.00, -97.70), places=9)

    def test_a_point_past_the_end_measures_from_the_end_not_the_line(self):
        """The clamp. Without it the infinite line through the route runs on
        forever and an incident fifty miles past the destination reads as being
        beside the road."""
        # Segment runs -97.80 -> -97.70 at latitude 30.00. `beside` is a little
        # north of the middle; `near` and `far` are due east of it, so an
        # unclamped projection would call those two the same distance.
        beside = self.dist(30.005, -97.75, 30.00, -97.80, 30.00, -97.70)
        near = self.dist(30.00, -97.60, 30.00, -97.80, 30.00, -97.70)
        far = self.dist(30.00, -97.50, 30.00, -97.80, 30.00, -97.70)
        self.assertAlmostEqual(0.345, beside, places=9)
        self.assertAlmostEqual(6.90, near, places=9)
        self.assertAlmostEqual(13.80, far, places=9)
        self.assertGreater(far - near, 6.0)

    def test_a_point_before_the_start_measures_from_the_start(self):
        self.assertAlmostEqual(
            6.90, self.dist(30.00, -97.90, 30.00, -97.80, 30.00, -97.70), places=9)

    def test_a_zero_length_segment_measures_to_the_point(self):
        """Origin and destination geocoded to the same place: the direction
        vector is zero and the projection divides by zero. It must fall back to
        the plain distance rather than raise -- this runs inside an incident
        handler, so an exception here is somebody's missing commute alert."""
        self.assertAlmostEqual(
            0.69, self.dist(30.01, -97.80, 30.00, -97.80, 30.00, -97.80), places=9)
        self.assertAlmostEqual(
            0.0, self.dist(30.00, -97.80, 30.00, -97.80, 30.00, -97.80), places=9)

    def test_the_distance_is_never_negative(self):
        for lat in (29.5, 30.0, 30.5):
            for lon in (-98.2, -97.75, -97.5):
                with self.subTest(lat=lat, lon=lon):
                    self.assertGreaterEqual(self.dist(lat, lon, 30.0, -97.8, 30.1, -97.7), 0.0)


class TestTheCorridorIsStillThreeMiles(unittest.TestCase):
    def test_both_callers_agree_on_the_width(self):
        home = _real_module("modules.commute_alerts")

        self.assertEqual(3.0, home._COMMUTE_CORRIDOR_MILES)
        self.assertIs(home._COMMUTE_CORRIDOR_MILES,
                      _real_module("modules.alerts")._COMMUTE_CORRIDOR_MILES)
        self.assertIs(home._COMMUTE_CORRIDOR_MILES,
                      _real_module("modules.commute")._COMMUTE_CORRIDOR_MILES)

    def test_the_itype_set_is_intact(self):
        home = _real_module("modules.commute_alerts")

        self.assertEqual(
            {"SHOOTING", "OFFICER DOWN", "PURSUIT", "STRUCTURE FIRE", "HAZMAT",
             "WEAPENS", "CRASH/COLLISION", "STABBING", "MASS CASUALTY"},
            home._COMMUTE_ALERT_ITYPES,
        )

    def test_the_alert_message_keeps_its_escapes_not_literal_emoji(self):
        """The copies disagreed here: one wrote the car as an escape, the other
        as a literal glyph. The escape won, so a stray encoding or an editor
        cannot change what a premium user is sent."""
        source = _HOME.read_text(encoding="utf-8")
        self.assertIn(r"\U0001f697 [COMMUTE ALERT]", source)
        self.assertIn(r"\U0001f552 Current travel time", source)
        # No literal glyph from the pictographic ranges anywhere in the module.
        glyphs = [
            (i, ch) for i, ch in enumerate(source)
            if 0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF
        ]
        self.assertEqual([], glyphs, f"literal emoji in the message: {glyphs}")


if __name__ == "__main__":
    unittest.main()