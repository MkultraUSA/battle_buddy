"""Tests for the isolated aircraft API and page module."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from flask import Flask

import modules.aircraft as aircraft
import modules.public as public

_ROOT = Path(__file__).parent.parent

# The preserved Esri basemap contract: Esri's tile service is {z}/{y}/{x}
# (the opposite of the OpenStreetMap leaflet {z}/{x}/{y} order).
_ESRI_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
)
_OSM_LEAFLET_HOST = "tile.openstreetmap.org"
_STATS_URL = "https://kevinwatkins.grafana.net/public-dashboards/235baceac1774dfe8bd12c242acbd014"
_NAV_HREFS = (
    "/public",
    "/public/aircraft",
    "/public/homicides",
    "/public/feed",
    "/public/about",
    "/tip",
)
# The splash is a landing page: its footer carries the section links and the
# Stats dashboard but has never had a Submit Tip link.
_SPLASH_FOOTER_HREFS = (
    "/public",
    "/public/aircraft",
    "/public/homicides",
    "/public/feed",
    "/public/about",
)
_ATTRIBUTION_RE = re.compile(r"attribution:\s*'([^']*)'")


class AircraftModuleTests(unittest.TestCase):
    def setUp(self):
        fd, tmp_name = tempfile.mkstemp()
        os.close(fd)
        self.tmp_path = Path(tmp_name)
        self.original_db_path = aircraft.DB_PATH
        aircraft.DB_PATH = str(self.tmp_path)
        with closing(sqlite3.connect(self.tmp_path)) as conn:
            conn.execute(
                """CREATE TABLE aircraft_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    icao24 TEXT NOT NULL,
                    callsign TEXT,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    alt_ft INTEGER,
                    heading REAL,
                    speed_kts REAL,
                    is_leo INTEGER DEFAULT 0,
                    label TEXT
                )"""
            )
            conn.commit()

        with aircraft._snapshot_lock:
            aircraft._snapshot.update(now=0.0, received_at=0.0, aircraft=[])

        app = Flask(__name__, template_folder=str(_ROOT / "templates"))
        app.register_blueprint(aircraft.aircraft_bp)
        app.testing = True
        self.client = app.test_client()
        self.env = mock.patch.dict(
            os.environ,
            {"BB_ADSB_INGEST_TOKEN": "test-ingest-token"},
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        aircraft.DB_PATH = self.original_db_path
        self.tmp_path.unlink(missing_ok=True)

    def test_live_snapshot_starts_stale(self):
        response = self.client.get("/api/adsb/live")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["stale"])
        self.assertEqual(response.json["aircraft"], [])

    def test_ingest_requires_token_and_sanitizes_snapshot(self):
        payload = {
            "now": 1_700_000_000,
            "aircraft": [
                {
                    "hex": "A820F8",
                    "flight": " AIR1 ",
                    "lat": 30.27,
                    "lon": -97.74,
                    "alt_baro": 1200,
                    "gs": 82,
                    "track": 91,
                    "category": "A7",
                    "dbFlags": 8,
                    "squawk": "7700",
                },
                {"hex": "outside", "lat": 31.0, "lon": -97.74},
            ],
        }

        unauthorized = self.client.post("/api/adsb/ingest", json=payload)
        self.assertEqual(unauthorized.status_code, 401)

        accepted = self.client.post(
            "/api/adsb/ingest",
            json=payload,
            headers={"Authorization": "Bearer test-ingest-token"},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json["aircraft"], 1)

        snapshot = self.client.get("/api/adsb/live").json
        self.assertFalse(snapshot["stale"])
        self.assertEqual(len(snapshot["aircraft"]), 1)
        item = snapshot["aircraft"][0]
        self.assertEqual(item["hex"], "a820f8")
        self.assertEqual(item["flight"], "AIR1")
        self.assertTrue(item["is_helicopter"])
        self.assertTrue(item["is_known_leo"])
        self.assertTrue(item["is_ladd"])
        self.assertTrue(item["is_emergency"])

    def test_local_aircraft_returns_latest_position_and_trail(self):
        now = time.time()
        rows = [
            (now - 20, "abc123", "TEST", 30.25, -97.75, 900, 80, 70, 0, "Test"),
            (now - 10, "abc123", "TEST", 30.26, -97.74, 1000, 90, 75, 0, "Test"),
        ]
        with closing(sqlite3.connect(self.tmp_path)) as conn:
            conn.executemany(
                """INSERT INTO aircraft_positions
                   (ts, icao24, callsign, lat, lon, alt_ft, heading, speed_kts, is_leo, label)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()

        response = self.client.get("/api/adsb")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json), 1)
        self.assertEqual(response.json[0]["lat"], 30.26)
        self.assertEqual(len(response.json[0]["trail"]), 2)

    def test_aircraft_page_uses_network_feed_and_typed_icons(self):
        response = self.client.get("/public/aircraft")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("fetch('/api/adsb/live'", html)
        self.assertIn("AIRCRAFT_SVGS", html)
        self.assertIn("helicopter", html)
        self.assertIn("light-aircraft", html)
        self.assertIn("airliner", html)

    # -- Esri basemap + attribution (preservation contract) ------------------
    def test_aircraft_page_uses_esri_tiles_and_required_attribution(self):
        html = self.client.get("/public/aircraft").get_data(as_text=True)
        _assert_esri_basemap(self, html, "templates/aircraft.html")
        attribution = _attribution(html)
        self.assertIn("ADSB.lol", attribution)
        self.assertIn("ODbL 1.0", attribution)

    # -- Nav completeness + external link hardening --------------------------
    def test_aircraft_page_nav_links_are_complete_and_hardened(self):
        html = self.client.get("/public/aircraft").get_data(as_text=True)
        for href in _NAV_HREFS:
            self.assertIn(f'href="{href}"', html, f"aircraft nav missing {href}")
        self.assertIn(_STATS_URL, html)
        self.assertIn('target="_blank" rel="noopener"', html)
        _assert_all_blank_links_hardened(self, html, "templates/aircraft.html")


class _BlankTargetLinkParser(HTMLParser):
    """Collect every anchor that opens a new browsing context."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blank_links: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrd = {k: (v or "") for k, v in attrs}
        if attrd.get("target") == "_blank":
            self.blank_links.append({"href": attrd.get("href", ""), "rel": attrd.get("rel", "")})


def _blank_links(html: str) -> list[dict[str, str]]:
    parser = _BlankTargetLinkParser()
    parser.feed(html)
    return parser.blank_links


def _attribution(html: str) -> str:
    match = _ATTRIBUTION_RE.search(html)
    assert match, "no Leaflet tileLayer attribution string found"
    return match.group(1)


def _assert_all_blank_links_hardened(case, html: str, surface: str) -> None:
    """Every target=_blank link on a public surface must carry rel=noopener."""
    links = _blank_links(html)
    case.assertTrue(links, f"{surface}: expected at least one target=_blank link to verify")
    for link in links:
        case.assertIn(
            "noopener",
            link["rel"].lower(),
            f"{surface}: target=_blank link to {link['href']!r} is missing rel=noopener",
        )


def _assert_esri_basemap(case, html: str, surface: str) -> None:
    """The Esri basemap must keep its {z}/{y}/{x} path order and full credits."""
    case.assertIn(_ESRI_TILE_URL, html, f"{surface}: Esri tile URL is missing or reordered")
    case.assertIn("{z}/{y}/{x}", html, f"{surface}: Esri tile path must be {{z}}/{{y}}/{{x}}")
    case.assertNotIn(
        _OSM_LEAFLET_HOST,
        html,
        f"{surface}: the OpenStreetMap leaflet tile host came back; the basemap must stay Esri",
    )
    attribution = _attribution(html)
    for credit in ("Tiles &copy; Esri", "Esri", "HERE", "Garmin", "OpenStreetMap contributors"):
        case.assertIn(credit, attribution, f"{surface}: attribution is missing {credit!r}")


class MapSurfacePreservationTests(unittest.TestCase):
    """The public map surfaces must keep the Esri basemap and the shared nav.

    These assert on the rendered HTML constants directly: the splash, map,
    feed, about and homicide routes return static documents, so this needs no
    database, no network and no running service.
    """

    # surface label, HTML getter, the hrefs its nav/footer must contain,
    # whether the surface is expected to carry the basemap.
    SURFACES = (
        (
            "modules/public.py PUBLIC_SPLASH_HTML",
            lambda: public.PUBLIC_SPLASH_HTML,
            _SPLASH_FOOTER_HREFS,
            False,
        ),
        ("modules/public.py PUBLIC_MAP_HTML", lambda: public.PUBLIC_MAP_HTML, _NAV_HREFS, True),
        ("modules/public.py PUBLIC_FEED_HTML", lambda: public.PUBLIC_FEED_HTML, _NAV_HREFS, False),
        (
            "modules/public.py PUBLIC_ABOUT_HTML",
            lambda: public.PUBLIC_ABOUT_HTML,
            _NAV_HREFS,
            False,
        ),
        (
            "modules/public.py HOMICIDE_MAP_HTML",
            lambda: public.HOMICIDE_MAP_HTML,
            _NAV_HREFS,
            False,
        ),
    )

    def test_map_surface_uses_esri_tiles_and_required_attribution(self):
        for name, get_html, _hrefs, has_map in self.SURFACES:
            with self.subTest(surface=name):
                html = get_html()
                if has_map:
                    _assert_esri_basemap(self, html, name)
                else:
                    self.assertNotIn(
                        _OSM_LEAFLET_HOST,
                        html,
                        f"{name}: the OpenStreetMap leaflet tile host came back",
                    )

    def test_map_surface_nav_links_are_present(self):
        for name, get_html, hrefs, _has_map in self.SURFACES:
            with self.subTest(surface=name):
                html = get_html()
                for href in hrefs:
                    self.assertIn(f'href="{href}"', html, f"{name}: nav/footer missing {href}")

    def test_map_surface_stats_link_is_present_and_hardened(self):
        for name, get_html, _hrefs, _has_map in self.SURFACES:
            with self.subTest(surface=name):
                html = get_html()
                self.assertIn(_STATS_URL, html, f"{name}: Stats dashboard link missing")
                self.assertIn(
                    f'href="{_STATS_URL}" target="_blank" rel="noopener"',
                    html,
                    f"{name}: the Stats link must open in a new tab with rel=noopener",
                )

    def test_interactive_surfaces_expose_the_tip_link(self):
        for name, get_html, hrefs, _has_map in self.SURFACES:
            with self.subTest(surface=name):
                if hrefs is _SPLASH_FOOTER_HREFS:
                    continue
                self.assertIn('href="/tip"', get_html(), f"{name}: Submit Tip link missing")

    def test_every_external_blank_link_is_noopener(self):
        for name, get_html, _hrefs, _has_map in self.SURFACES:
            with self.subTest(surface=name):
                _assert_all_blank_links_hardened(self, get_html(), name)

    def test_homicide_map_data_source_link_is_hardened(self):
        self.assertIn(
            'href="https://www.austintexas.gov/news?field_news_type_tid=75" '
            'target="_blank" rel="noopener"',
            public.HOMICIDE_MAP_HTML,
            "homicide map austintexas.gov link must be a hardened target=_blank",
        )

    def test_splash_footer_stats_link_is_hardened(self):
        self.assertIn(
            f'href="{_STATS_URL}" target="_blank" rel="noopener" '
            'style="color:#10b981;text-decoration:none"',
            public.PUBLIC_SPLASH_HTML,
            "splash footer Stats link must keep rel=noopener ahead of its style attribute",
        )


if __name__ == "__main__":
    unittest.main()
