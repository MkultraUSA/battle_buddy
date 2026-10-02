"""The Austin traffic-camera layer must be safe, subordinate, and honest.

The data is a committed snapshot of the city's public camera list. The city is a
trusted publisher, but the layer is still built so that a bad value cannot become
markup: the camera `name` is interpolated into popup HTML, and these tests assert
on the escaped string rather than merely that the page loads, which is the
technique the existing C3 tests use for the same reason.

Three things are pinned deliberately:

  * **no XSS.** A camera name containing markup must not reach the DOM as markup.
  * **incidents stay dominant.** The layer may not restyle, reorder or otherwise
    disturb the incident markers it sits behind.
  * **honesty.** The popup must not imply live video or a surveyed position --
    there is neither, and the city's data is explicitly approximate.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SNAPSHOT = _ROOT / "static" / "austin_cameras.json"
if not _SNAPSHOT.exists():
    _SNAPSHOT = _ROOT / "static" / "data" / "austin_cameras.json"
_JS = _ROOT / "static" / "js" / "public_map.js"
_PUBLIC_PY = _ROOT / "modules" / "public.py"


def _js() -> str:
    return _JS.read_text(encoding="utf-8")


class TestSnapshotIsWellFormed(unittest.TestCase):
    """A malformed snapshot would silently produce an empty map layer."""

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))

    def test_is_a_geojson_feature_collection(self):
        self.assertEqual("FeatureCollection", self.data["type"])

    def test_has_cameras(self):
        self.assertGreater(len(self.data["features"]), 500,
                           "the layer should carry the city's live camera set")

    def test_every_feature_is_a_point_with_finite_coordinates(self):
        for feat in self.data["features"]:
            with self.subTest(feat=feat.get("properties", {}).get("id")):
                self.assertEqual("Point", feat["geometry"]["type"])
                lon, lat = feat["geometry"]["coordinates"]
                self.assertIsInstance(lon, (int, float))
                self.assertIsInstance(lat, (int, float))
                # Austin. A value outside this would put a camera in the ocean.
                self.assertTrue(-98.1 < lon < -97.4, f"longitude {lon} out of range")
                self.assertTrue(29.9 < lat < 30.7, f"latitude {lat} out of range")

    def test_coordinates_are_lon_lat_order_not_swapped(self):
        """GeoJSON is [lon, lat]. A swap puts every camera in the Gulf of Mexico."""
        lons = [f["geometry"]["coordinates"][0] for f in self.data["features"]]
        lats = [f["geometry"]["coordinates"][1] for f in self.data["features"]]
        # Austin is at roughly lon -97.7, lat 30.3. Swapped, lats would cluster
        # near -97.7 and lons near 30.3, which is not a valid position for Austin.
        self.assertTrue(-98.0 < sum(lons) / len(lons) < -97.4)
        self.assertTrue(30.1 < sum(lats) / len(lats) < 30.5)

    def test_only_live_cameras(self):
        """Planned, voided and removed cameras make the layer look unreliable."""
        blob = json.dumps(self.data)
        self.assertNotIn("DESIRED", blob)
        self.assertNotIn("REMOVED", blob)
        self.assertNotIn("VOID", blob)

    def test_records_attribution_and_generation_time(self):
        self.assertIn("City of Austin", self.data["source"])
        self.assertRegex(self.data["generated"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestCameraNamesCannotInjectMarkup(unittest.TestCase):
    """The name is city-supplied text interpolated into popup HTML."""

    def test_name_is_escaped_before_entering_the_popup(self):
        src = _js()
        popup = src[src.index("function cameraPopupHtml"):src.index("async function loadCameras")]
        # Every interpolated value must pass through esc().
        for match in re.findall(r"'\s*\+\s*(\w+)\s*\+", popup):
            self.assertIn(
                "esc(", popup,
                f"popup interpolates {match!r} without esc(); a camera name "
                "containing markup would execute",
            )

    def test_popup_uses_the_existing_escaper_not_string_concatenation(self):
        self.assertIn("function esc(", _js(), "the shared escaper must exist")
        self.assertIn("esc(name)", _js())

    def test_layer_does_not_use_innerhtml(self):
        """bindPopup with an HTML string is fine; innerHTML would not be."""
        camera_block = _js()[_js().index("var CAMERAS_URL"):]
        camera_block = camera_block[:camera_block.index("async function loadMapStats")]
        self.assertNotIn(
            "innerHTML", camera_block,
            "the camera layer must not write to innerHTML",
        )


class TestIncidentsStayDominant(unittest.TestCase):
    def test_map_view_centre_and_zoom_are_unchanged(self):
        src = _js()
        self.assertIn("setView([30.32, -97.77], 11)", src)
        self.assertIn("minZoom: 10", src)

    def test_basemap_url_is_unchanged(self):
        src = _js()
        self.assertIn(
            "server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer",
            src,
        )
        self.assertIn("{z}/{y}/{x}", src)

    def test_esri_credits_are_appended_to_not_replaced(self):
        """The credits are a preservation contract of the tile source."""
        src = _js()
        attribution = re.search(r"attribution:\s*'([^']+)'", src).group(1)
        for credit in ("Esri", "HERE", "Garmin", "OpenStreetMap"):
            self.assertIn(credit, attribution, f"lost the {credit} credit")
        self.assertIn("City of Austin", attribution,
                      "the new source needs its own attribution")

    def test_camera_markers_are_muted_and_small(self):
        src = _js()
        block = src[src.index("var CAMERAS_URL"):src.index("async function loadMapStats")]
        self.assertIn("circleMarker", block)
        # Must not borrow an incident colour or grow large enough to dominate.
        for incident_colour in ("#ef4444", "#dc2626", "#f87171"):
            self.assertNotIn(
                f"color: '{incident_colour}'", block,
                "camera markers must not reuse an incident colour",
            )
        radius = re.search(r"radius:\s*(\d+)", block)
        self.assertIsNotNone(radius, "camera marker radius should be explicit")
        self.assertLessEqual(int(radius.group(1)), 5,
                             "a camera dot larger than 5px competes with incident pins")


class TestLayerIsHonestAboutWhatItIs(unittest.TestCase):
    def test_popup_disclaims_precision_and_live_video(self):
        src = _js().lower()
        popup = src[src.index("function camera popup") if "function camera popup" in src
                    else src.index("function camerapopuphtml"):]
        popup = popup[:popup.index("async function loadcameras")]
        self.assertIn("approximate", popup)
        self.assertIn("no live video", popup,
                      "the city publishes no imagery; implying otherwise would "
                      "mislead anyone who clicks a camera")

    def test_legend_entry_present_and_labelled_as_approximate(self):
        html = _PUBLIC_PY.read_text(encoding="utf-8")
        self.assertIn("City traffic camera", html)
        legend_line = html[html.index("City traffic camera"):]
        self.assertIn("approx", legend_line[:120].lower(),
                      "the legend must not imply surveyed precision")

    def test_no_navigation_link_was_added(self):
        """Decision for now: the layer is unlinked from the nav."""
        html = _PUBLIC_PY.read_text(encoding="utf-8")
        nav = html[:html.index('<div id="map">')]
        self.assertNotIn("camera", nav.lower(),
                         "no nav link was supposed to be added yet")


class TestLayerFailsSoft(unittest.TestCase):
    def test_errors_are_caught_not_thrown(self):
        src = _js()
        block = src[src.index("async function loadCameras"):]
        block = block[:block.index("async function loadMapStats")]
        self.assertIn("try {", block)
        self.assertIn("console.warn", block,
                      "a failed camera load must warn, not break the map")

    def test_empty_or_malformed_snapshot_is_skipped(self):
        src = _js()
        block = src[src.index("async function loadCameras"):]
        block = block[:block.index("async function loadMapStats")]
        self.assertIn("Array.isArray(data.features)", block)
        self.assertIn("no drawable points", block)

    def test_snapshot_is_fetched_from_the_static_prefix(self):
        self.assertIn("/static/data/austin_cameras.json", _js())
        self.assertTrue(_SNAPSHOT.exists(),
                        f"snapshot missing at {_SNAPSHOT}")


if __name__ == "__main__":
    unittest.main()