"""
Unit tests for modules/pollers/impl/austin_events.py.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock as mock
from datetime import date
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
    TALK_BASE="http://talk.test",
    TALK_USER="user",
    TALK_PASS="pass",
    TALK_ROOMS={"incidents": "room_incidents"},
)
_stub_leaf("modules.pollers", _pi_command_queue=[], send_dm_alert=lambda *a, **kw: None)

import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_from_file("modules.pollers.base", "modules/pollers/base.py")
_stub_leaf("modules.pollers.impl")
austin_events = _load_from_file(
    "modules.pollers.impl.austin_events",
    "modules/pollers/impl/austin_events.py",
)

from modules.pollers.base import BasePoller  # noqa: E402
from modules.pollers.impl.austin_events import (  # noqa: E402
    AUSTIN_EVENTS_POLL,
    AustinEventsPoller,
    _format_events,
    _upcoming_events,
)


class AustinEventsPollerTests(unittest.TestCase):
    def test_is_base_poller_with_expected_interval(self):
        poller = AustinEventsPoller()
        self.assertIsInstance(poller, BasePoller)
        self.assertEqual(poller.interval, AUSTIN_EVENTS_POLL)

    def test_upcoming_events_filters_and_sorts_window(self):
        doc = {
            "events": [
                {"id": "late", "name": "Late", "start": "2026-05-20"},
                {"id": "bad", "name": "Bad"},
                {"id": "two", "name": "Two", "start": "2026-05-06"},
                {"id": "one", "name": "One", "start": "2026-05-04", "end": "2026-05-05"},
            ]
        }

        events = _upcoming_events(doc, date(2026, 5, 4))

        self.assertEqual([event["id"] for event in events], ["one", "two"])

    def test_format_events_includes_impact_details(self):
        msg = _format_events(
            [
                {
                    "id": "sx",
                    "name": "South Festival",
                    "start": "2026-05-04",
                    "end": "2026-05-06",
                    "tier": "major",
                    "blast_radius_mi": 3,
                    "venue": "Downtown",
                }
            ],
            date(2026, 5, 4),
        )

        self.assertIn("This week in Austin", msg)
        self.assertIn("2026-05-04 -> 2026-05-06", msg)
        self.assertIn("MAJOR regional impact", msg)
        self.assertIn("3mi radius", msg)

    @mock.patch.object(austin_events.urllib.request, "urlopen")
    def test_run_posts_when_event_ids_change_and_saves_state(self, mock_urlopen):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.json"
            state_path = Path(tmpdir) / "state.json"
            events_path.write_text(
                json.dumps(
                    {
                        "events": [
                            {"id": "one", "name": "One", "start": "2026-05-04"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"last_post_date": "2026-05-01", "last_event_ids": []}),
                encoding="utf-8",
            )

            poller = AustinEventsPoller(str(events_path), str(state_path))
            with mock.patch.object(poller, "_today", return_value=date(2026, 5, 4)):
                poller.run()

            self.assertTrue(mock_urlopen.called)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_post_date"], "2026-05-04")
            self.assertEqual(state["last_event_ids"], ["one"])

    @mock.patch.object(austin_events.urllib.request, "urlopen")
    def test_run_stays_quiet_when_state_is_current(self, mock_urlopen):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.json"
            state_path = Path(tmpdir) / "state.json"
            events_path.write_text(
                json.dumps(
                    {
                        "events": [
                            {"id": "one", "name": "One", "start": "2026-05-04"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"last_post_date": "2026-05-04", "last_event_ids": ["one"]}),
                encoding="utf-8",
            )

            poller = AustinEventsPoller(str(events_path), str(state_path))
            with mock.patch.object(poller, "_today", return_value=date(2026, 5, 4)):
                poller.run()

        self.assertFalse(mock_urlopen.called)

    @mock.patch.object(austin_events.urllib.request, "urlopen")
    def test_post_to_talk_skips_missing_config(self, mock_urlopen):
        AustinEventsPoller._post_to_talk("summary", "", "user", "pass", {"incidents": "room"})
        AustinEventsPoller._post_to_talk("summary", "http://talk.test", "user", "pass", {})

        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
