"""The recorder's tag is authoritative for category and for the ignore list.

The server carries its own `gatrrs-tags.tsv`, and it had drifted badly out of
step with what the recorders actually send. Measured on production:

| tgid   | server TSV says      | recorder sends      | consequence                        |
|--------|----------------------|---------------------|------------------------------------|
| 2306   | *absent*             | GB Juv JC Main      | 723 calls/wk, never ignorable      |
| 2307   | Education School     | GB Juv JC Move      | 288 calls/wk, never ignorable      |
| 2403   | Transportation Trans | TCSO BAKER-EAST     | filed as "Transportation"          |
| 2405   | (TCEMS)              | TCSO ADAM-WEST      | a sheriff channel filed as EMS     |
| 3476   | (unknown)            | Bee Cave PD 1       | pinned downtown, ~12 mi out        |

`IGNORE_TAGS` already contained "GB Juv" and "Juv JC" -- the decision to drop
juvenile-detention traffic had been made -- but `receive()` only consulted the
TGID set built from the drifted TSV, so it never fired.

These tests pin the resolution, and pin that real dispatch traffic is NOT dropped.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Suite hygiene. Several modules here install stub `modules.*` entries into
# sys.modules at COLLECTION time and never remove them; a stubbed
# `modules.talkgroups` has no __file__ and makes this import fail with
# "cannot import name 'CAT_COORDS' ... (unknown location)". Evict the stub so the
# real module is loaded -- same pattern as the modules.config eviction in
# test_premium_dead_routes_removed.py and four other suites.
for _stubbed in ("modules.talkgroups", "modules.config"):
    _mod = sys.modules.get(_stubbed)
    if _mod is not None and getattr(_mod, "__file__", None) is None:
        del sys.modules[_stubbed]

from modules.talkgroups import (  # noqa: E402
    CAT_COORDS,
    _tag_is_ignored,
    _tag_to_category,
)


class TestIgnoreHonoursTheRecorderTag(unittest.TestCase):
    """Tags already on IGNORE_TAGS must be droppable by tag, not only by TGID."""

    def test_juvenile_detention_traffic_is_ignored(self):
        for tag in ("GB Juv JC Main", "GB Juv JC Move",
                    "GB Juv JC Secur", "GB Juv JC TRT"):
            with self.subTest(tag=tag):
                self.assertTrue(_tag_is_ignored(tag),
                                f"{tag!r} must be droppable -- 1,011 calls a week, "
                                "a third of them untranscribable")

    def test_real_dispatch_traffic_is_not_ignored(self):
        """The cost of a wrong answer here is a dropped incident."""
        keep = [
            "TCSO BAKER-EAST", "TCSO ADAM-WEST", "TCEMS Control", "TCEMS MedCom N",
            "TCEMS Dispatch", "AFD Locution", "AFD Firecom N", "AFD TAC",
            "APD Emergency", "APD Tac 2", "Bee Cave PD 1", "Pflug PD Ch A",
            "Lakeway PD 1", "UT PD Dispatch",
        ]
        for tag in keep:
            with self.subTest(tag=tag):
                self.assertFalse(_tag_is_ignored(tag), f"{tag!r} must not be dropped")


class TestCategoryComesFromTheTag(unittest.TestCase):
    def test_a_sheriff_channel_is_not_filed_as_ems(self):
        self.assertEqual(_tag_to_category("TCSO ADAM-WEST"), "TCSO")
        self.assertEqual(_tag_to_category("TCSO BAKER-EAST"), "TCSO")

    def test_agency_channels_resolve(self):
        expected = {
            "TCEMS Control": "TCEMS",
            "TCEMS MedCom N": "TCEMS",
            "AFD Locution": "AFD",
            "AFD Firecom N": "AFD",
            "APD Emergency": "APD",
            "UT PD Dispatch": "UTPD",
        }
        for tag, want in expected.items():
            with self.subTest(tag=tag):
                self.assertEqual(_tag_to_category(tag), want)

    def test_suburban_jurisdictions_have_their_own_coordinates(self):
        """Bee Cave was landing on the downtown default -- roughly 12 miles out."""
        cat = _tag_to_category("Bee Cave PD 1")
        self.assertEqual(cat, "BeeCave")
        lat, lon = CAT_COORDS[cat]
        self.assertNotEqual(
            (lat, lon), CAT_COORDS["Unknown"],
            "Bee Cave must not share the downtown default pin",
        )
        # inside the Austin metro, west of downtown
        self.assertAlmostEqual(lat, 30.3085, places=3)
        self.assertAlmostEqual(lon, -97.9450, places=3)

    def test_every_category_has_coordinates(self):
        """A category with no CAT_COORDS entry raises KeyError at call time."""
        for tag in ("TCSO BAKER-EAST", "AFD Locution", "TCEMS Control",
                    "APD Emergency", "Bee Cave PD 1", "Lakeway PD 1",
                    "Pflug PD Ch A", "UT PD Dispatch"):
            cat = _tag_to_category(tag)
            with self.subTest(tag=tag):
                self.assertIn(cat, CAT_COORDS, f"{cat!r} has no coordinates")


class TestReceiveWiring(unittest.TestCase):
    """`receive()` must actually consult the tag, not just define the helpers."""

    def test_receive_imports_the_underscore_helpers(self):
        src = (_ROOT / "audio_receiver.py").read_text()
        # `from modules.talkgroups import *` does NOT bring in underscore names,
        # so without an explicit import these raise NameError at request time.
        self.assertIn(
            "_tag_is_ignored", src,
            "audio_receiver must import the helpers explicitly; star-import skips "
            "underscore-prefixed names",
        )
        self.assertRegex(
            src,
            r"from modules\.talkgroups import [^\n]*_tag_is_ignored",
            "the helper is referenced but never imported",
        )

    def test_ignore_check_uses_the_incoming_tag(self):
        src = (_ROOT / "audio_receiver.py").read_text()
        self.assertRegex(
            src,
            r"if tgid in IGNORE_TGIDS or \(incoming_tag and _tag_is_ignored\(incoming_tag\)\)",
            "the ignore check must consider the recorder's tag, or IGNORE_TAGS "
            "never fires for talkgroups missing from the server TSV",
        )

    def test_category_is_derived_from_the_tag(self):
        src = (_ROOT / "audio_receiver.py").read_text()
        self.assertIn(
            "_tag_to_category(tag) if pi_tag else meta.get(\"cat\", \"Unknown\")",
            src,
            "category must come from the resolved tag; the TSV is the drifted source",
        )

    def test_the_tag_is_sanitised_once_and_reused(self):
        """Sanitising twice risks the ignore check and the label disagreeing."""
        src = (_ROOT / "audio_receiver.py").read_text()
        body = src[src.index("def receive():"):]
        self.assertEqual(
            body.count("_sanitize_pi_tag("), 1,
            "_sanitize_pi_tag must be called once; a second call could disagree "
            "with the ignore decision already made",
        )
        self.assertIn("pi_tag   = incoming_tag", body)


if __name__ == "__main__":
    unittest.main()