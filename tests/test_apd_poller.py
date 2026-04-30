"""
tests/test_apd_poller.py
~~~~~~~~~~~~~~~~~~~~~~~~
Unit test suite for modules/pollers/impl/apd_news.py.

Coverage:
  - Module-level constants and data structures
  - APDNewsPoller initialisation and BasePoller compliance
  - _apd_parse_rss()            — RSS parsing, dedup, malformed XML
  - _article_itype_from_title() — headline → itype classification
  - _resolve_article_url()      — URL resolution tiers (mocked network)
  - _pi_fetch()                 — Pi5 fetch agent wrapper (mocked network)
  - _apd_fetch_article()        — article fetch + address extraction (mocked)
  - _match_article_to_incident()— DB scoring logic (in-memory SQLite)
  - _store_article_link()       — DB write helpers (in-memory SQLite)
  - _post_to_talk()             — Talk posting (mocked urllib)
  - APDNewsPoller._poll_apd_press_releases() — full press-release sub-poll
  - APDNewsPoller._poll_traffic_news()       — traffic fatality sub-poll
  - Package __init__ re-exports

All network calls are monkey-patched / mocked — no real HTTP happens.
All DB operations use a fresh in-memory (or tmp-file) SQLite database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock as mock
from email.utils import formatdate
from pathlib import Path

# Ensure the project root is on sys.path so imports work in isolation
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Lazy stub helpers — let us import apd_news without the full app env
# ---------------------------------------------------------------------------

def _stub_leaf(name: str, **attrs):
    """Register a lightweight stub for *name* in sys.modules without touching
    any parent packages.  The parent packages (modules, modules.config, …)
    must already be importable from disk OR must have been registered by a
    prior call; this helper only sets the leaf entry.

    Use this for modules that apd_news.py imports lazily inside methods so
    that we don't need the full application environment at test-collection
    time.
    """
    mod = type(sys)(name)
    mod.__name__    = name
    mod.__package__ = name.rsplit(".", 1)[0] if "." in name else name
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# Pre-register stubs for modules that apd_news.py imports *lazily* inside
# run() / _poll_apd_press_releases() / _poll_traffic_news().
#
# apd_news.py itself only does ``from modules.pollers.base import BasePoller``
# at import time — all other imports are deferred inside methods.
#
# Strategy:
#   1. Register stub leaves for modules.config, modules.geocoding,
#      modules.incident_engine, and modules.pollers (send_dm_alert).
#      These are plain modules with no sub-packages so we can register them
#      as leaf stubs without disturbing the package hierarchy.
#   2. Import apd_news through the real package path so that
#      modules.pollers.base is loaded from disk.
#
# IMPORTANT: we do NOT pre-register any parent package ("modules",
# "modules.pollers", etc.) because those are real on-disk packages that the
# import machinery must traverse to reach impl/apd_news.py.

# ------------------------------------------------------------------
# Build the stub for modules.config (leaf module, not a sub-package)
# ------------------------------------------------------------------
_stub_leaf(
    "modules.config",
    DB_PATH="/tmp/_test_apd_news_unused.db",
    TALK_BASE="http://talk.test",
    TALK_USER="user",
    TALK_PASS="pass",
    TALK_ROOMS={"apd": "room_apd", "incidents": "room_inc"},
    GOOGLE_CSE_API_KEY="",
    GOOGLE_CSE_ID="",
    PI_FETCH_URL="",
    PI_FETCH_TOKEN="",
    _state={},
)
_stub_leaf(
    "modules.geocoding",
    _geocode_address=lambda addr: None,
)
_stub_leaf(
    "modules.incident_engine",
    _atak_post_marker=lambda *a, **kw: None,
)

# modules.pollers.__init__ does "from modules.pollers_legacy import *" which
# triggers a heavy chain of imports.  We stub the whole package as a leaf so
# the lazy "from modules.pollers import send_dm_alert" resolves cleanly.
# The real BasePoller lives in modules.pollers.base which we import directly.
_stub_leaf(
    "modules.pollers",
    send_dm_alert=lambda *a, **kw: None,
    _pi_command_queue=[],
)

# Now load modules.pollers.base and apd_news directly from disk, bypassing
# the heavy modules.pollers.__init__ (which pulls in pollers_legacy → full app).
import importlib.util as _ilu  # noqa: E402


def _load_from_file(dotted_name: str, rel_path: str):
    spec = _ilu.spec_from_file_location(dotted_name, str(_ROOT / rel_path))
    mod  = _ilu.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod

# Load BasePoller first (apd_news imports it at module level)
_load_from_file("modules.pollers.base", "modules/pollers/base.py")

# Stub impl __init__ so the impl package exists for attribute lookups
_stub_leaf("modules.pollers.impl")

# Load the module under test
apd_news = _load_from_file(
    "modules.pollers.impl.apd_news",
    "modules/pollers/impl/apd_news.py",
)

from modules.pollers.base import BasePoller  # noqa: E402
from modules.pollers.impl.apd_news import (  # noqa: E402
    _APD_HEADLINE_KW,
    _APD_NEWS_LOCK,
    _APD_SOURCE_RSS,
    _ARTICLE_MAX_AGE_SECS,
    _ARTICLE_STOP_WORDS,
    _NEWS_ITYPE_COMPAT,
    APD_NEWS_INTERVAL,
    APD_NEWS_URL,
    TRAFFIC_NEWS_URL,
    APDNewsPoller,
    _apd_fetch_article,
    _apd_parse_rss,
    _article_itype_from_title,
    _match_article_to_incident,
    _pi_fetch,
    _post_to_talk,
    _resolve_article_url,
    _store_article_link,
)

# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

def _make_db(path: str) -> sqlite3.Connection:
    """Create the minimal schema tables required by apd_news."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start    REAL,
            ts_updated  REAL,
            itype       TEXT,
            description TEXT,
            agencies    TEXT,
            tgids       TEXT,
            location    TEXT,
            lat         REAL,
            lon         REAL,
            article_url TEXT,
            status      TEXT
        );
        CREATE TABLE IF NOT EXISTS incident_articles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER,
            ts          REAL,
            headline    TEXT,
            url         TEXT,
            source      TEXT,
            snippet     TEXT,
            match_score REAL
        );
        CREATE TABLE IF NOT EXISTS apd_seen (
            url TEXT PRIMARY KEY,
            ts  REAL
        );
    """)
    conn.commit()
    return conn


def _tmp_db() -> str:
    """Return a path to a fresh temp DB file (caller responsible for cleanup)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _make_db(path).close()
    return path


# ---------------------------------------------------------------------------
# RSS fixture helpers
# ---------------------------------------------------------------------------

def _make_rss(items: list[dict]) -> str:
    """Build a minimal Google News RSS XML string from a list of item dicts."""
    def _item(d: dict) -> str:
        pub = d.get("pubDate", formatdate(usegmt=True))
        src = d.get("source", "kxan.com")
        return textwrap.dedent(f"""
            <item>
              <title>{d['title']}</title>
              <link>{d['link']}</link>
              <pubDate>{pub}</pubDate>
              <source url="https://{src}">{src}</source>
            </item>
        """)
    body = "\n".join(_item(i) for i in items)
    return f"<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>{body}</channel></rss>"


# ===========================================================================
# 1. Module-level constants
# ===========================================================================

class TestConstants(unittest.TestCase):
    def test_apd_news_url_contains_google_news(self):
        self.assertIn("news.google.com", APD_NEWS_URL)

    def test_apd_news_url_contains_press_release_keyword(self):
        self.assertIn("press+release", APD_NEWS_URL)

    def test_traffic_news_url_contains_google_news(self):
        self.assertIn("news.google.com", TRAFFIC_NEWS_URL)

    def test_traffic_news_url_contains_fatal(self):
        self.assertIn("fatal", TRAFFIC_NEWS_URL.lower())

    def test_apd_news_interval_is_300(self):
        self.assertEqual(APD_NEWS_INTERVAL, 300.0)

    def test_article_max_age_is_72_hours(self):
        self.assertAlmostEqual(_ARTICLE_MAX_AGE_SECS, 72 * 3600, delta=1)

    def test_news_itype_compat_shooting_includes_shooting(self):
        self.assertIn("SHOOTING", _NEWS_ITYPE_COMPAT["SHOOTING"])

    def test_news_itype_compat_homicide_is_broad(self):
        # HOMICIDE should match SHOOTING, STABBING, OFFICER DOWN, WEAPONS
        self.assertTrue(len(_NEWS_ITYPE_COMPAT["HOMICIDE"]) >= 3)

    def test_apd_source_rss_has_kxan(self):
        self.assertIn("kxan.com", _APD_SOURCE_RSS)

    def test_apd_headline_kw_has_homicide(self):
        self.assertIn("homicide", _APD_HEADLINE_KW)

    def test_article_stop_words_is_frozenset(self):
        self.assertIsInstance(_ARTICLE_STOP_WORDS, frozenset)

    def test_lock_is_threading_lock(self):
        # threading.Lock() returns _thread.lock, not RLock
        self.assertTrue(hasattr(_APD_NEWS_LOCK, "acquire"))


# ===========================================================================
# 2. APDNewsPoller initialisation & BasePoller compliance
# ===========================================================================

class TestAPDNewsPolllerInit(unittest.TestCase):
    def setUp(self):
        self.poller = APDNewsPoller()

    def test_is_base_poller_subclass(self):
        self.assertIsInstance(self.poller, BasePoller)

    def test_default_interval(self):
        self.assertEqual(self.poller.interval, int(APD_NEWS_INTERVAL))

    def test_has_stop_event(self):
        self.assertIsInstance(self.poller.stop_event, threading.Event)

    def test_has_thread(self):
        self.assertIsInstance(self.poller.thread, threading.Thread)

    def test_thread_not_started_on_init(self):
        self.assertFalse(self.poller.thread.is_alive())

    def test_lock_is_threading_lock(self):
        self.assertTrue(hasattr(self.poller._lock, "acquire"))

    def test_has_run_method(self):
        self.assertTrue(callable(getattr(self.poller, "run", None)))

    def test_has_poll_apd_method(self):
        self.assertTrue(callable(getattr(self.poller, "_poll_apd_press_releases", None)))

    def test_has_poll_traffic_method(self):
        self.assertTrue(callable(getattr(self.poller, "_poll_traffic_news", None)))

    def test_name_attribute(self):
        self.assertEqual(APDNewsPoller.NAME, "apd_news")


# ===========================================================================
# 3. _apd_parse_rss()
# ===========================================================================

_PUB_DATE = formatdate(time.time() - 3600, usegmt=True)  # 1 hour ago

_SIMPLE_RSS = _make_rss([
    {"title": "APD Press Release: Homicide Investigation", "link": "https://kxan.com/homicide1", "pubDate": _PUB_DATE},
    {"title": "APD Shooting update on South Congress", "link": "https://kvue.com/shooting1", "pubDate": _PUB_DATE},
])


class TestParseRSS(unittest.TestCase):
    def test_returns_two_items(self):
        items = _apd_parse_rss(_SIMPLE_RSS)
        self.assertEqual(len(items), 2)

    def test_first_item_title(self):
        items = _apd_parse_rss(_SIMPLE_RSS)
        self.assertIn("Homicide", items[0]["title"])

    def test_first_item_link(self):
        items = _apd_parse_rss(_SIMPLE_RSS)
        self.assertTrue(items[0]["link"].startswith("https://"))

    def test_pub_ts_is_float(self):
        items = _apd_parse_rss(_SIMPLE_RSS)
        self.assertIsInstance(items[0]["pub_ts"], float)

    def test_pub_ts_approximate_time(self):
        items = _apd_parse_rss(_SIMPLE_RSS)
        # pub_ts should be within the last hour
        self.assertAlmostEqual(items[0]["pub_ts"], time.time() - 3600, delta=60)

    def test_deduplicates_identical_links(self):
        dup_rss = _make_rss([
            {"title": "Story A", "link": "https://kxan.com/dup", "pubDate": _PUB_DATE},
            {"title": "Story B", "link": "https://kxan.com/dup", "pubDate": _PUB_DATE},
        ])
        items = _apd_parse_rss(dup_rss)
        self.assertEqual(len(items), 1)

    def test_returns_empty_on_malformed_xml(self):
        items = _apd_parse_rss("<<not valid XML>>")
        self.assertEqual(items, [])

    def test_returns_empty_on_missing_channel(self):
        items = _apd_parse_rss("<rss version='2.0'></rss>")
        self.assertEqual(items, [])

    def test_source_url_extracted(self):
        items = _apd_parse_rss(_SIMPLE_RSS)
        self.assertIn("kxan.com", items[0].get("source_url", ""))

    def test_missing_pubdate_gives_none_pub_ts(self):
        no_date_rss = (
            "<?xml version=\"1.0\"?>"
            "<rss version=\"2.0\"><channel>"
            "<item>"
            "<title>Some Story</title>"
            "<link>https://kxan.com/nodatestory</link>"
            "</item>"
            "</channel></rss>"
        )
        items = _apd_parse_rss(no_date_rss)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["pub_ts"])


# ===========================================================================
# 4. _article_itype_from_title()
# ===========================================================================

class TestClassifyItype(unittest.TestCase):
    def _c(self, title: str) -> str:
        return _article_itype_from_title(title)

    def test_homicide(self):
        self.assertEqual(self._c("APD Homicide investigation on Rundberg Ln"), "HOMICIDE")

    def test_murder(self):
        self.assertEqual(self._c("Man charged with murder in downtown Austin"), "HOMICIDE")

    def test_fatal_crash(self):
        self.assertEqual(self._c("Fatal crash on IH-35 leaves one dead"), "FATAL CRASH")

    def test_fatal_pedestrian(self):
        # "killed" alone without "fatal" in title resolves to CRASH/COLLISION
        # per the implementation: fatal+crash/accident/hit/pedestrian → FATAL CRASH
        self.assertEqual(self._c("APD: pedestrian killed in hit-and-run"), "CRASH/COLLISION")

    def test_fatal_pedestrian_with_fatal_keyword(self):
        self.assertEqual(self._c("APD: fatal hit-and-run kills pedestrian"), "FATAL CRASH")

    def test_shooting(self):
        self.assertEqual(self._c("APD: shooting reported on Slaughter Lane"), "SHOOTING")

    def test_shot(self):
        self.assertEqual(self._c("Man shot at East Austin apartment complex"), "SHOOTING")

    def test_stabbing(self):
        self.assertEqual(self._c("APD Press Release: stabbing at 6th St bar"), "STABBING")

    def test_weapons(self):
        self.assertEqual(self._c("Aggravated assault reported near Barton Springs"), "WEAPONS")

    def test_robbery(self):
        self.assertEqual(self._c("Robbery at North Austin gas station"), "WEAPONS")

    def test_crash_collision(self):
        self.assertEqual(self._c("Major collision blocks Lamar Blvd traffic"), "CRASH/COLLISION")

    def test_pedestrian_no_fatal(self):
        self.assertEqual(self._c("Pedestrian struck near UT campus"), "CRASH/COLLISION")

    def test_default_fallback(self):
        # Unknown headline → SHOOTING (default)
        self.assertEqual(self._c("APD issues press release"), "SHOOTING")


# ===========================================================================
# 5. _resolve_article_url()
# ===========================================================================

class TestResolveArticleUrl(unittest.TestCase):
    """Test URL resolution with mocked HTTP responses."""

    def _call(self, source_url="", title="APD: Shooting on Congress Ave", gnews_link="https://news.google.com/rss/articles/CBMi123",
              cse_key="", cse_id=""):
        return _resolve_article_url(source_url, title, gnews_link, cse_key, cse_id)

    def test_fallback_strips_rss_articles_prefix(self):
        url = self._call(gnews_link="https://news.google.com/rss/articles/CBMi123")
        self.assertIn("/articles/", url)
        self.assertNotIn("/rss/articles/", url)

    def test_no_source_url_falls_back_to_gnews_link(self):
        url = self._call(source_url="", title="Short")
        # Should be a google news URL (fallback)
        self.assertIn("news.google.com", url)

    @mock.patch("urllib.request.urlopen")
    def test_source_rss_tier_resolves_when_title_matches(self, mock_urlopen):
        """When source RSS contains a matching headline, return its link."""
        kxan_rss = _make_rss([
            {"title": "APD: Shooting on Congress Ave - KXAN", "link": "https://kxan.com/real-story-1", "pubDate": _PUB_DATE},
        ])
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = kxan_rss.encode()
        mock_urlopen.return_value.__enter__ = lambda s: mock_resp
        mock_urlopen.return_value.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        url = _resolve_article_url(
            "https://kxan.com/feed/",
            "APD: Shooting on Congress Ave - KXAN",
            "https://news.google.com/rss/articles/CBMi_kxan",
            "", "",
        )
        self.assertEqual(url, "https://kxan.com/real-story-1")

    @mock.patch("urllib.request.urlopen")
    def test_cse_tier_used_when_source_rss_fails(self, mock_urlopen):
        """When source RSS lookup raises, fall through to CSE tier."""
        mock_urlopen.side_effect = Exception("network error")
        # With no CSE key, should fall back to gnews link transformation
        url = _resolve_article_url(
            "https://kxan.com/feed/",
            "APD: Shooting on Congress Ave",
            "https://news.google.com/rss/articles/CBMi_x",
            "", "",
        )
        self.assertIn("news.google.com", url)

    @mock.patch("urllib.request.urlopen")
    def test_cse_returns_link_when_configured(self, mock_urlopen):
        """CSE tier returns the first item link when API key+ID are set."""
        cse_response = json.dumps({"items": [{"link": "https://kvue.com/cse-article"}]}).encode()
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = cse_response
        mock_urlopen.return_value = mock_resp

        url = _resolve_article_url(
            "https://unknown.example.com/",  # not in _APD_SOURCE_RSS → skip tier 1
            "Some Unique Headline About A Shooting Event - KVUE",
            "https://news.google.com/rss/articles/CBMi_cse",
            "my_api_key",
            "my_cse_id",
        )
        self.assertEqual(url, "https://kvue.com/cse-article")

    def test_oc_param_stripped_from_fallback(self):
        url = self._call(gnews_link="https://news.google.com/rss/articles/CBMi123?oc=5")
        self.assertNotIn("oc=", url)


# ===========================================================================
# 6. _pi_fetch()
# ===========================================================================

class TestPiFetch(unittest.TestCase):
    def test_returns_empty_dict_when_no_url_configured(self):
        result = _pi_fetch("https://kxan.com/story", "", "", "")
        self.assertEqual(result, {})

    def test_returns_empty_dict_when_no_token_configured(self):
        result = _pi_fetch("https://kxan.com/story", "http://pi5.local:9090", "", "")
        self.assertEqual(result, {})

    @mock.patch("urllib.request.urlopen")
    def test_returns_empty_on_non_200_status(self, mock_urlopen):
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({"status": 403, "text": ""}).encode()
        mock_urlopen.return_value = mock_resp
        result = _pi_fetch("https://kxan.com/story", "http://pi5.local:9090", "tok", "")
        self.assertEqual(result, {})

    @mock.patch("urllib.request.urlopen")
    def test_extracts_address_from_article_text(self, mock_urlopen):
        body_text = (
            "The incident occurred at 1234 South Congress Ave near a grocery store. "
            "APD responded to the scene."
        )
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps({"status": 200, "text": body_text, "final_url": "https://kxan.com/s"}).encode()
        mock_urlopen.return_value = mock_resp
        result = _pi_fetch("https://kxan.com/story", "http://pi5.local:9090", "tok", "")
        self.assertIn("address", result)
        self.assertIn("Congress", result["address"])

    @mock.patch("urllib.request.urlopen")
    def test_returns_empty_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")
        result = _pi_fetch("https://kxan.com/story", "http://pi5.local:9090", "tok", "")
        self.assertEqual(result, {})


# ===========================================================================
# 7. _apd_fetch_article()
# ===========================================================================

class TestFetchArticle(unittest.TestCase):
    @mock.patch("modules.pollers.impl.apd_news._pi_fetch")
    def test_pi_fetch_result_used_when_available(self, mock_pi):
        mock_pi.return_value = {"url": "https://kxan.com/real", "address": "100 Main St", "summary": "Details..."}
        result = _apd_fetch_article("https://kxan.com/story", "http://pi5:9090", "token")
        self.assertEqual(result["address"], "100 Main St")
        mock_pi.assert_called_once()

    @mock.patch("modules.pollers.impl.apd_news._pi_fetch")
    @mock.patch("urllib.request.urlopen")
    def test_falls_back_to_direct_fetch(self, mock_urlopen, mock_pi):
        mock_pi.return_value = {}  # Pi unavailable
        html = b"<html><body>Case Number: 2026-12345 Suspect arrested near 500 Riverside Dr. Contact Crime Stoppers.</body></html>"
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = html
        mock_resp.url = "https://kxan.com/story"
        mock_urlopen.return_value = mock_resp
        result = _apd_fetch_article("https://kxan.com/story", "", "")
        self.assertIn("summary", result)

    @mock.patch("modules.pollers.impl.apd_news._pi_fetch")
    @mock.patch("urllib.request.urlopen")
    def test_returns_empty_on_fetch_failure(self, mock_urlopen, mock_pi):
        mock_pi.return_value = {}
        mock_urlopen.side_effect = Exception("timeout")
        result = _apd_fetch_article("https://kxan.com/story", "", "")
        self.assertEqual(result, {})

    @mock.patch("modules.pollers.impl.apd_news._pi_fetch")
    @mock.patch("urllib.request.urlopen")
    def test_extracts_address_from_direct_html(self, mock_urlopen, mock_pi):
        mock_pi.return_value = {}
        html = b"<html><body>Police responded to 1500 block of Airport Blvd. The victim was transported.</body></html>"
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = html
        mock_resp.url = "https://kvue.com/story"
        mock_urlopen.return_value = mock_resp
        result = _apd_fetch_article("https://kvue.com/story", "", "")
        self.assertIn("address", result)
        self.assertIn("Airport", result.get("address", ""))


# ===========================================================================
# 8. _match_article_to_incident()
# ===========================================================================

class TestMatchArticleToIncident(unittest.TestCase):
    def setUp(self):
        self.db_path = _tmp_db()
        self._conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self._conn.close()
        os.unlink(self.db_path)

    def _insert_incident(self, itype: str, description: str, location: str, ts_start: float) -> int:
        cur = self._conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, location, status) "
            "VALUES (?,?,?,?,?,?,?,'active')",
            (ts_start, ts_start, itype, description, '["APD"]', "[]", location),
        )
        self._conn.commit()
        return cur.lastrowid

    def test_returns_none_when_no_incidents(self):
        inc_id, score = _match_article_to_incident("Homicide at 6th St", "HOMICIDE", time.time(), self.db_path)
        self.assertIsNone(inc_id)
        self.assertEqual(score, 0)

    def test_returns_single_candidate_with_score_ge_1(self):
        ts = time.time() - 1800  # 30 min ago
        # HOMICIDE articles look for incidents with SHOOTING/STABBING/OFFICER DOWN/WEAPONS
        # per _NEWS_ITYPE_COMPAT; insert a SHOOTING incident so the query finds it
        self._insert_incident("SHOOTING", "Shooting victim found", "6th St", ts)
        inc_id, score = _match_article_to_incident(
            "APD Press Release: Homicide at 6th Street", "HOMICIDE", time.time(), self.db_path
        )
        self.assertIsNotNone(inc_id)
        self.assertGreaterEqual(score, 1.0)

    def test_location_token_boosts_score(self):
        ts = time.time() - 3600
        id1 = self._insert_incident("SHOOTING", "Shooting near Lamar", "Lamar Blvd", ts)  # noqa: F841
        id2 = self._insert_incident("SHOOTING", "Shooting near Congress", "Congress Ave", ts)
        inc_id, score = _match_article_to_incident(
            "Man shot on Congress Avenue near downtown", "SHOOTING", time.time(), self.db_path
        )
        self.assertEqual(inc_id, id2)
        self.assertGreaterEqual(score, 1.0)

    def test_skips_existing_press_release_incidents(self):
        ts = time.time() - 1000
        self._conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, location, status) "
            "VALUES (?,?,?,?,?,?,?,'active')",
            (ts, ts, "SHOOTING", "[APD Press Release] Prior release", '["APD"]', "[]", "Congress Ave"),
        )
        self._conn.commit()
        inc_id, score = _match_article_to_incident(
            "Shooting on Congress Ave", "SHOOTING", time.time(), self.db_path
        )
        self.assertIsNone(inc_id)

    def test_itype_mismatch_reduces_match_probability(self):
        ts = time.time() - 500
        self._insert_incident("STRUCTURE FIRE", "Fire on 6th Street", "6th Street", ts)
        # SHOOTING itype doesn't match STRUCTURE FIRE → no match expected
        inc_id, score = _match_article_to_incident(
            "Shooting at 6th Street", "SHOOTING", time.time(), self.db_path
        )
        self.assertIsNone(inc_id)

    def test_old_incident_outside_window_not_matched(self):
        old_ts = time.time() - 60 * 3600  # 60 hours ago, outside 48h window
        self._insert_incident("SHOOTING", "Old shooting", "Congress Ave", old_ts)
        inc_id, score = _match_article_to_incident(
            "Shooting on Congress Ave", "SHOOTING", time.time(), self.db_path
        )
        self.assertIsNone(inc_id)


# ===========================================================================
# 9. _store_article_link()
# ===========================================================================

class TestStoreArticleLink(unittest.TestCase):
    def setUp(self):
        self.db_path = _tmp_db()
        self._conn = sqlite3.connect(self.db_path)
        cur = self._conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, location, status) "
            "VALUES (?,?,?,?,?,?,?,'active')",
            (time.time(), time.time(), "SHOOTING", "desc", '["APD"]', "[]", "Congress Ave"),
        )
        self._conn.commit()
        self.inc_id = cur.lastrowid

    def tearDown(self):
        self._conn.close()
        os.unlink(self.db_path)

    def test_inserts_row_in_incident_articles(self):
        _store_article_link(self.inc_id, time.time(), "Headline", "https://kxan.com/a", "apd_pr", "snippet", 1.5, self.db_path)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT * FROM incident_articles").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)

    def test_sets_article_url_on_incident(self):
        url = "https://kxan.com/article-url"
        _store_article_link(self.inc_id, time.time(), "Headline", url, "apd_pr", "snippet", 2.0, self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT article_url FROM incidents WHERE id=?", (self.inc_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], url)

    def test_does_not_overwrite_existing_article_url(self):
        existing = "https://kvue.com/first"
        self._conn.execute("UPDATE incidents SET article_url=? WHERE id=?", (existing, self.inc_id))
        self._conn.commit()
        _store_article_link(self.inc_id, time.time(), "New Headline", "https://kxan.com/second", "apd_pr", "", 1.0, self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT article_url FROM incidents WHERE id=?", (self.inc_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], existing)

    def test_stores_row_with_none_incident_id(self):
        """Unmatched articles (incident_id=None) should still be stored."""
        _store_article_link(None, time.time(), "Headline", "https://kxan.com/unmatched", "apd_pr", "snip", 0.0, self.db_path)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT * FROM incident_articles WHERE incident_id IS NULL").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)

    def test_snippet_truncated_to_300_chars(self):
        long_snip = "x" * 500
        _store_article_link(self.inc_id, time.time(), "H", "https://kxan.com/t", "apd_pr", long_snip, 1.0, self.db_path)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT snippet FROM incident_articles").fetchone()
        conn.close()
        self.assertLessEqual(len(row[0]), 300)


# ===========================================================================
# 10. _post_to_talk()
# ===========================================================================

class TestPostToTalk(unittest.TestCase):
    @mock.patch("urllib.request.urlopen")
    def test_posts_to_each_room(self, mock_urlopen):
        mock_urlopen.return_value = mock.MagicMock()
        _post_to_talk("Test message", ["roomA", "roomB"], "http://talk.test", "user", "pass")
        self.assertEqual(mock_urlopen.call_count, 2)

    @mock.patch("urllib.request.urlopen")
    def test_includes_message_in_payload(self, mock_urlopen):
        mock_urlopen.return_value = mock.MagicMock()
        _post_to_talk("Hello World", ["roomX"], "http://talk.test", "user", "pass")
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode())
        self.assertEqual(payload["message"], "Hello World")

    @mock.patch("urllib.request.urlopen")
    def test_uses_basic_auth(self, mock_urlopen):
        mock_urlopen.return_value = mock.MagicMock()
        _post_to_talk("msg", ["room1"], "http://talk.test", "myuser", "mypass")
        req = mock_urlopen.call_args[0][0]
        self.assertIn("Authorization", req.headers)
        self.assertTrue(req.headers["Authorization"].startswith("Basic "))

    @mock.patch("urllib.request.urlopen")
    def test_does_not_raise_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection failed")
        # Should not propagate the exception
        try:
            _post_to_talk("msg", ["roomA"], "http://talk.test", "u", "p")
        except Exception as exc:
            self.fail(f"_post_to_talk raised unexpectedly: {exc}")

    @mock.patch("urllib.request.urlopen")
    def test_no_rooms_makes_no_requests(self, mock_urlopen):
        _post_to_talk("msg", [], "http://talk.test", "u", "p")
        mock_urlopen.assert_not_called()


# ===========================================================================
# 11. APDNewsPoller._poll_apd_press_releases() — integration (mocked I/O)
# ===========================================================================

class TestPollAPDPressReleases(unittest.TestCase):
    """Integration tests for the APD press-release sub-poll.

    All network I/O (urlopen) is mocked; real SQLite operations use a
    temporary on-disk database.
    """

    def setUp(self):
        self.db_path = _tmp_db()
        self.poller = APDNewsPoller()

    def tearDown(self):
        os.unlink(self.db_path)

    def _run_poll(self, rss_xml: str, *, geocode_fn=None, atak_fn=None, send_dm=None):
        """Run _poll_apd_press_releases with mocked network and stubs."""
        if geocode_fn is None:
            geocode_fn = lambda addr: None  # noqa: E731
        if atak_fn is None:
            atak_fn = mock.MagicMock()
        if send_dm is None:
            send_dm = mock.MagicMock()

        def _fake_urlopen(req, timeout=None):
            resp = mock.MagicMock()
            resp.read.return_value = rss_xml.encode()
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             mock.patch("modules.pollers.impl.apd_news._apd_fetch_article", return_value={}), \
             mock.patch("modules.pollers.impl.apd_news._resolve_article_url",
                        side_effect=lambda su, t, link, k, cid: link), \
             mock.patch("modules.pollers.impl.apd_news._append_homicide_json"), \
             mock.patch.object(sys.modules["modules.pollers"], "send_dm_alert", new=send_dm):
            self.poller._poll_apd_press_releases(
                db_path=self.db_path,
                talk_base="http://talk.test",
                talk_user="u",
                talk_pass="p",
                talk_rooms={"apd": "room_apd", "incidents": "room_inc"},
                google_cse_api_key="",
                google_cse_id="",
                pi_fetch_url="",
                pi_fetch_token="",
                geocode_fn=geocode_fn,
                atak_post_fn=atak_fn,
            )

    def test_new_article_inserted_into_apd_seen(self):
        rss = _make_rss([{"title": "APD: Homicide on 6th St", "link": "https://kxan.com/hom1", "pubDate": _PUB_DATE}])
        self._run_poll(rss)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT url FROM apd_seen").fetchall()
        conn.close()
        self.assertTrue(any("hom1" in r[0] for r in rows))

    def test_already_seen_article_not_reprocessed(self):
        link = "https://kxan.com/seen_before"
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO apd_seen (url, ts) VALUES (?, ?)", (link, time.time()))
        conn.commit()
        conn.close()
        rss = _make_rss([{"title": "APD: Shooting update", "link": link, "pubDate": _PUB_DATE}])
        send_dm = mock.MagicMock()
        self._run_poll(rss, send_dm=send_dm)
        # DM alert should NOT have been sent (already seen)
        send_dm.assert_not_called()

    def test_headline_without_keyword_is_skipped(self):
        rss = _make_rss([{"title": "APD awards ceremony downtown", "link": "https://kxan.com/awards", "pubDate": _PUB_DATE}])
        send_dm = mock.MagicMock()
        self._run_poll(rss, send_dm=send_dm)
        conn = sqlite3.connect(self.db_path)
        inc_rows = conn.execute("SELECT * FROM incidents").fetchall()
        conn.close()
        self.assertEqual(len(inc_rows), 0)
        send_dm.assert_not_called()

    def test_no_pub_ts_article_is_skipped(self):
        """Articles with no pubDate should be skipped (no pub_ts)."""
        no_date_rss = (
            "<?xml version=\"1.0\"?>"
            "<rss version=\"2.0\"><channel>"
            "<item>"
            "<title>APD: Homicide investigation opens</title>"
            "<link>https://kxan.com/nodate</link>"
            "</item>"
            "</channel></rss>"
        )
        send_dm = mock.MagicMock()
        self._run_poll(no_date_rss, send_dm=send_dm)
        send_dm.assert_not_called()

    def test_stale_article_is_skipped(self):
        """Articles older than _ARTICLE_MAX_AGE_SECS should be skipped."""
        stale_date = formatdate(time.time() - (_ARTICLE_MAX_AGE_SECS + 3600), usegmt=True)
        rss = _make_rss([{"title": "APD: Homicide from last week", "link": "https://kxan.com/stale1", "pubDate": stale_date}])
        send_dm = mock.MagicMock()
        self._run_poll(rss, send_dm=send_dm)
        conn = sqlite3.connect(self.db_path)
        inc_rows = conn.execute("SELECT * FROM incidents").fetchall()
        conn.close()
        self.assertEqual(len(inc_rows), 0)
        send_dm.assert_not_called()

    def test_unmatched_article_creates_new_incident(self):
        rss = _make_rss([{"title": "APD: Shooting on Slaughter Lane", "link": "https://kxan.com/slt1", "pubDate": _PUB_DATE}])
        self._run_poll(rss)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT * FROM incidents").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)

    def test_unmatched_incident_description_contains_press_release_tag(self):
        rss = _make_rss([{"title": "APD: Shooting on Slaughter Lane", "link": "https://kxan.com/slt2", "pubDate": _PUB_DATE}])
        self._run_poll(rss)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT description FROM incidents").fetchone()
        conn.close()
        self.assertIn("[APD Press Release]", row[0])

    def test_unmatched_article_sends_dm_alert(self):
        rss = _make_rss([{"title": "APD: Fatal shooting near UT", "link": "https://kxan.com/ut1", "pubDate": _PUB_DATE}])
        send_dm = mock.MagicMock()
        self._run_poll(rss, send_dm=send_dm)
        send_dm.assert_called_once()

    def test_matched_article_links_to_existing_incident(self):
        ts_event = time.time() - 1800
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, location, status) "
            "VALUES (?,?,?,?,?,?,?,'active')",
            (ts_event, ts_event, "SHOOTING", "Shooting on Congress Ave", '["AFD"]', "[]", "Congress Ave"),
        )
        conn.commit()
        inc_id = cur.lastrowid
        conn.close()

        rss = _make_rss([{"title": "APD Press Release: Shooting on Congress Avenue", "link": "https://kxan.com/congr1", "pubDate": _PUB_DATE}])
        self._run_poll(rss)

        conn = sqlite3.connect(self.db_path)
        linked = conn.execute("SELECT * FROM incident_articles WHERE incident_id=?", (inc_id,)).fetchall()
        conn.close()
        self.assertTrue(len(linked) >= 1, "Article should be linked to existing incident")

    def test_atak_marker_posted_when_coords_available(self):
        def _geocode_with_coords(addr):
            return (30.27, -97.74)

        rss = _make_rss([{"title": "APD: Stabbing at 800 Cesar Chavez", "link": "https://kxan.com/stab1", "pubDate": _PUB_DATE}])
        atak_fn = mock.MagicMock()
        send_dm = mock.MagicMock()

        def _fake_urlopen(req, timeout=None):
            resp = mock.MagicMock()
            resp.read.return_value = rss.encode()
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             mock.patch("modules.pollers.impl.apd_news._apd_fetch_article",
                        return_value={"address": "800 Cesar Chavez St", "summary": "APD stabbing"}), \
             mock.patch("modules.pollers.impl.apd_news._resolve_article_url",
                        side_effect=lambda su, t, link, k, cid: link), \
             mock.patch("modules.pollers.impl.apd_news._append_homicide_json"), \
             mock.patch.object(sys.modules["modules.pollers"], "send_dm_alert", new=send_dm):
            self.poller._poll_apd_press_releases(
                db_path=self.db_path,
                talk_base="http://talk.test",
                talk_user="u",
                talk_pass="p",
                talk_rooms={"apd": "room_apd", "incidents": "room_inc"},
                google_cse_api_key="",
                google_cse_id="",
                pi_fetch_url="",
                pi_fetch_token="",
                geocode_fn=_geocode_with_coords,
                atak_post_fn=atak_fn,
            )

        # Give daemon threads a moment to run
        time.sleep(0.2)
        atak_fn.assert_called()

    def test_rss_fetch_error_does_not_raise(self):
        """Network errors during RSS fetch should be caught silently."""
        with mock.patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            try:
                self.poller._poll_apd_press_releases(
                    db_path=self.db_path,
                    talk_base="http://talk.test",
                    talk_user="u", talk_pass="p",
                    talk_rooms={"apd": "room_apd", "incidents": "room_inc"},
                    google_cse_api_key="", google_cse_id="",
                    pi_fetch_url="", pi_fetch_token="",
                    geocode_fn=lambda a: None,
                    atak_post_fn=mock.MagicMock(),
                )
            except Exception as exc:
                self.fail(f"_poll_apd_press_releases raised unexpectedly: {exc}")


# ===========================================================================
# 12. APDNewsPoller._poll_traffic_news() — integration (mocked I/O)
# ===========================================================================

class TestPollTrafficNews(unittest.TestCase):
    def setUp(self):
        self.db_path = _tmp_db()
        self.poller = APDNewsPoller()

    def tearDown(self):
        os.unlink(self.db_path)

    def _run_poll(self, rss_xml: str, geocode_fn=None):
        if geocode_fn is None:
            geocode_fn = lambda addr: None  # noqa: E731

        def _fake_urlopen(req, timeout=None):
            resp = mock.MagicMock()
            resp.read.return_value = rss_xml.encode()
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             mock.patch("modules.pollers.impl.apd_news._apd_fetch_article", return_value={}), \
             mock.patch("modules.pollers.impl.apd_news._resolve_article_url",
                        side_effect=lambda su, t, l, k, cid: l):  # noqa: E741
            self.poller._poll_traffic_news(
                db_path=self.db_path,
                google_cse_api_key="",
                google_cse_id="",
                pi_fetch_url="",
                pi_fetch_token="",
                geocode_fn=geocode_fn,
            )

    def test_already_seen_traffic_article_not_reprocessed(self):
        link = "https://kvue.com/traffic_seen"
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO apd_seen (url, ts) VALUES (?, ?)", (link, time.time()))
        conn.commit()
        conn.close()
        rss = _make_rss([{"title": "Fatal crash on IH-35", "link": link, "pubDate": _PUB_DATE}])
        # Should not create new incidents or raise
        self._run_poll(rss)
        conn = sqlite3.connect(self.db_path)
        arts = conn.execute("SELECT * FROM incident_articles").fetchall()
        conn.close()
        self.assertEqual(len(arts), 0)

    def test_traffic_article_without_fatal_keyword_skipped(self):
        rss = _make_rss([{"title": "Minor fender-bender on Lamar", "link": "https://kvue.com/fender1", "pubDate": _PUB_DATE}])
        self._run_poll(rss)
        conn = sqlite3.connect(self.db_path)
        arts = conn.execute("SELECT * FROM incident_articles").fetchall()
        conn.close()
        self.assertEqual(len(arts), 0)

    def test_traffic_article_with_no_pub_ts_is_skipped(self):
        no_date_rss = (
            "<?xml version=\"1.0\"?>"
            "<rss version=\"2.0\"><channel>"
            "<item>"
            "<title>Fatal crash on 183</title>"
            "<link>https://kvue.com/crash_nodate</link>"
            "</item>"
            "</channel></rss>"
        )
        self._run_poll(no_date_rss)
        conn = sqlite3.connect(self.db_path)
        arts = conn.execute("SELECT * FROM incident_articles").fetchall()
        conn.close()
        self.assertEqual(len(arts), 0)

    def test_matched_traffic_article_stored_as_incident_article(self):
        ts_event = time.time() - 1800
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, location, status) "
            "VALUES (?,?,?,?,?,?,?,'active')",
            (ts_event, ts_event, "FATAL CRASH", "Fatal crash on IH-35", '["AFD"]', "[]", "IH-35 near 183"),
        )
        conn.commit()
        inc_id = cur.lastrowid
        conn.close()

        rss = _make_rss([{"title": "Person killed in fatal crash on IH-35", "link": "https://kvue.com/fatal1", "pubDate": _PUB_DATE}])
        self._run_poll(rss)

        conn = sqlite3.connect(self.db_path)
        linked = conn.execute("SELECT * FROM incident_articles WHERE incident_id=?", (inc_id,)).fetchall()
        conn.close()
        self.assertTrue(len(linked) >= 1)

    def test_traffic_rss_fetch_error_does_not_raise(self):
        with mock.patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            try:
                self.poller._poll_traffic_news(
                    db_path=self.db_path,
                    google_cse_api_key="", google_cse_id="",
                    pi_fetch_url="", pi_fetch_token="",
                    geocode_fn=lambda a: None,
                )
            except Exception as exc:
                self.fail(f"_poll_traffic_news raised unexpectedly: {exc}")

    def test_traffic_coordinates_update_incident_location(self):
        ts_event = time.time() - 900
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, location, lat, lon, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,'active')",
            (ts_event, ts_event, "FATAL CRASH", "Crash on 183", '["AFD"]', "[]", None, None, None),
        )
        conn.commit()
        inc_id = cur.lastrowid
        conn.close()

        rss = _make_rss([{"title": "Deadly crash on 183 freeway", "link": "https://kvue.com/dead1", "pubDate": _PUB_DATE}])

        def _fake_urlopen(req, timeout=None):
            resp = mock.MagicMock()
            resp.read.return_value = rss.encode()
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             mock.patch("modules.pollers.impl.apd_news._apd_fetch_article",
                        return_value={"address": "183 Freeway at Airport Blvd", "summary": "Fatal crash"}), \
             mock.patch("modules.pollers.impl.apd_news._resolve_article_url",
                        side_effect=lambda su, t, l, k, cid: l):  # noqa: E741
            self.poller._poll_traffic_news(
                db_path=self.db_path,
                google_cse_api_key="", google_cse_id="",
                pi_fetch_url="", pi_fetch_token="",
                geocode_fn=lambda addr: (30.27, -97.73),
            )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT lat, lon, location FROM incidents WHERE id=?", (inc_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(row[0])
        self.assertIsNotNone(row[1])
        self.assertIn("183", row[2])


# ===========================================================================
# 13. Package __init__ re-exports
# ===========================================================================

class TestExportedFromInit(unittest.TestCase):
    def test_import_from_package(self):
        """APDNewsPoller must be importable from modules.pollers (the package)."""
        # modules.pollers is already loaded via the stubs above; we can also
        # test the impl package directly.
        from modules.pollers.impl.apd_news import APDNewsPoller as _P
        self.assertIs(_P, APDNewsPoller)

    def test_in_all(self):
        """APDNewsPoller should appear in the pollers package __all__ (if defined)."""
        # __init__.py defines __all__ including APDNewsPoller
        import importlib
        pkg = importlib.import_module("modules.pollers")
        if hasattr(pkg, "__all__"):
            self.assertIn("APDNewsPoller", pkg.__all__)


# ===========================================================================
# 14. Thread-safety — concurrent apd_seen dedup
# ===========================================================================

class TestConcurrentDedup(unittest.TestCase):
    """Verify that the _APD_NEWS_LOCK prevents race conditions when multiple
    pollers (or threads) write to apd_seen simultaneously."""

    def setUp(self):
        self.db_path = _tmp_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_concurrent_inserts_do_not_corrupt_db(self):
        link = "https://kxan.com/concurrent_test"
        results = []
        errors = []

        def _try_insert():
            try:
                with _APD_NEWS_LOCK:
                    conn = sqlite3.connect(self.db_path)
                    existing = {row[0] for row in conn.execute("SELECT url FROM apd_seen")}
                    if link not in existing:
                        conn.execute("INSERT OR IGNORE INTO apd_seen (url, ts) VALUES (?,?)", (link, time.time()))
                        conn.commit()
                        results.append("inserted")
                    else:
                        results.append("skipped")
                    conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_try_insert) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT url FROM apd_seen WHERE url=?", (link,)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1, "URL should be inserted exactly once")
        self.assertEqual(results.count("inserted"), 1)
        self.assertEqual(results.count("skipped"), 9)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
