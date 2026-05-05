"""
Unit tests for modules/pollers/impl/adsb_air_asset.py.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _stub_leaf(name: str, **attrs):
    mod = type(sys)(name)
    mod.__name__ = name
    mod.__package__ = name.rsplit(".", 1)[0] if "." in name else name
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


_stub_leaf(
    "modules.config",
    DB_PATH=":memory:",
    TALK_BASE="http://talk.test",
    TALK_USER="user",
    TALK_PASS="pass",
    TALK_ROOMS={"apd": "room_apd", "incidents": "room_incidents"},
)
_stub_leaf("modules.incident_engine", _atak_post_marker=lambda *a, **kw: None)
_stub_leaf("modules.pollers", _pi_command_queue=[], send_dm_alert=lambda *a, **kw: None)
_stub_leaf("modules.pollers_legacy", send_dm_alert=lambda *a, **kw: None)

import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_from_file("modules.pollers.base", "modules/pollers/base.py")
_stub_leaf("modules.pollers.impl")
adsb = _load_from_file(
    "modules.pollers.impl.adsb_air_asset",
    "modules/pollers/impl/adsb_air_asset.py",
)

from modules.pollers.base import BasePoller  # noqa: E402
from modules.pollers.impl.adsb_air_asset import (  # noqa: E402
    ADSB_INTERVAL,
    ADSBAirAssetPoller,
    check_orbit,
)


class ADSBAirAssetPollerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        ADSBAirAssetPoller.ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts_start REAL, ts_updated REAL, itype TEXT, description TEXT, "
            "agencies TEXT, tgids TEXT, location TEXT, lat REAL, lon REAL, status TEXT)",
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def _unknown_helo(self, icao="abc123"):
        return {
            "hex": icao,
            "flight": "TEST1",
            "lat": 30.25,
            "lon": -97.75,
            "alt_baro": 1200,
            "track": 90,
            "gs": 80,
            "category": "A7",
        }

    def _leo(self):
        ac = self._unknown_helo("a820f8")
        ac["flight"] = "AIR1"
        ac["category"] = "A1"
        return ac

    def test_is_base_poller_with_expected_interval(self):
        poller = ADSBAirAssetPoller()
        self.assertIsInstance(poller, BasePoller)
        self.assertEqual(poller.interval, ADSB_INTERVAL)

    def test_normalize_filters_ground_high_and_non_helo_unknowns(self):
        self.assertIsNone(ADSBAirAssetPoller.normalize_aircraft({"hex": "abc", "lat": 1, "lon": 2, "alt_baro": "ground"}))
        self.assertIsNone(ADSBAirAssetPoller.normalize_aircraft({**self._unknown_helo(), "alt_baro": 6000}))
        self.assertIsNone(ADSBAirAssetPoller.normalize_aircraft({**self._unknown_helo(), "category": "A1"}))

        normalized = ADSBAirAssetPoller.normalize_aircraft(self._unknown_helo())
        self.assertEqual(normalized["icao24"], "abc123")
        self.assertFalse(normalized["is_leo"])
        self.assertEqual(normalized["display_label"], "Unknown Helicopter")

    def test_store_position_for_unknown_helo_and_post_marker(self):
        poller = ADSBAirAssetPoller()
        marker = mock.Mock()

        with mock.patch.object(adsb.threading.Thread, "start", lambda self: self._target(*self._args)):
            result = poller.process_aircraft(
                self._unknown_helo(),
                10_000.0,
                self.db_path,
                "http://talk.test",
                "user",
                "pass",
                {"apd": "room_apd", "incidents": "room_incidents"},
                marker,
            )

        self.assertEqual(result, "non-leo")
        marker.assert_called_once()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT icao24, is_leo, label FROM aircraft_positions").fetchone()
        conn.close()
        self.assertEqual(row, ("abc123", 0, "TEST1"))

    def test_refractory_suppresses_duplicate_alert_but_stores_position(self):
        poller = ADSBAirAssetPoller()
        marker = mock.Mock()

        with mock.patch.object(adsb.threading.Thread, "start", lambda self: self._target(*self._args)):
            self.assertEqual(
                poller.process_aircraft(
                    self._unknown_helo(),
                    10_000.0,
                    self.db_path,
                    "",
                    "",
                    "",
                    {},
                    marker,
                ),
                "non-leo",
            )
            self.assertEqual(
                poller.process_aircraft(
                    self._unknown_helo(),
                    10_100.0,
                    self.db_path,
                    "",
                    "",
                    "",
                    {},
                    marker,
                ),
                "refractory",
            )

        self.assertEqual(marker.call_count, 1)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM aircraft_positions").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    @mock.patch.object(adsb.urllib.request, "urlopen")
    def test_leo_aircraft_creates_incident_and_marker(self, mock_urlopen):
        poller = ADSBAirAssetPoller()
        marker = mock.Mock()

        with mock.patch.object(adsb.threading.Thread, "start", lambda self: self._target(*self._args)):
            result = poller.process_aircraft(
                self._leo(),
                10_000.0,
                self.db_path,
                "http://talk.test",
                "user",
                "pass",
                {"apd": "room_apd", "incidents": "room_incidents"},
                marker,
            )

        self.assertEqual(result, "leo")
        marker.assert_called_once()
        self.assertTrue(mock_urlopen.called)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT itype, location FROM incidents").fetchone()
        pos = conn.execute("SELECT icao24, is_leo, label FROM aircraft_positions").fetchone()
        conn.close()
        self.assertEqual(row, ("AIR ASSET ACTIVE", "Austin airspace"))
        self.assertEqual(pos, ("a820f8", 1, "APD Air1 (N6227)"))

    def test_check_orbit_requires_tight_track_and_heading_span(self):
        now = 10_000.0
        conn = sqlite3.connect(self.db_path)
        headings = [0, 45, 90, 180, 225, 270]
        for idx, heading in enumerate(headings):
            conn.execute(
                "INSERT INTO aircraft_positions "
                "(ts,icao24,callsign,lat,lon,alt_ft,heading,speed_kts,is_leo,label) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now - 60 + idx, "a820f8", "AIR1", 30.25 + idx * 0.0001, -97.75, 1000, heading, 80, 1, "APD Air1"),
            )
        conn.commit()
        conn.close()

        self.assertTrue(check_orbit(self.db_path, "a820f8", now))

    @mock.patch.object(adsb.urllib.request, "urlopen")
    def test_detect_orbits_creates_orbit_incident_and_notifications(self, mock_urlopen):
        poller = ADSBAirAssetPoller()
        marker = mock.Mock()
        send_alert = mock.Mock()
        now = 10_000.0
        conn = sqlite3.connect(self.db_path)
        for idx, heading in enumerate([0, 60, 120, 180, 240, 300]):
            conn.execute(
                "INSERT INTO aircraft_positions "
                "(ts,icao24,callsign,lat,lon,alt_ft,heading,speed_kts,is_leo,label) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now - 60 + idx, "a820f8", "AIR1", 30.25, -97.75 + idx * 0.0001, 1000, heading, 80, 1, "APD Air1"),
            )
        conn.commit()
        conn.close()

        with mock.patch.object(adsb.threading.Thread, "start", lambda self: self._target(*self._args)):
            poller.detect_orbits(
                self.db_path,
                now,
                "http://talk.test",
                "user",
                "pass",
                {"apd": "room_apd", "incidents": "room_incidents"},
                marker,
                send_alert,
            )

        self.assertEqual(mock_urlopen.call_count, 2)
        marker.assert_called_once()
        send_alert.assert_called_once()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT itype FROM incidents").fetchone()
        conn.close()
        self.assertEqual(row, ("AIR ASSET ORBIT",))


if __name__ == "__main__":
    unittest.main()
