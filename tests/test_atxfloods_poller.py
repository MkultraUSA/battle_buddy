"""
Unit tests for modules/pollers/impl/atxfloods.py.
"""

from __future__ import annotations

import json
import sys
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
    _atak_clear_marker=lambda *a, **kw: None,
    _atak_post_marker=lambda *a, **kw: None,
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
atxfloods = _load_from_file(
    "modules.pollers.impl.atxfloods",
    "modules/pollers/impl/atxfloods.py",
)

from modules.pollers.base import BasePoller  # noqa: E402
from modules.pollers.impl.atxfloods import (  # noqa: E402
    ATXFLOODS_POLL_INTERVAL,
    ATXFloodsPoller,
)


class ATXFloodsPollerTests(unittest.TestCase):
    def _crossing(self, status="open", crossing_id="101"):
        return {
            "id": crossing_id,
            "status": status,
            "name": "Shoal Creek",
            "jurisdiction": "Austin",
            "address": "100 Water St",
            "lat": "30.25",
            "lon": "-97.75",
            "comment": "High water",
        }

    def test_is_base_poller_with_expected_interval(self):
        poller = ATXFloodsPoller()
        self.assertIsInstance(poller, BasePoller)
        self.assertEqual(poller.interval, ATXFLOODS_POLL_INTERVAL)
        self.assertEqual(poller._state, {})

    def test_first_sighting_seeds_state_silently(self):
        poller = ATXFloodsPoller()
        posted = poller._process_crossing(
            self._crossing("open"),
            "http://talk.test",
            "user",
            "pass",
            {"incidents": "room"},
            mock.Mock(),
            mock.Mock(),
        )

        self.assertFalse(posted)
        self.assertEqual(poller._state[101]["status"], "open")

    @mock.patch.object(atxfloods.urllib.request, "urlopen")
    def test_closed_transition_posts_and_creates_marker(self, mock_urlopen):
        poller = ATXFloodsPoller()
        poller._state[101] = {"status": "open", "marker_id": None}
        clear_marker = mock.Mock()
        post_marker = mock.Mock()

        with mock.patch.object(atxfloods.threading.Thread, "start", lambda self: self._target(*self._args)):
            transitioned = poller._process_crossing(
                self._crossing("closed"),
                "http://talk.test",
                "user",
                "pass",
                {"incidents": "room"},
                clear_marker,
                post_marker,
            )

        self.assertTrue(transitioned)
        self.assertEqual(poller._state[101]["status"], "closed")
        self.assertEqual(poller._state[101]["marker_id"], -200102)
        self.assertFalse(clear_marker.called)
        post_marker.assert_called_once_with(
            -200102,
            30.25,
            -97.75,
            "FLOODING",
            "Shoal Creek 100 Water St",
        )
        self.assertTrue(mock_urlopen.called)

    @mock.patch.object(atxfloods.urllib.request, "urlopen")
    def test_open_transition_clears_existing_marker(self, mock_urlopen):
        poller = ATXFloodsPoller()
        poller._state[101] = {"status": "closed", "marker_id": -200102}
        clear_marker = mock.Mock()
        post_marker = mock.Mock()

        with mock.patch.object(atxfloods.threading.Thread, "start", lambda self: self._target(*self._args)):
            transitioned = poller._process_crossing(
                self._crossing("open"),
                "http://talk.test",
                "user",
                "pass",
                {"incidents": "room"},
                clear_marker,
                post_marker,
            )

        self.assertTrue(transitioned)
        self.assertEqual(poller._state[101]["status"], "open")
        self.assertIsNone(poller._state[101]["marker_id"])
        clear_marker.assert_called_once_with(-200102)
        self.assertFalse(post_marker.called)
        self.assertTrue(mock_urlopen.called)

    @mock.patch.object(atxfloods.urllib.request, "urlopen")
    def test_post_to_talk_skips_missing_config(self, mock_urlopen):
        ATXFloodsPoller._post_to_talk(
            self._crossing("closed"),
            "closed",
            "open",
            "",
            "user",
            "pass",
            {"incidents": "room"},
        )
        ATXFloodsPoller._post_to_talk(
            self._crossing("closed"),
            "closed",
            "open",
            "http://talk.test",
            "user",
            "pass",
            {},
        )

        mock_urlopen.assert_not_called()

    def test_run_fetches_payload_and_processes_crossings(self):
        poller = ATXFloodsPoller()
        payload = {"attributes": [self._crossing("open")]}

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        response.__exit__.return_value = None

        with mock.patch.object(atxfloods.urllib.request, "urlopen", return_value=response):
            poller.run()

        self.assertEqual(poller._state[101]["status"], "open")


if __name__ == "__main__":
    unittest.main()
