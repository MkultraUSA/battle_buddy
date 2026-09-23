"""
tests/test_homicide_means.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for modules.homicide_count.means_of() — server-side means-of-death
classification for the homicide map.

Why server-side: the classifier used to live in inline page JavaScript inside
a Python triple-quoted string, where \b regex escapes were silently eaten at
import time and every marker rendered UNKNOWN grey. Classification now runs in
Python (tested here) and the API ships a `means` field the static JS renders.
"""
from __future__ import annotations

import unittest

from modules.homicide_count import MEANS_COLORS, means_of


class TestMeansOf(unittest.TestCase):
    def test_shooting(self):
        self.assertEqual(means_of("Shot at party in event center"), "SHOOTING")

    def test_gunshot_wounds(self):
        self.assertEqual(
            means_of("Found deceased with gunshot wounds during welfare check"),
            "SHOOTING",
        )

    def test_constable_is_not_a_stabbing(self):
        # Word boundaries matter: "conSTABle" must not match.
        self.assertEqual(
            means_of("Off-duty constable shot working security at Club Rodeo"),
            "SHOOTING",
        )

    def test_shot_beats_knife(self):
        # Victim was shot; knife belonged to the encounter, not the cause.
        self.assertEqual(
            means_of("Shot by driver after breaking vehicle window with knife"),
            "SHOOTING",
        )

    def test_stabbing(self):
        self.assertEqual(
            means_of("Stabbing on S Lamar; Hunter found dead holding knife"),
            "STABBING",
        )

    def test_stabbed(self):
        self.assertEqual(
            means_of("Found stabbed multiple times at home"), "STABBING"
        )

    def test_blunt_force(self):
        self.assertEqual(
            means_of("Found behind apartments; blunt force trauma"), "OTHER"
        )

    def test_fentanyl(self):
        self.assertEqual(
            means_of("Student found unresponsive; fentanyl death", "Ethan Westgard"),
            "OTHER",
        )

    def test_victim_field_counts(self):
        self.assertEqual(means_of("", "Knifed victim"), "STABBING")

    def test_unknown_when_no_signal(self):
        self.assertEqual(
            means_of("Homicide investigation at 5012 East 7th Street"), "UNKNOWN"
        )

    def test_none_inputs(self):
        self.assertEqual(means_of(None, None), "UNKNOWN")

    def test_colors_cover_all_means(self):
        for means in ("SHOOTING", "STABBING", "OTHER", "UNKNOWN"):
            self.assertIn(means, MEANS_COLORS)


if __name__ == "__main__":
    unittest.main()
