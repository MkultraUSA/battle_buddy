"""Tests for the isolated aircraft API and page module."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from flask import Flask

import modules.aircraft as aircraft

_ROOT = Path(__file__).parent.parent


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


if __name__ == "__main__":
    unittest.main()
