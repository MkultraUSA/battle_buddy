"""
Focused tests for modules/pollers/impl/afd_news.py.
"""

from __future__ import annotations

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
afd_news = _load_from_file(
    "modules.pollers.impl.afd_news",
    "modules/pollers/impl/afd_news.py",
)

from modules.pollers.impl.afd_news import AFDOpenDataPoller  # noqa: E402


class AFDOpenDataPollerTests(unittest.TestCase):
    def _incident(self):
        return {
            "address": "100 Fire Rd",
            "issue_reported": "Structure fire",
            "published_date": "2026-05-04T12:30:00",
            "latitude": "30.25",
            "longitude": "-97.75",
        }

    @mock.patch.object(afd_news.urllib.request, "urlopen")
    def test_post_to_talk_skips_missing_config(self, mock_urlopen):
        AFDOpenDataPoller._post_to_talk(
            self._incident(),
            "STRUCTURE FIRE",
            None,
            "",
            "user",
            "pass",
            {"fire-ems": "room"},
        )
        AFDOpenDataPoller._post_to_talk(
            self._incident(),
            "STRUCTURE FIRE",
            None,
            "http://talk.test",
            "user",
            "pass",
            {},
        )

        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
