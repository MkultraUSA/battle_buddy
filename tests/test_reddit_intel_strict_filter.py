"""
Strict-filter regression set for modules/pollers/impl/reddit_intel.py.

Each case drives the REAL matcher (reddit_matches imported directly from
modules/pollers/impl/reddit_intel.py -- audio_receiver is never imported)
and asserts the capture/confidence contract:

    verdict.captured    -- True only if the post is worth storing
    verdict.confidence  -- "high" (may alert), "medium" (store, never alert),
                           or "none" (not stored)
    verdict.keywords    -- whole-word/phrase hits that drove the decision

These tests FAIL on the loose base (bare substring `kw in text`, no word
boundaries, bare medium words capture and alert) and PASS with the strict
confidence-based classifier.
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

from modules.pollers.impl.reddit_intel import (  # noqa: E402
    RedditIntelPoller,
    reddit_matches,
)


def _verdict(title: str, body: str = ""):
    return reddit_matches(title, body)


class StrictFilterRegressionTests(unittest.TestCase):
    # -- A. word boundaries: substring hits must not capture -----------------

    def test_fireworks_does_not_match_fire(self):
        v = _verdict(
            "Why fireworks in South Austin tonight?",
            "Hearing loud fireworks going off near South Austin, anyone know what's up?",
        )
        self.assertFalse(v.captured, f"fireworks must not match fire: {v!r}")
        self.assertEqual(v.confidence, "none")

    def test_copy_does_not_match_cop(self):
        v = _verdict(
            "Seeking a copy of The Chronicle for September 4th?",
            "Looking for a copy of the paper, does anyone have one?",
        )
        self.assertFalse(v.captured, f"'copy' must not match cop: {v!r}")
        self.assertEqual(v.confidence, "none")

    def test_copper_does_not_match_cop(self):
        v = _verdict(
            "Good Lord the size of this monster...",
            "Found this copper-colored spider on my porch, absolutely huge",
        )
        self.assertFalse(v.captured, f"'copper' must not match cop: {v!r}")
        self.assertEqual(v.confidence, "none")

    # -- C. topic filter: everyday subjects are not crime reports -------------

    def test_sunset_pictures_not_captured(self):
        v = _verdict(
            "Did anyone take pictures of the sunset?",
            "The police helicopter was flying over but I just wanted sunset pics",
        )
        self.assertFalse(v.captured, f"sunset photos are not a crime report: {v!r}")
        self.assertEqual(v.confidence, "none")

    def test_celebrity_sighting_not_captured(self):
        v = _verdict(
            "Natasha Lyonne at ATX airport",
            "Celebrity sighting! Spotted her near the airport police station, "
            "fire trucks around too",
        )
        self.assertFalse(v.captured, f"celebrity sighting is not a crime report: {v!r}")
        self.assertEqual(v.confidence, "none")

    def test_protest_press_conference_mentioning_police_not_captured(self):
        v = _verdict(
            "APD press conference on downtown protest policing",
            "Police discussed the rally plans and the vigil schedule with reporters",
        )
        self.assertFalse(v.captured, f"presser/protest chatter must not capture: {v!r}")
        self.assertEqual(v.confidence, "none")

    # -- B. bare medium words alone must not capture --------------------------

    def test_bare_medium_word_is_not_high_confidence(self):
        v = _verdict(
            "Helicopter overhead",
            "Just hearing a helicopter circling, anyone know why?",
        )
        self.assertNotEqual(v.confidence, "high", f"bare medium word: {v!r}")
        self.assertFalse(v.captured, f"bare medium word alone must not capture: {v!r}")

    # -- F. tuned false negatives: real reports must be captured --------------

    def test_bus_stop_stabbing_suspect_search_is_high(self):
        v = _verdict(
            "APD looking for suspect in February bus stop stabbing",
            "Police say the suspect stabbed a man at the bus stop and is still at large",
        )
        self.assertTrue(v.captured, f"real stabbing report must capture: {v!r}")
        self.assertEqual(v.confidence, "high")

    def test_body_found_is_high(self):
        v = _verdict(
            "APD: Body found on sidewalk in east Austin",
            "Officers found a body on the sidewalk, homicide detectives responding",
        )
        self.assertTrue(v.captured, f"body-found report must capture: {v!r}")
        self.assertEqual(v.confidence, "high")

    def test_highway_closure_is_captured_as_medium_traffic(self):
        v = _verdict(
            "All southbound lanes of I-35 near Grand Avenue Parkway closed",
            "Police and fire on scene, traffic blocked for miles",
        )
        self.assertTrue(v.captured, f"highway closure must be stored: {v!r}")
        # Traffic/closure intel is stored but must NOT page anyone.
        self.assertEqual(v.confidence, "medium")

    def test_shots_fired_with_cross_streets_is_high(self):
        v = _verdict(
            "Shots fired just now around 12th and Chicon",
            "Heard at least five gunshots, police responding now",
        )
        self.assertTrue(v.captured, f"shots-fired report must capture: {v!r}")
        self.assertEqual(v.confidence, "high")

    def test_stabbing_at_college_is_high(self):
        v = _verdict(
            "Stabbing at Austin Community College",
            "One person stabbed on campus, suspect in custody, avoid the area",
        )
        self.assertTrue(v.captured, f"stabbing report must capture: {v!r}")
        self.assertEqual(v.confidence, "high")

    # -- E. storage/alerting separation ---------------------------------------

    def _post(self, post_id, title, body):
        return {
            "post_id": post_id,
            "subreddit": "Austin",
            "title": title,
            "url": f"https://reddit.test/{post_id}",
            "author": "tester",
            "body": body,
        }

    def _confidence_row(self, db_path, post_id):
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT confidence, notified, keywords FROM reddit_intel WHERE post_id=?",
                (post_id,),
            ).fetchone()
        finally:
            conn.close()
        return row

    def _fresh_db(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        RedditIntelPoller.ensure_schema(tmp.name)
        return tmp.name

    def test_high_confidence_row_alerts_and_records_confidence(self):
        db_path = self._fresh_db()
        poller = RedditIntelPoller()
        send_alert = mock.Mock()
        with mock.patch.object(
            reddit_intel, "extract_tip_location", return_value=(None, None, None)
        ), mock.patch.object(
            reddit_intel, "reddit_match_incident", return_value=(None, 0.0)
        ), mock.patch.object(
            reddit_intel.threading.Thread, "start", lambda self: self._target(*self._args)
        ):
            changed = poller.process_post(
                self._post("hi1", "Shots fired just now around 12th and Chicon",
                           "Heard gunshots, police responding"),
                db_path,
                send_alert,
            )
        self.assertTrue(changed)
        conf, notified, _kw = self._confidence_row(db_path, "hi1")
        self.assertEqual(conf, "high")
        self.assertEqual(notified, 1)
        send_alert.assert_called_once()

    def test_medium_confidence_row_is_stored_without_alert(self):
        db_path = self._fresh_db()
        poller = RedditIntelPoller()
        send_alert = mock.Mock()
        with mock.patch.object(
            reddit_intel, "extract_tip_location", return_value=(None, None, None)
        ), mock.patch.object(
            reddit_intel, "reddit_match_incident", return_value=(None, 0.0)
        ):
            changed = poller.process_post(
                self._post("med1",
                           "All southbound lanes of I-35 near Grand Avenue Parkway closed",
                           "Police and fire on scene, traffic blocked"),
                db_path,
                send_alert,
            )
        self.assertTrue(changed)
        conf, notified, _kw = self._confidence_row(db_path, "med1")
        self.assertEqual(conf, "medium")
        self.assertEqual(notified, 0)
        send_alert.assert_not_called()

    def test_noise_post_is_not_stored(self):
        db_path = self._fresh_db()
        poller = RedditIntelPoller()
        send_alert = mock.Mock()
        changed = poller.process_post(
            self._post("noise1", "Why fireworks in South Austin tonight?",
                       "Hearing fireworks, anyone know what's up?"),
            db_path,
            send_alert,
        )
        self.assertFalse(changed)
        self.assertIsNone(self._confidence_row(db_path, "noise1"))
        send_alert.assert_not_called()

    def test_schema_paths_carry_confidence_column(self):
        schema = (_ROOT / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("confidence", schema)
        db_path = self._fresh_db()
        conn = sqlite3.connect(db_path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(reddit_intel)")]
        finally:
            conn.close()
        self.assertIn("confidence", cols)


if __name__ == "__main__":
    unittest.main()
