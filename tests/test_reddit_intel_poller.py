"""
Unit tests for modules/pollers/impl/reddit_intel.py.
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


_stub_leaf("modules.config", DB_PATH=":memory:")
_stub_leaf("modules.incident_engine", _haversine_km=lambda *a, **kw: 999)
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
reddit_intel = _load_from_file(
    "modules.pollers.impl.reddit_intel",
    "modules/pollers/impl/reddit_intel.py",
)

from modules.pollers.base import BasePoller  # noqa: E402
from modules.pollers.impl.reddit_intel import (  # noqa: E402
    REDDIT_INTERVAL,
    RedditIntelPoller,
    extract_tip_location,
    reddit_matches,
)


class RedditIntelPollerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        RedditIntelPoller.ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE incidents (id INTEGER, ts_start REAL, itype TEXT, "
            "description TEXT, location TEXT, is_test INTEGER DEFAULT 0)",
        )
        conn.execute(
            "CREATE TABLE calls (id INTEGER, ts REAL, tag TEXT, category TEXT, "
            "transcript TEXT, lat REAL, lon REAL, location TEXT)",
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def _post(self, post_id="abc123", title="Shots fired near Mueller", body="Police are blocking the road"):
        return {
            "post_id": post_id,
            "subreddit": "Austin",
            "title": title,
            "url": f"https://reddit.test/{post_id}",
            "author": "tester",
            "body": body,
        }

    def _row(self, post_id="abc123"):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT post_id, title, keywords, notified, tip_status, tip_location "
            "FROM reddit_intel WHERE post_id=?",
            (post_id,),
        ).fetchone()
        conn.close()
        return row

    def test_is_base_poller_with_expected_interval(self):
        poller = RedditIntelPoller(feeds=["https://www.reddit.com/r/Austin/new.rss"])
        self.assertIsInstance(poller, BasePoller)
        self.assertEqual(poller.interval, REDDIT_INTERVAL)
        self.assertEqual(poller.feeds, ["https://www.reddit.com/r/Austin/new.rss"])

    def test_keyword_matching(self):
        hi, matched, keywords = reddit_matches("Shots fired downtown", "")
        self.assertTrue(hi)
        self.assertTrue(matched)
        self.assertEqual(set(keywords.split(",")), {"shots fired", "shots", "fire"})
        self.assertEqual(reddit_matches("Police blocking road", ""), (False, True, "police"))
        self.assertEqual(reddit_matches("Best tacos?", ""), (False, False, ""))

    def test_non_matching_post_is_noop(self):
        poller = RedditIntelPoller()
        send_alert = mock.Mock()

        changed = poller.process_post(
            self._post(title="Best tacos?", body="Looking for dinner"),
            self.db_path,
            send_alert,
        )

        self.assertFalse(changed)
        self.assertIsNone(self._row())
        send_alert.assert_not_called()

    def test_duplicate_post_is_ignored(self):
        poller = RedditIntelPoller()
        send_alert = mock.Mock()

        with mock.patch.object(reddit_intel, "extract_tip_location", return_value=(None, None, None)), \
             mock.patch.object(reddit_intel, "reddit_match_incident", return_value=(None, 0.0)):
            self.assertTrue(poller.process_post(self._post(), self.db_path, send_alert))
            self.assertFalse(poller.process_post(self._post(), self.db_path, send_alert))

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM reddit_intel WHERE post_id='abc123'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_high_confidence_post_dispatches_alert_and_marks_notified(self):
        poller = RedditIntelPoller()
        send_alert = mock.Mock()

        with mock.patch.object(reddit_intel, "extract_tip_location", return_value=("Mueller", 30.2932, -97.6987)), \
             mock.patch.object(reddit_intel, "reddit_match_incident", return_value=(None, 0.0)), \
             mock.patch.object(reddit_intel.threading.Thread, "start", lambda self: self._target(*self._args)):
            changed = poller.process_post(self._post(), self.db_path, send_alert)

        self.assertTrue(changed)
        row = self._row()
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], "investigating")
        self.assertEqual(row[5], "Mueller")
        send_alert.assert_called_once()
        args = send_alert.call_args.args
        self.assertEqual(args[0], "CITIZEN REPORT")
        self.assertIn("Reddit Citizen Report", args[1])
        self.assertEqual(args[3], "Reddit")

    def test_parse_entry_uses_fallback_url(self):
        xml = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>t3_xyz789</id>
            <title>APD activity near Domain</title>
            <author><name>poster</name></author>
            <content type="html">&lt;p&gt;Police everywhere&lt;/p&gt;</content>
          </entry>
        </feed>"""
        root = reddit_intel.ET.fromstring(xml)
        entry = root.findall("atom:entry", {"atom": "http://www.w3.org/2005/Atom"})[0]

        post = RedditIntelPoller.parse_entry(entry, "Austin", {"atom": "http://www.w3.org/2005/Atom"})

        self.assertEqual(post["post_id"], "xyz789")
        self.assertEqual(post["url"], "https://www.reddit.com/r/Austin/comments/xyz789/")
        self.assertEqual(post["body"], "Police everywhere")

    def test_run_continues_on_fetch_error(self):
        poller = RedditIntelPoller(feeds=["https://www.reddit.com/r/Austin/new.rss"])
        sys.modules["modules.config"].DB_PATH = self.db_path

        with mock.patch.object(poller, "fetch_feed", side_effect=RuntimeError("network down")), \
             mock.patch.object(poller, "tip_recheck") as recheck:
            poller.run()

        recheck.assert_called_once()

    def test_known_neighborhood_location_is_extracted_without_geocoding(self):
        with mock.patch.object(reddit_intel, "nominatim_geocode") as geocode:
            loc, lat, lon = extract_tip_location("Huge police response", "near Hyde Park")

        self.assertEqual(loc, "Hyde Park")
        self.assertAlmostEqual(lat, 30.3091)
        self.assertAlmostEqual(lon, -97.7341)
        geocode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
