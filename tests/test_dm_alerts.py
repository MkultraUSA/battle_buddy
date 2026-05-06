"""
Unit tests for DM alert helpers in modules/alerts.py.
"""

from __future__ import annotations

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
    "modules.config",
    DB_PATH=":memory:",
    DECK_BASE="http://deck.test",
    DECK_BOARD_ID="board",
    DECK_LABELS={"SHOOTING": "label"},
    DECK_STACK_NEW="stack",
    GOOGLE_ROUTES_KEY="routes",
    TALK_PASS="pass",
    TALK_USER="user",
)
_stub_leaf("modules.database", get_subscribers=lambda _itype, _category: [])
_stub_leaf(
    "modules.talk",
    _bot_reply=lambda _token, _message: None,
    _get_or_create_dm_room=lambda _username: None,
)

import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


alerts = _load_from_file("modules.alerts", "modules/alerts.py")

from modules.alerts import send_dm_alert  # noqa: E402


class _ImmediateThread:
    def __init__(self, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class DMAlertTests(unittest.TestCase):
    def test_send_dm_alert_returns_zero_without_subscribers(self):
        sent = send_dm_alert(
            "SHOOTING",
            "details",
            "Downtown",
            "APD",
            "APD",
            subscribers_provider=lambda _itype, _category: [],
        )

        self.assertEqual(sent, 0)

    def test_send_dm_alert_skips_subscribers_without_room_token(self):
        replies = []

        sent = send_dm_alert(
            "SHOOTING",
            "details",
            None,
            "APD",
            "APD",
            subscribers_provider=lambda _itype, _category: ["kevin"],
            room_provider=lambda _username: None,
            reply_func=lambda token, message: replies.append((token, message)),
            thread_factory=_ImmediateThread,
        )

        self.assertEqual(sent, 0)
        self.assertEqual(replies, [])

    def test_send_dm_alert_formats_and_dispatches_breaking_dm(self):
        replies = []

        sent = send_dm_alert(
            "SHOOTING",
            "Active investigation",
            "100 Congress Ave",
            "APD, EMS",
            "APD",
            subscribers_provider=lambda itype, category: ["kevin", "sam"] if (itype, category) == ("SHOOTING", "APD") else [],
            room_provider=lambda username: f"room-{username}",
            reply_func=lambda token, message: replies.append((token, message)),
            thread_factory=_ImmediateThread,
        )

        self.assertEqual(sent, 2)
        self.assertEqual([token for token, _message in replies], ["room-kevin", "room-sam"])
        self.assertIn("🔴 BREAKING — SHOOTING @ 100 Congress Ave", replies[0][1])
        self.assertIn("Agencies: APD, EMS", replies[0][1])
        self.assertIn("Active investigation", replies[0][1])


if __name__ == "__main__":
    unittest.main()
