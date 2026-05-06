"""
Unit tests for modules/sitrep.py.
"""

from __future__ import annotations

import datetime as _dt
import sys
import unittest
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
    "modules.database",
    active_incidents=lambda: [],
    calls_for_sitrep=lambda _minutes: [],
)

import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


sitrep = _load_from_file("modules.sitrep", "modules/sitrep.py")

from modules.sitrep import build_sitrep, build_voice_sitrep  # noqa: E402


class SitrepTests(unittest.TestCase):
    def now(self):
        return _dt.datetime(2026, 5, 5, 21, 30, tzinfo=_dt.timezone(_dt.timedelta(hours=-5), "CDT"))

    def test_build_sitrep_handles_no_calls_or_incidents(self):
        text = build_sitrep(
            15,
            calls_provider=lambda _minutes: [],
            incidents_provider=lambda: [],
            now_func=self.now,
        )

        self.assertIn("SITUATION REPORT — last 15 min — 2026-05-05 21:30 CDT", text)
        self.assertIn("Total calls: 0", text)
        self.assertIn("No active incidents.", text)
        self.assertIn("No calls in the last 15 minutes.", text)

    def test_build_sitrep_summarizes_incidents_and_priority_calls(self):
        calls = [
            {
                "ts": 1_000.0,
                "tag": "APD DISP",
                "tgid": 1001,
                "category": "APD",
                "location": "100 Congress Ave",
                "transcript": "shots fired reported",
                "groq": {"priority": "LOW", "description": "possible shooting"},
            },
            {
                "ts": 1_060.0,
                "tag": "AFD DISP",
                "tgid": 2001,
                "category": "AFD",
                "location": None,
                "transcript": "minor crash blocking lane",
                "groq": {"priority": "MED"},
            },
            {
                "ts": 1_120.0,
                "tag": "EMS",
                "tgid": 3001,
                "category": "EMS",
                "location": None,
                "transcript": "routine patient transport",
                "groq": {},
            },
        ]
        incidents = [
            {
                "itype": "SHOOTING",
                "location": "100 Congress Ave",
                "ts_start": 9_400.0,
                "ts_updated": 9_700.0,
                "agencies": '["APD", "EMS"]',
                "description": "Active investigation",
            },
            {
                "itype": "TEST",
                "is_test": True,
                "ts_start": 9_900.0,
                "ts_updated": 9_900.0,
                "agencies": "[]",
                "description": "ignore",
            },
        ]

        text = build_sitrep(
            60,
            calls_provider=lambda _minutes: calls,
            incidents_provider=lambda: incidents,
            now_func=self.now,
            time_func=lambda: 10_000.0,
        )

        self.assertIn("[SHOOTING] @ 100 Congress Ave — started 10m ago", text)
        self.assertIn("*** HIGH PRIORITY ***", text)
        self.assertIn("🔴", text)
        self.assertIn("APD DISP @ 100 Congress Ave: shots fired reported", text)
        self.assertIn("→ possible shooting", text)
        self.assertIn("*** NOTABLE ***", text)
        self.assertIn("🟡", text)
        self.assertIn("AFD DISP: minor crash blocking lane", text)
        self.assertIn("APD: 1", text)
        self.assertIn("AFD: 1", text)
        self.assertIn("EMS: 1", text)
        self.assertNotIn("[TEST]", text)

    def test_build_voice_sitrep_summarizes_for_tts(self):
        calls = [
            {"category": "APD"},
            {"category": "APD"},
            {"category": "AFD"},
        ]
        incidents = [
            {
                "itype": "CRASH/COLLISION",
                "location": "I-35",
                "ts_start": 9_700.0,
                "agencies": '["APD", "AFD", "EMS", "DPS"]',
            }
        ]

        text = build_voice_sitrep(
            30,
            calls_provider=lambda _minutes: calls,
            incidents_provider=lambda: incidents,
            now_func=self.now,
            time_func=lambda: 10_000.0,
        )

        self.assertIn("Battle Buddy. Austin Metro situation report as of 9:30 PM CDT.", text)
        self.assertIn("1 active incident.", text)
        self.assertIn("CRASH or COLLISION at I-35, detected 5 minutes ago, APD, AFD, EMS responding.", text)
        self.assertIn("3 calls monitored in the past 30 minutes across APD 2, AFD 1.", text)


if __name__ == "__main__":
    unittest.main()
