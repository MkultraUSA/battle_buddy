"""
Unit tests for modules/pollers/impl/apd_cad.py.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
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


_stub_leaf("modules.config", DB_PATH="unused.db")
_stub_leaf("modules.pollers", _pi_command_queue=[], send_dm_alert=lambda *a, **kw: None)
_stub_leaf("modules.talkgroups", TGID_META={}, IGNORE_TGIDS=set())

import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_from_file("modules.pollers.base", "modules/pollers/base.py")
_stub_leaf("modules.pollers.impl")
apd_cad = _load_from_file(
    "modules.pollers.impl.apd_cad",
    "modules/pollers/impl/apd_cad.py",
)

from modules.pollers.base import BasePoller  # noqa: E402
from modules.pollers.impl.apd_cad import (  # noqa: E402
    APD_CAD_POLL_INTERVAL,
    APDCADPoller,
    _parse_cad_ts,
)


class APDCADPollerTests(unittest.TestCase):
    def test_is_base_poller_with_expected_interval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            poller = APDCADPoller(str(Path(tmpdir) / "cad.db"))
            self.assertIsInstance(poller, BasePoller)
            self.assertEqual(poller.interval, APD_CAD_POLL_INTERVAL)

    def test_parse_cad_ts_handles_valid_and_invalid_values(self):
        self.assertIsInstance(_parse_cad_ts("2026-05-04T12:30:00.000"), float)
        self.assertIsNone(_parse_cad_ts(""))
        self.assertIsNone(_parse_cad_ts("not-a-date"))

    def test_init_db_creates_cad_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "cad.db")
            APDCADPoller(db_path).init_db()

            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            conn.close()

        self.assertIn("apd_cad", tables)
        self.assertIn("tgid_sector_hints", tables)

    @mock.patch.object(apd_cad.urllib.request, "urlopen")
    def test_fetch_and_store_upserts_records(self, mock_urlopen):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "cad.db")
            poller = APDCADPoller(db_path)
            poller.init_db()
            payload = [
                {
                    "incident_number": "CAD1",
                    "response_datetime": "2026-05-04T12:30:00.000",
                    "call_closed_datetime": "2026-05-04T12:45:00.000",
                    "sector": "GE",
                    "initial_problem_category": "Shoot/Stab",
                    "final_problem_description": "Shooting call",
                    "call_disposition_description": "Report written",
                },
                {"response_datetime": "2026-05-04T12:31:00.000"},
            ]
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            response.__exit__.return_value = None
            mock_urlopen.return_value = response

            upserted = poller.fetch_and_store()

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT sector, initial_category, final_description FROM apd_cad WHERE incident_number='CAD1'"
            ).fetchone()
            conn.close()

        self.assertEqual(upserted, 1)
        self.assertEqual(row, ("GE", "Shoot/Stab", "Shooting call"))

    def test_match_and_harvest_enriches_high_confidence_incident(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "cad.db")
            poller = APDCADPoller(db_path)
            poller.init_db()
            response_ts = time.time() - 10800
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE incidents (
                    id INTEGER PRIMARY KEY,
                    ts_start REAL,
                    itype TEXT,
                    agencies TEXT,
                    description TEXT,
                    is_test INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE calls (
                    id INTEGER PRIMARY KEY,
                    ts REAL,
                    tgid INTEGER
                )
                """
            )
            conn.execute(
                "INSERT INTO incidents VALUES (1, ?, 'SHOOTING', 'APD', 'Initial desc', 0)",
                (response_ts + 60,),
            )
            conn.execute(
                """
                INSERT INTO apd_cad
                    (incident_number, response_ts, call_closed_ts, sector,
                     initial_description, initial_category, final_description,
                     disposition)
                VALUES ('CAD1', ?, ?, 'GE', 'Shoot/Stab hotshot',
                        'Shoot/Stab', 'Shooting call', 'Report written')
                """,
                (response_ts, response_ts + 600),
            )
            conn.execute("INSERT INTO calls VALUES (1, ?, 12345)", (response_ts + 10,))
            conn.execute("INSERT INTO calls VALUES (2, ?, 12345)", (response_ts + 20,))
            conn.commit()
            conn.close()

            matched, harvested = poller.match_and_harvest()

            conn = sqlite3.connect(db_path)
            cad = conn.execute(
                "SELECT matched_incident_id, match_confidence FROM apd_cad WHERE incident_number='CAD1'"
            ).fetchone()
            incident = conn.execute("SELECT description FROM incidents WHERE id=1").fetchone()
            hint = conn.execute(
                "SELECT tgid, sector, hit_count FROM tgid_sector_hints"
            ).fetchone()
            conn.close()

        self.assertEqual((matched, harvested), (1, 1))
        self.assertEqual(cad, (1, "high"))
        self.assertIn("[CAD: Shooting call, Report written, sector GE]", incident[0])
        self.assertEqual(hint, (12345, "GE", 1))

    def test_match_and_harvest_skips_known_tgid_hints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys.modules["modules.talkgroups"].TGID_META = {12345: {"tag": "Known"}}
            try:
                db_path = str(Path(tmpdir) / "cad.db")
                poller = APDCADPoller(db_path)
                poller.init_db()
                response_ts = time.time() - 10800
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "CREATE TABLE incidents (id INTEGER PRIMARY KEY, ts_start REAL, itype TEXT, agencies TEXT, description TEXT, is_test INTEGER)"
                )
                conn.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, ts REAL, tgid INTEGER)")
                conn.execute(
                    "INSERT INTO apd_cad (incident_number, response_ts, call_closed_ts, sector, initial_category) VALUES ('CAD1', ?, ?, 'GE', 'Shoot/Stab')",
                    (response_ts, response_ts + 600),
                )
                conn.execute("INSERT INTO calls VALUES (1, ?, 12345)", (response_ts + 10,))
                conn.execute("INSERT INTO calls VALUES (2, ?, 12345)", (response_ts + 20,))
                conn.commit()
                conn.close()

                matched, harvested = poller.match_and_harvest()
            finally:
                sys.modules["modules.talkgroups"].TGID_META = {}

        self.assertEqual((matched, harvested), (0, 0))


if __name__ == "__main__":
    unittest.main()
