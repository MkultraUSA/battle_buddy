"""
Unit tests for modules/pollers/impl/traffic_open_data.py.
"""

from __future__ import annotations

import json
import sys
import threading
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
    TALK_BASE="http://talk.test",
    TALK_USER="user",
    TALK_PASS="pass",
    TALK_ROOMS={"incidents": "room_incidents"},
)
_stub_leaf(
    "modules.incident_engine",
    _active_incidents={},
    _atak_clear_marker=lambda *a, **kw: None,
    _atak_post_marker=lambda *a, **kw: None,
    _haversine_km=lambda *a, **kw: 999,
    _incident_lock=threading.Lock(),
)
_stub_leaf("modules.pollers", _pi_command_queue=[])

import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_from_file("modules.pollers.base", "modules/pollers/base.py")
_stub_leaf("modules.pollers.impl")
traffic_open_data = _load_from_file(
    "modules.pollers.impl.traffic_open_data",
    "modules/pollers/impl/traffic_open_data.py",
)

from modules.pollers.base import BasePoller  # noqa: E402
from modules.pollers.impl.traffic_open_data import (  # noqa: E402
    TRAFFIC_POLL_INTERVAL,
    TrafficOpenDataPoller,
    _traffic_issue_to_itype,
)


class TrafficOpenDataPollerTests(unittest.TestCase):
    def _incident(self, report_id="T1", issue="Crash urgent", lat="30.25", lon="-97.75"):
        return {
            "traffic_report_id": report_id,
            "issue_reported": issue,
            "address": "100 Congress Ave",
            "latitude": lat,
            "longitude": lon,
            "published_date": "2026-05-04T12:30:00",
            "agency": "ATD",
        }

    def test_is_base_poller_with_expected_interval(self):
        poller = TrafficOpenDataPoller()
        self.assertIsInstance(poller, BasePoller)
        self.assertEqual(poller.interval, TRAFFIC_POLL_INTERVAL)
        self.assertEqual(poller._active_ids, {})

    def test_issue_mapping_uses_first_word(self):
        self.assertEqual(_traffic_issue_to_itype("Crash urgent"), "CRASH/COLLISION")
        self.assertEqual(_traffic_issue_to_itype("Flooded road"), "FLOODING")
        self.assertEqual(_traffic_issue_to_itype("Mystery"), "TRAFFIC INCIDENT")
        self.assertEqual(_traffic_issue_to_itype(""), "TRAFFIC INCIDENT")

    def test_unmatched_significant_incident_posts_and_creates_marker(self):
        poller = TrafficOpenDataPoller()
        post_marker = mock.Mock()

        with mock.patch.object(traffic_open_data.threading.Thread, "start", lambda self: self._target(*self._args)), \
             mock.patch.object(traffic_open_data.urllib.request, "urlopen"):
            matched_id = poller._process_incident(
                self._incident("T1", "Crash urgent"),
                "http://talk.test",
                "user",
                "pass",
                {"incidents": "room"},
                {},
                threading.Lock(),
                lambda *args: 999,
                post_marker,
            )

        self.assertIsNone(matched_id)
        self.assertIn("T1", poller._active_ids)
        marker_id = poller._active_ids["T1"]["atak_marker_id"]
        self.assertLessEqual(marker_id, -100001)
        post_marker.assert_called_once_with(
            marker_id,
            30.25,
            -97.75,
            "CRASH/COLLISION",
            "100 Congress Ave",
        )

    @mock.patch.object(traffic_open_data.urllib.request, "urlopen")
    def test_matched_minor_incident_posts_without_marker(self, mock_urlopen):
        poller = TrafficOpenDataPoller()
        active = {42: {"lat": 30.2501, "lon": -97.7501}}
        post_marker = mock.Mock()

        with mock.patch.object(traffic_open_data.threading.Thread, "start", lambda self: self._target(*self._args)):
            matched_id = poller._process_incident(
                self._incident("T2", "Stalled vehicle"),
                "http://talk.test",
                "user",
                "pass",
                {"incidents": "room"},
                active,
                threading.Lock(),
                lambda *args: 0.1,
                post_marker,
            )

        self.assertEqual(matched_id, 42)
        self.assertNotIn("atak_marker_id", poller._active_ids["T2"])
        self.assertFalse(post_marker.called)
        self.assertTrue(mock_urlopen.called)

    def test_duplicate_incident_is_ignored(self):
        poller = TrafficOpenDataPoller()
        poller._active_ids["T1"] = self._incident("T1")

        matched_id = poller._process_incident(
            self._incident("T1"),
            "http://talk.test",
            "user",
            "pass",
            {"incidents": "room"},
            {},
            threading.Lock(),
            lambda *args: 999,
            mock.Mock(),
        )

        self.assertIsNone(matched_id)

    def test_run_clears_stale_marker_and_processes_payload(self):
        poller = TrafficOpenDataPoller()
        poller._active_ids["old"] = {"issue_reported": "Crash", "address": "Old", "atak_marker_id": -123}
        payload = [self._incident("new", "Signal issue")]

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        response.__exit__.return_value = None

        clear_marker = mock.Mock()
        post_marker = mock.Mock()
        incident_engine = sys.modules["modules.incident_engine"]
        incident_engine._atak_clear_marker = clear_marker
        incident_engine._atak_post_marker = post_marker

        with mock.patch.object(traffic_open_data.urllib.request, "urlopen", return_value=response), \
             mock.patch.object(traffic_open_data.threading.Thread, "start", lambda self: self._target(*self._args)):
            poller.run()

        clear_marker.assert_called_once_with(-123)
        self.assertNotIn("old", poller._active_ids)
        self.assertIn("new", poller._active_ids)
        self.assertTrue(post_marker.called)


if __name__ == "__main__":
    unittest.main()
