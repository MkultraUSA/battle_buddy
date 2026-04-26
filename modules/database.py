"""
modules/database.py — SQLite helper functions for Battle Buddy.

Extracted from audio_receiver.py (lines 436-675).
All functions that previously relied on module-level DB_PATH and CAT_COORDS
now import those constants from modules.config — no circular imports.
"""

import json
import sqlite3
import time

from modules.config import CAT_COORDS, DB_PATH


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            tgid        INTEGER,
            tag         TEXT,
            category    TEXT,
            node        TEXT,
            duration    REAL,
            transcript  TEXT,
            lat         REAL,
            lon         REAL,
            location    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
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
            status      TEXT DEFAULT 'active'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            username    TEXT    NOT NULL,
            beat        TEXT    NOT NULL DEFAULT 'all',
            PRIMARY KEY (username, beat)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_calls (
            incident_id INTEGER NOT NULL,
            call_id     INTEGER NOT NULL,
            PRIMARY KEY (incident_id, call_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_escalations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            ts          REAL    NOT NULL,
            stage       TEXT    NOT NULL,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tgid_guesses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tgid        INTEGER NOT NULL,
            ts          REAL    NOT NULL,
            guess       TEXT    NOT NULL,
            category    TEXT,
            confidence  TEXT,
            reasoning   TEXT,
            transcript  TEXT,
            confirmed   INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drone_sightings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            serial      TEXT    NOT NULL,
            ua_type     INTEGER DEFAULT 0,
            lat         REAL    NOT NULL,
            lon         REAL    NOT NULL,
            alt_geo     REAL,
            alt_agl     REAL,
            speed_ms    REAL,
            heading     INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tips (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL NOT NULL,
            location_text   TEXT,
            lat             REAL,
            lon             REAL,
            description     TEXT,
            photo_path      TEXT,
            status          TEXT DEFAULT 'pending',
            source          TEXT DEFAULT 'web',
            incident_id     INTEGER,
            reviewer_note   TEXT

        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_articles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER,
            ts          REAL NOT NULL,
            headline    TEXT NOT NULL,
            url         TEXT NOT NULL,
            source      TEXT,
            snippet     TEXT,
            match_score REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aircraft_positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            icao24      TEXT    NOT NULL,
            callsign    TEXT,
            lat         REAL    NOT NULL,
            lon         REAL    NOT NULL,
            alt_ft      INTEGER,
            heading     REAL,
            speed_kts   REAL,
            is_leo      INTEGER DEFAULT 0,
            label       TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_ts ON aircraft_positions(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_icao ON aircraft_positions(icao24, ts)")
    conn.commit()
    conn.close()


def get_subscribers(itype: str, category: str) -> list[str]:
    """Return usernames subscribed to this incident type/category."""
    beat_map = {
        "APD": "apd", "TCSO": "apd", "UTPD": "apd", "DPS": "apd",
        "AFD": "fire-ems", "TCFD": "fire-ems", "TCEMS": "fire-ems",
    }
    beat = beat_map.get(category, "general")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT username FROM subscriptions WHERE beat='all' OR beat=?", (beat,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_subscription(username: str, beat: str = "all"):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO subscriptions (username, beat) VALUES (?,?)",
                 (username, beat))
    conn.commit()
    conn.close()


def remove_subscription(username: str, beat: str = "all"):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM subscriptions WHERE username=? AND beat=?", (username, beat))
    conn.commit()
    conn.close()


def insert_call(ts, tgid, tag, category, node, duration, transcript, lat, lon, location, coords_approx=0) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO calls (ts,tgid,tag,category,node,duration,transcript,lat,lon,location,coords_approx) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, tgid, tag, category, node, duration, transcript, lat, lon, location, coords_approx)
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def recent_calls(limit=200):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM calls ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def calls_since(since_ts: float) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM calls WHERE ts > ? ORDER BY ts DESC", (since_ts,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def calls_for_sitrep(minutes=60):
    return calls_since(time.time() - minutes * 60)


def _fill_incident_coords(inc: dict) -> dict:
    """Fill in lat/lon from CAT_COORDS if the incident has none."""
    if inc.get("lat") is None or inc.get("lon") is None:
        try:
            agencies = json.loads(inc.get("agencies") or "[]")
            cat = agencies[0] if agencies else "Unknown"
        except Exception:
            cat = "Unknown"
        lat, lon = CAT_COORDS.get(cat, CAT_COORDS["Unknown"])
        inc["lat"] = lat
        inc["lon"] = lon
        inc["_coords_approx"] = True
    return inc


def active_incidents() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Active = status 'active' and updated within the last 30 min
    cutoff = time.time() - 30 * 60
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status='active' AND ts_updated > ? ORDER BY ts_updated DESC",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [_fill_incident_coords(dict(r)) for r in rows]


def get_all_incidents(limit=50) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY ts_start DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [_fill_incident_coords(dict(r)) for r in rows]
