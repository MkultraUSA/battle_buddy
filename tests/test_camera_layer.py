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
  * **honesty.** The popup must not imply a surveyed position -- the city's point
    is approximate -- nor a live video stream. Austin publishes a still frame per
    camera, which is what the popup shows and links to.
  * **a camera earns its marker.** Only cameras the city publishes a frame for
    ship in the snapshot. There is no point plotting a location with nothing to
    look at behind it.
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


def _popup_js() -> str:
    """The camera popup builder, including the frame gate it depends on."""
    src = _js()
    return src[src.index("function cameraFrameUrl"):src.index("async function loadCameras")]


def _fetcher():
    """Import the fetcher so the frame gate is tested by behavior, not text."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_fetch_austin_cameras", _ROOT / "scripts" / "fetch_austin_cameras.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class TestCameraWeightTracksZoom(unittest.TestCase):
    """820 dots at one weight hazed the city and buried the incident pin.

    Found by screenshot review, not by any metric: the layer reported 820 drawn
    and every test passed while the map was effectively unusable at city zoom.
    """

    def _block(self) -> str:
        src = _js()
        start = src.index("var CAMERAS_URL")
        return src[start:src.index("async function loadMapStats")]

    def test_opacity_is_a_function_of_zoom(self):
        src = _js()
        self.assertIn("function cameraOpacityForZoom", src)
        self.assertIn("function applyCameraZoom", src)

    def test_zoomend_listener_updates_the_weight(self):
        self.assertIn("map.on('zoomend'", self._block(),
                      "the weight must follow zoom, not be fixed at draw time")

    def test_layer_is_faint_when_zoomed_out_and_full_when_close(self):
        src = _js()
        self.assertIn("CAMERA_DOT_MIN_ZOOM", src)
        self.assertIn("CAMERA_DOT_FULL_ZOOM", src)
        # The faded value must genuinely be faint, not a token reduction.
        faint = re.search(r"CAMERA_DOT_MIN_ZOOM\)\s*return\s*([\d.]+)", src)
        self.assertIsNotNone(faint, "expected an explicit faint opacity")
        self.assertLessEqual(
            float(faint.group(1)), 0.25,
            "at city zoom the layer must read as texture, not compete with incidents",
        )

    def test_default_viewport_zoom_is_faded(self):
        """The map opens at zoom 11, which must land in the faded band."""
        src = _js()
        min_zoom = int(re.search(r"CAMERA_DOT_MIN_ZOOM = (\d+)", src).group(1))
        self.assertIn("setView([30.32, -97.77], 11)", src)
        self.assertLessEqual(
            11, min_zoom,
            f"the map opens at zoom 11 but the fade only starts above {min_zoom}, "
            "so the opening view is the hazed one this change exists to fix",
        )


class TestLayerIsHonestAboutWhatItIs(unittest.TestCase):
    def test_popup_disclaims_precision_and_labels_the_frame_a_still(self):
        popup = _popup_js().lower()
        self.assertIn("approximate", popup)
        self.assertIn("published frame", popup)
        self.assertIn("not a live video stream", popup,
                      "Austin publishes a frame, not a stream; a popup that "
                      "read as live video would overclaim what you can see")

    def test_popup_offers_the_frame_and_a_full_size_link(self):
        popup = _popup_js()
        self.assertIn('class="cam-frame"', popup)
        self.assertIn('loading="lazy"', popup,
                      "one image, fetched when the popup opens -- never 820 "
                      "eager requests on page load")
        self.assertIn('target="_blank"', popup)
        self.assertIn('rel="noopener noreferrer"', popup)

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
        links = re.findall(r"<a\b[^>]*>[^<]*</a>", nav, re.IGNORECASE)
        self.assertEqual(
            [], [a for a in links if "camera" in a.lower()],
            "no nav link was supposed to be added yet",
        )


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


class TestCamerasEarnTheirMarkerByPublishingAPicture(unittest.TestCase):
    """No published picture, no marker.

    The city's active list carries a point for every camera whether or not it
    publishes an image. A dot with nothing behind it is a dead end for whoever
    clicks it, so the snapshot only carries cameras the city actually
    publishes a frame for.
    """

    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))

    def test_every_camera_in_the_snapshot_has_a_published_frame(self):
        missing = [f["properties"].get("id") for f in self.data["features"]
                   if not f["properties"].get("image")]
        self.assertEqual([], missing,
                         "a camera with no published picture must be dropped "
                         "from the snapshot, not plotted as a bare location")

    def test_frames_are_https_on_the_citys_own_host(self):
        for feature in self.data["features"]:
            self.assertRegex(
                feature["properties"]["image"],
                r"^https://cctv\.austinmobility\.io/image/[A-Za-z0-9_-]+\.jpg$",
            )

    def test_the_frame_url_names_the_same_camera(self):
        for feature in self.data["features"]:
            props = feature["properties"]
            self.assertTrue(
                props["image"].endswith(f"/image/{props['id']}.jpg"),
                "frame id and camera id must agree or a popup shows someone "
                "else's camera",
            )

    def test_the_browser_rechecks_the_host(self):
        """A committed snapshot is still data; the popup gate is the last line."""
        src = _js()
        self.assertIn("var CAMERA_FRAME_HOST = 'cctv.austinmobility.io';", src)
        gate = src[src.index("function cameraFrameUrl"):]
        gate = gate[:gate.index("function cameraFrameHtml")]
        self.assertIn("p.hostname !== CAMERA_FRAME_HOST", gate)
        self.assertIn("p.protocol !== 'https:'", gate)
        self.assertIn(r"\/image\/", gate,
                      "the gate pins the path shape, not just the host")


class TestFrameGateRejectsAnythingButTheCitysOwnUrl(unittest.TestCase):
    """Behavioral test of the fetcher's gate: feed it hostile values."""

    @classmethod
    def setUpClass(cls):
        # staticmethod, or attribute lookup would bind this as a method and
        # pass the TestCase as the first argument.
        cls.gate = staticmethod(_fetcher().frame_url)

    def test_the_citys_own_https_frame_is_kept(self):
        self.assertEqual(
            "https://cctv.austinmobility.io/image/674.jpg",
            self.gate("https://cctv.austinmobility.io/image/674.jpg"),
        )

    def test_rejected(self):
        for bad in (
            "http://cctv.austinmobility.io/image/674.jpg",       # not https
            "https://evil.example/image/674.jpg",                # other host
            "https://cctv.austinmobility.io.evil.example/1.jpg",  # suffix host
            "https://cctv.austinmobility.io/image/674.php",      # wrong path
            "https://cctv.austinmobility.io/other/674.jpg",      # wrong dir
            "https://cctv.austinmobility.io/image/674.jpg?x=1",  # query
            "https://user:pw@cctv.austinmobility.io/image/1.jpg",  # credentials
            "https://cctv.austinmobility.io:8443/image/1.jpg",   # odd port
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "",
            None,
            42,
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(self.gate(bad))


class TestBuildDropsCamerasWithoutAPicture(unittest.TestCase):
    def test_a_camera_with_no_usable_frame_never_reaches_the_snapshot(self):
        build = _fetcher().build
        good = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-97.735786, 30.260996]},
            "properties": {
                "camera_id": "674",
                "location_name": " CESAR CHAVEZ ST / 35 SVRD",
                "screenshot_address": "https://cctv.austinmobility.io/image/674.jpg",
            },
        }
        hostile = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-97.7, 30.2]},
            "properties": {
                "camera_id": "999",
                "location_name": "SOMEWHERE",
                "screenshot_address": "https://evil.example/image/999.jpg",
            },
        }
        frameless = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-97.6, 30.1]},
            "properties": {"camera_id": "1000", "location_name": "NO IMAGE"},
        }
        out = build([good, hostile, frameless])
        self.assertEqual(["674"], [f["properties"]["id"] for f in out["features"]])
        kept = out["features"][0]["properties"]
        self.assertEqual("CESAR CHAVEZ ST / 35 SVRD", kept["name"],
                         "the leading space the city adds is still trimmed")
        self.assertEqual("https://cctv.austinmobility.io/image/674.jpg", kept["image"])


if __name__ == "__main__":
    unittest.main()