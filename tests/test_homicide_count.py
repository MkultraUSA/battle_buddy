"""
tests/test_homicide_count.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for modules/homicide_count.py — canonical area-wide homicide
counting policy.

Coverage:
  - _is_valid_url()          — validation edge cases
  - _normalise_url()         — tracking-param stripping
  - load_seed()              — file I/O, malformed JSON, missing file
  - fetch_live_homicides()   — DB query, filtering
  - canonical_homicides()    — dedup, source_url gating, agency breakdown
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from modules.homicide_count import (
    _agency_from_entry,
    _is_valid_url,
    _normalise_url,
    canonical_homicides,
    fetch_live_homicides,
    load_seed,
)

# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

class TestIsValidUrl(unittest.TestCase):
    def test_valid_https(self):
        self.assertTrue(_is_valid_url("https://kxan.com/story"))

    def test_valid_http(self):
        self.assertTrue(_is_valid_url("http://example.com"))

    def test_empty_string(self):
        self.assertFalse(_is_valid_url(""))

    def test_none(self):
        self.assertFalse(_is_valid_url(None))

    def test_not_a_string(self):
        self.assertFalse(_is_valid_url(123))

    def test_whitespace_only(self):
        self.assertFalse(_is_valid_url("   "))

    def test_ftp_rejected(self):
        self.assertFalse(_is_valid_url("ftp://example.com"))

    def test_partial_url(self):
        self.assertFalse(_is_valid_url("kxan.com/story"))


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

class TestNormaliseUrl(unittest.TestCase):
    def test_strips_query_params(self):
        url = "https://kxan.com/story?id=123&utm_source=feed"
        self.assertEqual(_normalise_url(url), "https://kxan.com/story")

    def test_strips_fragment(self):
        url = "https://kxan.com/story#section"
        self.assertEqual(_normalise_url(url), "https://kxan.com/story")

    def test_strips_trailing_slash(self):
        url = "https://kxan.com/story/"
        self.assertEqual(_normalise_url(url), "https://kxan.com/story")

    def test_preserves_path(self):
        url = "https://kxan.com/news/local/homicide"
        self.assertEqual(_normalise_url(url), "https://kxan.com/news/local/homicide")


# ---------------------------------------------------------------------------
# Seed loader
# ---------------------------------------------------------------------------

class TestLoadSeed(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(load_seed("/nonexistent/path.json"), [])

    def test_valid_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([{"n": 1, "url": "https://example.com"}], f)
            path = f.name
        try:
            data = load_seed(path)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["n"], 1)
        finally:
            os.unlink(path)

    def test_malformed_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json{{{")
            path = f.name
        try:
            self.assertEqual(load_seed(path), [])
        finally:
            os.unlink(path)

    def test_non_list_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"key": "value"}, f)
            path = f.name
        try:
            self.assertEqual(load_seed(path), [])
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Live DB fetcher
# ---------------------------------------------------------------------------

def _make_test_db() -> str:
    """Create a temp DB with the incidents table and return its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE incidents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start    REAL NOT NULL,
            ts_updated  REAL NOT NULL,
            ts_cleared  REAL,
            itype       TEXT,
            description TEXT,
            agencies    TEXT,
            tgids       TEXT,
            location    TEXT,
            lat         REAL,
            lon         REAL,
            status      TEXT DEFAULT 'active',
            article_url TEXT,
            is_test     INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    return path


class TestFetchLiveHomicides(unittest.TestCase):
    def setUp(self):
        self.db_path = _make_test_db()

    def tearDown(self):
        os.unlink(self.db_path)

    def _insert(self, **kw):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO incidents
               (ts_start, ts_updated, itype, description, location, lat, lon,
                article_url, agencies, is_test)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                kw.get("ts_start", 1768435200.0),
                kw.get("ts_updated", 1768435200.0),
                kw.get("itype", "HOMICIDE"),
                kw.get("description", "test"),
                kw.get("location", "123 Main St"),
                kw.get("lat", 30.25),
                kw.get("lon", -97.75),
                kw.get("article_url", None),
                kw.get("agencies", '["APD"]'),
                kw.get("is_test", 0),
            ),
        )
        conn.commit()
        conn.close()

    def test_returns_homicide_rows(self):
        self._insert(article_url="https://kxan.com/1")
        rows = fetch_live_homicides(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://kxan.com/1")

    def test_excludes_non_homicide(self):
        self._insert(itype="SHOOTING")
        rows = fetch_live_homicides(self.db_path)
        self.assertEqual(len(rows), 0)

    def test_excludes_null_lat(self):
        self._insert(lat=None)
        rows = fetch_live_homicides(self.db_path)
        self.assertEqual(len(rows), 0)

    def test_excludes_null_lon(self):
        self._insert(lon=None)
        rows = fetch_live_homicides(self.db_path)
        self.assertEqual(len(rows), 0)

    def test_excludes_test_rows(self):
        self._insert(is_test=1)
        rows = fetch_live_homicides(self.db_path)
        self.assertEqual(len(rows), 0)

    def test_includes_agencies_column(self):
        self._insert(article_url="https://kxan.com/1", agencies='["APD"]')
        rows = fetch_live_homicides(self.db_path)
        self.assertEqual(rows[0]["agencies"], '["APD"]')


# ---------------------------------------------------------------------------
# Canonical merge + dedup
# ---------------------------------------------------------------------------

class TestCanonicalHomicides(unittest.TestCase):
    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _seed_entry(**kw) -> dict:
        return {
            "n": kw.get("n", 1),
            "date": kw.get("date", "2026-01-01"),
            "address": kw.get("address", "123 Main St"),
            "victim": kw.get("victim", "Unknown"),
            "summary": kw.get("summary", "test"),
            "url": kw.get("url", "https://example.com/1"),
            "lat": kw.get("lat", 30.25),
            "lon": kw.get("lon", -97.75),
            "source": kw.get("source", ""),
        }

    @staticmethod
    def _live_entry(**kw) -> dict:
        return {
            "source": "scanner",
            "date": kw.get("date", "2026-01-01"),
            "itype": kw.get("itype", "HOMICIDE"),
            "summary": kw.get("summary", "test"),
            "address": kw.get("address", "123 Main St"),
            "lat": kw.get("lat", 30.25),
            "lon": kw.get("lon", -97.75),
            "url": kw.get("url", "https://example.com/1"),
            "agencies": kw.get("agencies", '["APD"]'),
            "_db_id": kw.get("_db_id", 1),
        }

    # -- tests -----------------------------------------------------------

    def test_empty_inputs(self):
        canonical, total, by_agency = canonical_homicides([], [])
        self.assertEqual(canonical, [])
        self.assertEqual(total, 0)
        self.assertEqual(by_agency, {})

    def test_seed_only(self):
        seed = [self._seed_entry(url="https://example.com/1")]
        canonical, total, by_agency = canonical_homicides(seed, [])
        self.assertEqual(len(canonical), 1)
        self.assertEqual(total, 1)

    def test_live_only(self):
        live = [self._live_entry(url="https://example.com/1")]
        canonical, total, by_agency = canonical_homicides([], live)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(total, 1)

    def test_dedup_same_url(self):
        seed = [self._seed_entry(url="https://example.com/1")]
        live = [self._live_entry(url="https://example.com/1")]
        canonical, total, by_agency = canonical_homicides(seed, live)
        self.assertEqual(len(canonical), 1)
        self.assertEqual(total, 1)

    def test_dedup_by_normalised_url(self):
        seed = [self._seed_entry(url="https://example.com/1")]
        live = [self._live_entry(url="https://example.com/1?utm_source=feed")]
        canonical, total, by_agency = canonical_homicides(seed, live)
        self.assertEqual(len(canonical), 1)

    def test_missing_url_excluded_from_total(self):
        seed = [self._seed_entry(url="")]
        canonical, total, by_agency = canonical_homicides(seed, [])
        self.assertEqual(len(canonical), 1)  # kept in list
        self.assertEqual(total, 0)          # but not counted

    def test_invalid_url_excluded_from_total(self):
        seed = [self._seed_entry(url="not-a-url")]
        canonical, total, by_agency = canonical_homicides(seed, [])
        self.assertEqual(total, 0)

    def test_valid_url_counted(self):
        seed = [self._seed_entry(url="https://example.com/1")]
        _, total, _ = canonical_homicides(seed, [])
        self.assertEqual(total, 1)

    def test_multiple_different_urls(self):
        seed = [
            self._seed_entry(n=1, url="https://example.com/1"),
            self._seed_entry(n=2, url="https://example.com/2"),
        ]
        canonical, total, _ = canonical_homicides(seed, [])
        self.assertEqual(len(canonical), 2)
        self.assertEqual(total, 2)

    def test_agency_breakdown(self):
        seed = [
            self._seed_entry(url="https://example.com/1", source="APD"),
        ]
        live = [
            self._live_entry(url="https://example.com/2", agencies='["TCSO"]'),
        ]
        _, total, by_agency = canonical_homicides(seed, live)
        self.assertEqual(total, 2)
        self.assertEqual(by_agency.get("APD"), 1)
        self.assertEqual(by_agency.get("TCSO"), 1)

    def test_agency_breakdown_sums_to_total(self):
        seed = [
            self._seed_entry(n=1, url="https://a.com/1"),
            self._seed_entry(n=2, url="https://a.com/2"),
        ]
        live = [
            self._live_entry(url="https://b.com/1", agencies='["TCSO"]'),
            self._live_entry(url="https://b.com/2", agencies='["APD"]'),
        ]
        _, total, by_agency = canonical_homicides(seed, live)
        self.assertEqual(total, sum(by_agency.values()))

    def test_no_valid_urls_gives_zero_total(self):
        seed = [
            self._seed_entry(url=""),
            self._seed_entry(n=2, url=""),
        ]
        _, total, by_agency = canonical_homicides(seed, [])
        self.assertEqual(total, 0)
        self.assertEqual(by_agency, {})

    def test_seed_takes_priority_over_live(self):
        seed = [self._seed_entry(url="https://example.com/1", victim="From Seed")]
        live = [self._live_entry(url="https://example.com/1", summary="From Live")]
        canonical, _, _ = canonical_homicides(seed, live)
        self.assertEqual(canonical[0]["victim"], "From Seed")

    def test_live_fills_gap_when_seed_has_no_url(self):
        seed = [self._seed_entry(url="")]
        live = [self._live_entry(url="https://example.com/1")]
        canonical, total, _ = canonical_homicides(seed, live)
        self.assertEqual(len(canonical), 2)
        self.assertEqual(total, 1)  # only the live entry has a valid URL


# ---------------------------------------------------------------------------
# Agency extraction
# ---------------------------------------------------------------------------

class TestAgencyFromEntry(unittest.TestCase):
    def test_scanner_with_apd_agencies(self):
        entry = {"source": "scanner", "agencies": '["APD", "AFD"]'}
        self.assertEqual(_agency_from_entry(entry), "APD")

    def test_scanner_with_empty_agencies(self):
        entry = {"source": "scanner", "agencies": "[]"}
        self.assertEqual(_agency_from_entry(entry), "Unknown")

    def test_non_scanner_source(self):
        entry = {"source": "FOX 7 Austin / Pflugerville PD"}
        self.assertEqual(_agency_from_entry(entry), "Pflugerville PD")

    def test_non_scanner_source_no_known_agency(self):
        entry = {"source": "Some Random Blog"}
        self.assertEqual(_agency_from_entry(entry), "Some Random Blog")

    def test_missing_source_and_agencies(self):
        entry = {}
        self.assertEqual(_agency_from_entry(entry), "Unknown")


if __name__ == "__main__":
    unittest.main()
