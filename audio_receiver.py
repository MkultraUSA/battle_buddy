#!/usr/bin/env python3
"""
Battle Buddy — Audio Receiver / Brain  v2.0

Receives call audio from call_recorder.py on Pi 1,
transcribes with Whisper, categorizes by talkgroup,
detects incidents, controls OP25 hold/skip on Pi 1,
stores in SQLite, and serves a map + sitrep web UI.

Usage:
    python3 audio_receiver.py [--port 9001] [--model base] [--enable-hold]
"""

import argparse
import uuid
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import ssl
import subprocess
import shutil
import pwd
import tempfile
import threading
import time
import urllib.request
try:
    import anthropic
except ImportError:
    anthropic = None
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
_CDT = ZoneInfo("America/Chicago")

# Bypass SSL cert verification for all urllib calls (Nextcloud snap cert not in system store)
_ssl_ctx = ssl._create_unverified_context()
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_ctx))
)

from flask import Flask, jsonify, render_template_string, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

from modules.config import DB_PATH
from modules.incident_engine import (
    analyze_for_incident,
    incident_cleanup_thread,
    load_active_incidents,
    register_callbacks,
    _active_incidents,
    _incident_lock,
)
from modules import queue_manager
TIPS_UPLOAD_DIR = "/opt/battlebuddy/static/tips"
TGID_TSV      = "/opt/battlebuddy/gatrrs-tags.tsv"
PI1_OP25_URL  = "http://radiodesk.ddns.net:8080/"

# Groq — LLM incident analysis (llama-3.3-70b), called directly from Contabo
# Audio transcription is LOCAL (faster-whisper), works offline in the field
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL          = "llama-3.3-70b-versatile"
GROQ_ENABLED        = bool(GROQ_API_KEY)
GROQ_API_BASE       = "https://api.groq.com/openai/v1"

# Anthropic Claude — used for intel query synthesis
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_ENABLED   = bool(ANTHROPIC_API_KEY) and (anthropic is not None)

# Nextcloud Talk — post each transcript to the BattleBuddy room
TALK_BASE    = "https://kevcloud.ddns.net/ocs/v2.php/apps/spreed/api/v1"
TALK_USER    = "battlebuddy"
TALK_PASS    = os.environ.get("TALK_PASS", "")
TALK_ENABLED = True

# Mailgun email alerts
MAILGUN_API_KEY  = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN   = os.environ.get("MAILGUN_DOMAIN", "")
MAILGUN_FROM     = f"Battle Buddy <mailgun@{MAILGUN_DOMAIN}>"
ALERT_EMAIL      = "k.watkins@me.com"

# Google Custom Search — article URL resolution for APD news poller
GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_ID      = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_ROUTES_KEY  = os.environ.get("GOOGLE_ROUTES_KEY", "")
GOOGLE_MAPS_JS_KEY = os.environ.get("GOOGLE_MAPS_JS_KEY", "")

# Pi5 Fetch Agent — residential IP article fetcher (fetch_agent.py on Pi)
# Quick-tunnel URL changes on Pi reboot; update PI_FETCH_URL in .env after restart.
# Upgrade to a named Cloudflare Tunnel for a stable subdomain.
PI_FETCH_URL   = os.environ.get("PI_FETCH_URL", "").rstrip("/")
PI_FETCH_TOKEN = os.environ.get("PI_FETCH_TOKEN", "")
PI_FETCH_ENABLED = bool(PI_FETCH_URL and PI_FETCH_TOKEN)

# FreeTAKServer ATAK integration
FTS_HOST      = "radiodesk.ddns.net"
FTS_REST_PORT = 19023
FTS_COT_PORT  = 8089
FTS_TOKEN     = "token"
FTS_ENABLED   = True

# Deck integration
DECK_BASE     = "https://kevcloud.ddns.net/index.php/apps/deck/api/v1.0"

# Nextcloud WebDAV — incident snapshot exports
NC_WEBDAV     = "https://kevcloud.ddns.net/remote.php/dav/files/kevin"
NC_USER       = os.environ.get("NC_USER", "")
NC_PASS       = os.environ.get("NC_PASS", "")
NC_REPORT_DIR = "PresentationNotes/FlaggedIncidents"
DECK_BOARD_ID = 2
DECK_STACK_NEW = 5        # 🆕 New
DECK_LABELS   = {
    "SHOOTING":           10,
    "OFFICER DOWN":       11,
    "STRUCTURE FIRE":     12,
    "MASS CASUALTY":      13,
    "HAZMAT":             14,
    "CRASH/COLLISION":    15,
    "MULTI-AGENCY RESPONSE": 16,
    "AIR ASSET ACTIVE":   17,
    "DPS CAPITOL ACTIVATION": 18,
}

# Beat rooms — each category routes to its own room
# Main room is the original catch-all (kept for bot commands)
TALK_ROOM    = "iyidr3xy"   # general / catch-all (kxq9mkms was deleted)
TALK_ROOMS   = {
    "incidents": "89q5fnh5",  # 🔴/🟡 priority alerts only
    "apd":       "m38srso2",  # APD traffic
    "fire-ems":  "ee6si4vj",  # AFD, TCFD, TCEMS
    "general":   "iyidr3xy",  # everything else
}

# Which categories route to which room
CATEGORY_ROOM = {
    "APD":   "apd",
    "AFD":   "fire-ems",
    "TCFD":  "fire-ems",
    "TCEMS": "fire-ems",
    "TCSO":  "apd",
    "UTPD":  "apd",
    "DPS":   "apd",
}

def _room_for_call(call: dict, priority: str) -> list[str]:
    """Return list of room tokens to post to for this call."""
    cat    = call.get("category", "Unknown")
    rooms  = set()
    # Always post to the category beat room
    beat   = CATEGORY_ROOM.get(cat, "general")
    rooms.add(TALK_ROOMS[beat])
    # Also post 🔴/🟡 to the incidents room
    if priority in ("🔴", "🟡"):
        rooms.add(TALK_ROOMS["incidents"])
    return list(rooms)

# Talk bot shared secret — must match what is registered with occ talk:bot:install
TALK_BOT_SECRET = os.environ.get("TALK_BOT_SECRET", "")

# Hold/skip commands to Pi 1 OP25 — OFF until behavior is verified.
# Run with --enable-hold to turn on.
HOLD_ENABLED = False

# Release hold after this many minutes of silence on the held channel.
HOLD_RELEASE_MINUTES = 5

# Per-type incident timeout (minutes of silence before auto-close).
# Timer resets any time a new call updates the incident.
INCIDENT_TIMEOUT_MINUTES = {
    "SHOOTING":               120,
    "OFFICER DOWN":           120,
    "PURSUIT":                120,
    "WEAPONS":                120,
    "STABBING":               120,
    "MASS CASUALTY":          120,
    "STRUCTURE FIRE":          45,
    "HAZMAT":                  45,
    "FIRE DISPATCH":           20,
    "AIR ASSET ACTIVE":        20,
    "DPS CAPITOL ACTIVATION":  20,
    "CRASH/COLLISION":         30,
    "PEDESTRIAN INCIDENT":     30,
    "DEATH INVESTIGATION":     60,
    "FATAL CRASH":             60,
}
_INCIDENT_TIMEOUT_DEFAULT = 10  # minutes — crash, generic keyword hits

# Multi-agency convergence window (minutes)
MULTIAGENCY_WINDOW_MIN = 15

# APD surge detection
APD_SURGE_WINDOW_MIN  = 10
APD_SURGE_THRESHOLD   = 4   # calls within window to trigger

# ---------------------------------------------------------------------------
# Talkgroup loading — from RadioReference TSV export
# ---------------------------------------------------------------------------

# Tag substrings that mark a talkgroup as non-public-safety (skip Whisper)
IGNORE_TAGS = [
    "Aus Wtr", "AusWtr", "WATER",
    "SOLID", "SW RECYCLE", "RECYCLE", "STORMWATER",
    "Parking", "ParkingMeter",
    "AusLibrary", "Library",
    "AusEnergy", "Austin Energy", "AusEnergy",
    "ACO ", "Animal Ctr", "Animal Control",
    "Recyc CM", "Aus Recyc",
    "TXDOT Event", "TXDOT EOC", "TXDOT Security", "TXDOT WIDE",
    "GB Juv", "Juv JC",
    "Code Enf", "CodeEnf",
]

# Map tag substrings → agency category
CATEGORY_PATTERNS = [
    ("APD",          ["APD"]),
    ("AFD",          ["AFD"]),
    ("TCEMS",        ["TCEMS", "St Davids"]),
    ("ABIA",         ["ABIA"]),
    ("TCSO",         ["TCSO"]),
    ("TCFD",         ["TCFD", "TCFMD"]),
    ("UTPD",         ["UT PD", "UT 29"]),
    ("DPS",          ["DPS", "THP", "Trooper", "State Trooper", "Highway Patrol", "Capitol Protect"]),
    ("Bastrop",      ["Bastrop"]),
    ("Burnet",       ["Burnet", "Llano", "Blanco", "Hamilton"]),
    ("Comal",        ["Comal"]),
    ("Kerr",         ["Kerr"]),
    ("Pflugerville", ["Pflug"]),
    ("Lakeway",      ["Lakeway"]),
    ("TXDOT",        ["TXDOT Hero"]),
    ("Interop",      ["Interop"]),
]

# Default map coordinates by category
CAT_COORDS = {
    "APD":          (30.2672, -97.7431),
    "AFD":          (30.2672, -97.7431),
    "TCEMS":        (30.2672, -97.7431),
    "ABIA":         (30.1975, -97.6664),
    "TCSO":         (30.2672, -97.7431),
    "TCFD":         (30.2672, -97.7431),
    "UTPD":         (30.2849, -97.7341),
    "DPS":          (30.2747, -97.7404),   # Texas State Capitol
    "Bastrop":      (30.1107, -97.3154),
    "Burnet":       (30.7488, -98.2345),
    "Comal":        (29.7030, -98.1245),
    "Kerr":         (30.0474, -99.1403),
    "Pflugerville": (30.4394, -97.6200),
    "Lakeway":      (30.3577, -97.9772),
    "TXDOT":        (30.2672, -97.7431),
    "Interop":      (30.2672, -97.7431),
    "Unknown":      (30.2672, -97.7431),
}

CAT_COLORS = {
    "APD":          "#3b82f6",
    "AFD":          "#ef4444",
    "TCEMS":        "#f97316",
    "ABIA":         "#8b5cf6",
    "TCSO":         "#06b6d4",
    "TCFD":         "#f43f5e",
    "UTPD":         "#a78bfa",
    "DPS":          "#fbbf24",   # Gold — state agency
    "Interop":      "#6b7280",
    "Bastrop":      "#10b981",
    "Burnet":       "#14b8a6",
    "Comal":        "#f59e0b",
    "Kerr":         "#ec4899",
    "Pflugerville": "#84cc16",
    "Lakeway":      "#0ea5e9",
    "TXDOT":        "#fb923c",
    "Unknown":      "#9ca3af",
}

# ---------------------------------------------------------------------------
# DPS / Capitol intelligence
# Austin is the Texas state capital. DPS is not just highway patrol —
# they protect the Capitol complex with bicycle units, mounted (horse) patrol,
# ATVs, motorcycles, and air assets (helicopters). DPS activity near downtown
# often signals a dignitary visit, protest response, or Capitol security event.
# ---------------------------------------------------------------------------

# Keywords in transcripts that reveal DPS asset type
DPS_ASSET_PATTERNS = [
    (re.compile(r'\b(helo|helicopter|air\s*unit|aviation|bird|fly[ing]*\s*over|airship)\b', re.I), "Air Asset"),
    (re.compile(r'\b(horse|mounted|equine|cavalry)\b', re.I),                                      "Mounted Unit"),
    (re.compile(r'\b(bicycle|bike\s*unit|bike\s*patrol|cycle)\b', re.I),                           "Bicycle Unit"),
    (re.compile(r'\b(atv|four.wheel|quad|off.road)\b', re.I),                                      "ATV Unit"),
    (re.compile(r'\b(motorcycle|motor\s*unit|moto)\b', re.I),                                      "Motorcycle Unit"),
    (re.compile(r'\b(sniper|counter.sniper|overwatch|rooftop)\b', re.I),                           "Sniper/Overwatch"),
    (re.compile(r'\b(dignitary|protectee|detail|motorcade|convoy)\b', re.I),                       "Dignitary Protection"),
    (re.compile(r'\b(governor|lieutenant\s*gov|senator|legislat|session)\b', re.I),                "Capitol Event"),
    (re.compile(r'\b(protest|demonstrat|crowd\s*control|civil\s*disturbance|unlawful\s*assembly)\b', re.I), "Crowd Control"),
]

# Keywords that signal DPS involvement even on non-DPS talkgroups
DPS_MENTION_PATTERNS = re.compile(
    r'\b(dps|state\s*trooper|highway\s*patrol|texas\s*ranger|ranger\s*unit|capitol\s*police|'
    r'protect.*detail|executive\s*protect)\b', re.I
)

# Capitol-area location hints
CAPITOL_KEYWORDS = ["capitol", "state capitol", "congress ave", "11th street", "governor",
                    "state cemetery", "governor's mansion", "mansion"]


def detect_dps_assets(transcript: str) -> list[str]:
    """Return list of DPS asset types detected in a transcript."""
    if not transcript:
        return []
    found = []
    for pattern, label in DPS_ASSET_PATTERNS:
        if pattern.search(transcript):
            found.append(label)
    return found


def is_capitol_area(transcript: str, location: str | None) -> bool:
    """Return True if the call appears to be in/around the Capitol complex."""
    text = (transcript + " " + (location or "")).lower()
    return any(k in text for k in CAPITOL_KEYWORDS)


def mentions_dps(transcript: str) -> bool:
    """Return True if any agency's transcript references DPS."""
    return bool(DPS_MENTION_PATTERNS.search(transcript or ""))


# APD Metro 1-10 (972-987) — active only for Cap Metro transit incidents
TRANSIT_TGIDS = set(range(972, 988))

# Fire dispatch channels — when active, a significant fire response is likely
LOCUTION_TGIDS = {1147, 1162}   # AFD Locution, TCFD Locution

# Known Whisper misreads on locution transcripts.
# Locution systems read a CAD incident type code as the first word(s) of each
# dispatch message (e.g. "Stabbing, check for staging..." or "Structure Fire...").
# Whisper occasionally mishears these short phonetic codes.  These corrections
# are applied only to LOCUTION_TGID transcripts before classification so that
# Rule 3 (keyword match) and Rule 4 (locution dispatch) get clean input.
LOCUTION_CORRECTIONS: list[tuple[re.Pattern, str]] = [
    # "Assault" at the start of a locution dispatch → likely "A stab" or
    # "Stabbing" misheard by Whisper.  Fire/EMS locutions use "Assault" as the
    # CAD code for a stabbing/cutting victim, so this also normalises genuine
    # CAD-coded assaults that are physically stabbings.
    (re.compile(r'(?i)assault'), 'stabbing'),
    # "A salt" / "a salt" → occasionally produced by Whisper for "assault"
    (re.compile(r'(?i)a salt'), 'stabbing'),
]

def _apply_locution_corrections(transcript: str) -> str:
    """Apply LOCUTION_CORRECTIONS substitutions to a locution transcript."""
    for pattern, replacement in LOCUTION_CORRECTIONS:
        transcript = pattern.sub(replacement, transcript)
    return transcript

# Airport emergency — tgid 1481 turned out to be routine ops chatter, not alerts.
# Leaving this empty until the real ABIA emergency channel is identified.
ABIA_ALERT_TGIDS = set()

# ABIA operational talkgroups — routine airport ops that use alarming-sounding
# language (barricade, hostage, weapons, code red) in normal daily context.
# Exclude from keyword matching to prevent false positives.
ABIA_OPS_TGIDS = {1471, 1472, 1474, 1476, 1478, 1479, 1480, 1481, 1487}

# Air asset talkgroups — ANY activity here is high-signal news.
# A police helicopter in the air means pursuit, active shooter perimeter,
# search operation, crowd overwatch, or dignitary movement.
AIR_ASSET_TGIDS = {989, 1521, 1522, 1523}  # APD Air/K9, APD Aviation 1/2/CID

# Transcript patterns that indicate air asset deployment across any agency
AIR_ASSET_PATTERN = re.compile(
    r'\b(helo|helicopter|air\s*(?:unit|support|asset|one|two)|aviation|'
    r'bird\s*(?:up|in\s*the\s*air|is\s*up|overhead)|'
    r'chopper|aircraft|fly[ing]*\s*over|eye\s*in\s*the\s*sky|'
    r'unit\s*(?:air|a/?c)|airship|rotary)\b', re.I
)

# What air asset deployment typically signals — for reporter context
AIR_ASSET_CONTEXT = {
    "APD":   "pursuit, active shooter perimeter, search, or crowd overwatch",
    "DPS":   "dignitary protection, Capitol overwatch, or major protest response",
    "TCSO":  "rural search, pursuit, or major incident perimeter",
    "AFD":   "aerial water drop or large structure fire recon",
    "ABIA":  "aircraft emergency or airfield security",
    "default": "major law enforcement or emergency response operation",
}


def detect_air_asset(tgid: int, transcript: str, category: str) -> str | None:
    """Return a context string if an air asset is active, else None."""
    if tgid in AIR_ASSET_TGIDS or AIR_ASSET_PATTERN.search(transcript or ""):
        context = AIR_ASSET_CONTEXT.get(category, AIR_ASSET_CONTEXT["default"])
        return context
    return None

IGNORE_TGIDS: set[int] = set()
TGID_META: dict[int, dict] = {}


def _tag_is_ignored(tag: str) -> bool:
    tl = tag.lower()
    return any(p.lower() in tl for p in IGNORE_TAGS)


def _tag_to_category(tag: str) -> str:
    for cat, patterns in CATEGORY_PATTERNS:
        if any(p.lower() in tag.lower() for p in patterns):
            return cat
    return "Unknown"


def load_talkgroups(tsv_path: str = TGID_TSV):
    global TGID_META, IGNORE_TGIDS
    if not os.path.exists(tsv_path):
        print(f"[tg] TSV not found at {tsv_path} — using built-in metadata only", flush=True)
        return
    loaded = ignored = 0
    with open(tsv_path) as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            try:
                tgid = int(parts[0])
            except ValueError:
                continue
            tag = parts[1].strip()
            if _tag_is_ignored(tag):
                IGNORE_TGIDS.add(tgid)
                ignored += 1
            else:
                cat       = _tag_to_category(tag)
                lat, lon  = CAT_COORDS.get(cat, CAT_COORDS["Unknown"])
                TGID_META[tgid] = {"tag": tag, "cat": cat, "lat": lat, "lon": lon}
                loaded += 1
    print(f"[tg] {loaded} talkgroups loaded, {ignored} on ignore list", flush=True)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address_key  TEXT PRIMARY KEY,
            lat          REAL,
            lon          REAL,
            ts_cached    REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_geocode_key ON geocode_cache(address_key)"
    )
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


# ---------------------------------------------------------------------------
# Location extraction
# ---------------------------------------------------------------------------

LOCATION_HINTS = [
    ("state capitol",   30.2747, -97.7404),
    ("capitol complex", 30.2747, -97.7404),
    ("governor's mansion", 30.2757, -97.7417),
    ("congress ave",    30.2672, -97.7431),
    ("congress",        30.2672, -97.7431),
    ("6th street",      30.2672, -97.7388),
    ("lamar",           30.2950, -97.7545),
    ("mopac",           30.3500, -97.7690),
    ("i-35",            30.2672, -97.7306),
    ("airport",         30.1975, -97.6664),
    ("abia",            30.1975, -97.6664),
    ("domain",          30.4015, -97.7296),
    ("round rock",      30.5083, -97.6789),
    ("cedar park",      30.5052, -97.8203),
    ("pflugerville",    30.4394, -97.6200),
    ("bastrop",         30.1107, -97.3154),
    ("burnet",          30.7488, -98.2345),
    ("new braunfels",   29.7030, -98.1245),
    ("kerrville",       30.0474, -99.1403),
    ("manor",           30.3424, -97.5564),
    ("buda",            30.0849, -97.8403),
    ("kyle",            29.9891, -97.8772),
    ("bee cave",        30.3077, -97.9461),
    ("lakeway",         30.3577, -97.9772),
    ("cap metro",       30.2672, -97.7431),
    ("capital metro",   30.2672, -97.7431),
    ("bus",             30.2672, -97.7431),
    ("ut campus",       30.2849, -97.7341),
    ("university",      30.2849, -97.7341),
    ("townes terrace",  30.3566, -97.4930),  # Carillon subdivision, Manor TX
    ("thomas wheeler",  30.3566, -97.4930),  # Carillon subdivision, Manor TX
    ("carillon",        30.3566, -97.4930),  # Carillon subdivision, Manor TX
]


_geocode_cache: dict[str, tuple[float, float] | None] = {}
_geocode_lock  = threading.Lock()

# Rough bounding box for Austin/Travis County metro — reject geocodes outside this
_GEO_BOUNDS = (29.85, -98.25, 30.70, -97.25)  # (min_lat, min_lon, max_lat, max_lon)

# Regex patterns that suggest a real street address in the transcript
_ADDR_RE = re.compile(
    r'\b(\d{3,5})[,\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b'   # e.g. "2525 West Anderson Lane" or "15017, Stave Oak Lane"
    r'|'
    r'\b(\d{1,2}(?:st|nd|rd|th))\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'  # e.g. "15th and West"
    r'|'
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'  # e.g. "Porter and Vargas"
)


_nominatim_sem = threading.Semaphore(1)  # enforce 1 concurrent Nominatim call

def _geocode_load_db() -> None:
    """Warm in-memory cache from persistent DB at startup."""
    try:
        conn = sqlite3.connect(DB_PATH)
        for row in conn.execute("SELECT address_key, lat, lon FROM geocode_cache"):
            _geocode_cache[row[0]] = (row[1], row[2]) if row[1] is not None else None
        conn.close()
        print(f"[geocode] loaded {len(_geocode_cache)} cached entries from DB", flush=True)
    except Exception as exc:
        print(f"[geocode] DB warm-up failed: {exc}", flush=True)

def _geocode_save_db(key: str, lat: float | None, lon: float | None) -> None:
    """Persist a geocode result (or None miss) to calls.db."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO geocode_cache (address_key, lat, lon, ts_cached) "
            "VALUES (?, ?, ?, ?)",
            (key, lat, lon, time.time())
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[geocode] DB save failed for '{key}': {exc}", flush=True)

def _geocode_address(address: str) -> tuple[float, float] | None:
    """Geocode an address in Austin/Travis County TX. DB+memory cached. Returns (lat, lon) or None."""
    key = address.lower().strip()
    with _geocode_lock:
        if key in _geocode_cache:
            return _geocode_cache[key]
    
    # Nominatim: enforce 1 concurrent call + 1s minimum gap between requests
    with _nominatim_sem:
        # Double-check after acquiring semaphore — another thread may have just resolved it
        with _geocode_lock:
            if key in _geocode_cache:
                return _geocode_cache[key]
        try:
            geo = Nominatim(user_agent="battlebuddy/1.0")
            min_lat, min_lon, max_lat, max_lon = _GEO_BOUNDS
            for context in (f"{address}, Austin, TX", f"{address}, Travis County, TX", f"{address}, TX"):
                result = geo.geocode(context, timeout=4)
                if result:
                    lat, lon = result.latitude, result.longitude
                    if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                        with _geocode_lock:
                            _geocode_cache[key] = (lat, lon)
                        _geocode_save_db(key, lat, lon)
                        print(f"[geocode] '{address}' -> {lat:.4f},{lon:.4f} (via '{context}')", flush=True)
                        return lat, lon
            # Cache the miss so we don't re-query on restart
            with _geocode_lock:
                _geocode_cache[key] = None
            _geocode_save_db(key, None, None)
        except Exception as exc:
            print(f"[geocode] error for '{address}': {exc}", flush=True)
            with _geocode_lock:
                _geocode_cache[key] = None
            # Do NOT persist errors — transient network failures should be retried next restart
        time.sleep(1.0)  # Nominatim 1 req/s policy
    return None


def extract_location(text: str) -> tuple[float | None, float | None, str | None]:
    if not text:
        return None, None, None
    lower = text.lower()
    # 1. Fast keyword match against known landmarks/streets
    for keyword, lat, lon in LOCATION_HINTS:
        if keyword in lower:
            return lat, lon, keyword.title()
    # 2. Try to find and geocode a real street address in the transcript
    best_candidate = None
    for m in _ADDR_RE.finditer(text):
        candidate = m.group(0).strip()
        # Skip very short matches that are likely noise (e.g. beat codes like "Baker 40")
        if len(candidate) < 8:
            continue
        result = _geocode_address(candidate)
        if result:
            return result[0], result[1], candidate
        # Save first plausible address even if geocoding fails — at least we have the text
        if best_candidate is None:
            best_candidate = candidate
    # Return address text with no coordinates so it shows in incident description
    if best_candidate:
        return None, None, best_candidate
    return None, None, None


# ---------------------------------------------------------------------------
# Transcription — faster-whisper large-v3-turbo INT8 (local, offline-capable)
# 4-8x faster than openai-whisper, ~200MB RAM, works with no internet
# ---------------------------------------------------------------------------

from faster_whisper import WhisperModel as _FasterWhisperModel

_fw_model      = None
_fw_model_lock          = threading.Lock()  # serialises Whisper inference
_MAX_PROCESS_THREADS    = 20                # hard cap on concurrent process() threads
_BROADCASTIFY_MAX       = 15                # broadcastify can hold at most this many slots (reserves 5 for pi5)
_process_sem            = threading.Semaphore(_MAX_PROCESS_THREADS)
_broadcastify_sem       = threading.Semaphore(_BROADCASTIFY_MAX)

# ---------------------------------------------------------------------------
# /receive deduplication cache
# Keyed by SHA-256 of the raw WAV bytes.  Each entry stores the Unix timestamp
# at which the hash expires (default TTL: 60 s).  A second identical payload
# arriving within that window is silently dropped with status "duplicate".
# The cache is bounded to _DEDUP_MAX_ENTRIES entries; when full, stale entries
# are evicted before inserting a new one.  Protected by a threading.Lock.
# ---------------------------------------------------------------------------
_DEDUP_TTL_SECONDS   = 60          # seconds a hash is remembered
_DEDUP_MAX_ENTRIES   = 2048        # upper bound; prevents unbounded growth
_recv_seen_hashes: dict[str, float] = {}   # {sha256_hex: expiry_ts}
_recv_dedup_lock     = threading.Lock()

def _get_fw_model() -> _FasterWhisperModel:
    global _fw_model
    if _fw_model is None:
        print("[whisper] loading faster-whisper large-v3-turbo int8...", flush=True)
        _fw_model = _FasterWhisperModel("distil-large-v3", device="cpu", compute_type="int8",
                                        cpu_threads=2, num_workers=1)
        print("[whisper] model ready", flush=True)
    return _fw_model


def transcribe(wav_bytes: bytes) -> str:
    """Transcribe audio locally with faster-whisper. No external API needed."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp = f.name
    try:
        acquired = _fw_model_lock.acquire(timeout=90)
        if not acquired:
            print("[whisper] TIMEOUT waiting for model lock — dropping call", flush=True)
            return ""
        try:
            model = _get_fw_model()
            segments, _ = model.transcribe(tmp, language="en", beam_size=1,
                                           vad_filter=True)
            return " ".join(s.text for s in segments).strip()
        finally:
            _fw_model_lock.release()
    except Exception as e:
        print(f"[whisper] error: {e}", flush=True)
        return ""
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Groq LLM analysis — direct API call (no Pi relay needed on Contabo)
# ---------------------------------------------------------------------------

def _call_groq_llm(system_prompt: str, user_msg: str) -> dict:
    """Call Groq chat completions directly. Returns parsed JSON dict."""
    req = urllib.request.Request(
        f"{GROQ_API_BASE}/chat/completions",
        data=json.dumps({
            "model":           GROQ_MODEL,
            "messages":        [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            "max_tokens":      300,
            "temperature":     0.1,
            "response_format": {"type": "json_object"},
        }).encode(),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type":  "application/json",
            "User-Agent":    "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read())
    return json.loads(data["choices"][0]["message"]["content"])

_GROQ_SYSTEM = """You are the incident detection brain for Battle Buddy, a real-time P25 radio monitoring system covering Austin/Travis County emergency services on the GATRRS trunked system.

Austin agencies: APD (police), AFD (Austin Fire Dept), TCEMS (Travis County EMS), TCFD (Travis County Fire), ABIA (Austin-Bergstrom Airport), TCSO (Travis County Sheriff), DPS (Texas Dept of Public Safety), UTPD (UT Police).

Analyze the radio call transcript and recent context. Respond ONLY with a JSON object — no prose, no markdown.

Required JSON fields:
{
  "incident_type": <one of the values below, or null for routine>,
  "priority": <"HIGH" | "MED" | "NONE">,
  "should_hold": <true | false>,
  "description": <one concise sentence describing the event>,
  "escalation_stage": <"welfare"|"disturbance"|"pursuit"|"weapons"|"backup"|"tactical"|"k9"|"air" or null>,
  "reasoning": <one sentence explaining your decision>
}

Valid incident_type values:
  "OFFICER DOWN", "SHOOTING", "STABBING", "AIRCRAFT EMERGENCY", "MASS CASUALTY",
  "STRUCTURE FIRE", "HAZMAT", "HOSTAGE/BARRICADE", "CRASH/COLLISION", "FATAL CRASH",
  "FIRE DISPATCH", "TRANSIT INCIDENT", "AIRPORT EMERGENCY",
  "MULTI-AGENCY RESPONSE", "APD SURGE", "AIR ASSET ACTIVE", "DPS CAPITOL ACTIVATION",
  "DEATH INVESTIGATION"

Priority rules:
  HIGH — active life threat: officer down, shooting, structure fire, mass casualty, hostage/barricade, aircraft emergency
  NOTE: EMS/AFD reporting "GSW", "gunshot wound", or "gunshot victim" = SHOOTING even without APD confirmation.
  MED  — significant incident: crash, hazmat, fire dispatch, multi-agency, surge, transit, airport alert, death investigation
  NONE — routine: traffic stop, medical assist, disturbance, welfare check, normal patrol, minor fender bender

should_hold: true only if the event is actively unfolding and worth continuous monitoring.

Be conservative. Most radio traffic is routine. Only flag genuine emergencies."""


_groq_call_times: list  = []
_groq_rate_lock         = threading.Lock()
_groq_backoff_until     = 0.0          # epoch — skip all calls until this time after a 429
_GROQ_RATE_LIMIT        = 4            # max calls per 60 seconds — at ~1000 tok/call keeps TPM under 6000 free tier
_GROQ_MIN_TRANSCRIPT    = 50           # skip short/garbled clips (raised from 20)
_GROQ_MIN_DURATION      = 2.5          # skip clips too short for meaningful speech (seconds)
_GROQ_BACKOFF_SECS      = 600           # back off this long after a 429 (10 min clears Groq rate window)

# ROUTINE cooldown: after this many consecutive ROUTINEs from the same tgid within
# _GROQ_ROUTINE_WINDOW seconds, suppress Groq for that tgid for _GROQ_ROUTINE_COOLDOWN
# seconds — unless the transcript contains a safety keyword.
_GROQ_ROUTINE_STREAK    = 3
_GROQ_ROUTINE_WINDOW    = 600          # 10 minutes
_GROQ_ROUTINE_COOLDOWN  = 300          # 5 minutes
_GROQ_SAFETY_RE         = re.compile(
    r"shoot|shot|weapon|gun|stab|assault|pursuit|chase|crash|fire|smoke|explosion|"
    r"officer down|man down|unconscious|not breathing|overdose|hostage|threat|bomb",
    re.IGNORECASE
)
# tgid -> {"streak": int, "cooldown_until": float, "last_ts": float}
_groq_routine_tracker: dict = {}

def groq_analyze(call: dict, recent_calls: list) -> dict | None:
    """Call Groq LLM to analyze a call. Returns parsed JSON dict or None on failure."""
    global _groq_backoff_until, _groq_call_times
    if not GROQ_ENABLED:
        return None
    # Never analyze broadcastify (tgid=0) — no real talkgroup, wastes rate quota
    if call.get("tgid", 0) == 0:
        return None
    transcript = call.get("transcript") or ""
    if not transcript or len(transcript) < _GROQ_MIN_TRANSCRIPT:
        return None
    # Skip clips too short to contain meaningful speech
    if call.get("duration", 99.0) < _GROQ_MIN_DURATION:
        return None
    # ROUTINE cooldown: suppress known-quiet talkgroups unless safety keyword present
    tgid = call.get("tgid", 0)
    if not _GROQ_SAFETY_RE.search(transcript):
        now_pre = time.time()
        tracker = _groq_routine_tracker.get(tgid)
        if tracker and now_pre < tracker.get("cooldown_until", 0):
            return None
    now = time.time()
    # Thread-safe rate limiter + backoff check
    with _groq_rate_lock:
        if now < _groq_backoff_until:
            return None
        _groq_call_times[:] = [t for t in _groq_call_times if now - t < 60]
        if len(_groq_call_times) >= _GROQ_RATE_LIMIT:
            return None
        _groq_call_times.append(now)

    ctx_lines = []
    for rc in recent_calls[-6:]:
        rc_txt = (rc.get("transcript") or "")[:100]
        if rc_txt:
            ctx_lines.append(
                f"  [{rc.get('category','?')}] {rc.get('tag') or 'TGID '+str(rc.get('tgid','?'))}: {rc_txt}"
            )
    context_block = "\n".join(ctx_lines) if ctx_lines else "  (none)"

    user_msg = (
        f"Agency: {call.get('category','Unknown')}\n"
        f"Talkgroup: {call.get('tag') or 'TGID '+str(call.get('tgid',0))}\n"
        f"Location hint: {call.get('location') or 'unknown'}\n"
        f"Transcript: {transcript}\n\n"
        f"Recent calls (last 15 min):\n{context_block}"
    )

    try:
        result = _call_groq_llm(_GROQ_SYSTEM, user_msg)
        itype  = result.get("incident_type") or "ROUTINE"
        pri    = result.get("priority", "NONE")
        hold   = result.get("should_hold", False)
        reason = (result.get("reasoning") or "")[:100]
        print(f"[groq] {call.get('tag','?')} → {itype} pri={pri} hold={hold} | {reason}",
              flush=True)
        # Update ROUTINE streak tracker
        now_post = time.time()
        if itype in (None, "ROUTINE"):
            tr = _groq_routine_tracker.setdefault(tgid, {"streak": 0, "cooldown_until": 0.0, "last_ts": 0.0})
            if now_post - tr["last_ts"] < _GROQ_ROUTINE_WINDOW:
                tr["streak"] += 1
            else:
                tr["streak"] = 1
            tr["last_ts"] = now_post
            if tr["streak"] >= _GROQ_ROUTINE_STREAK:
                tr["cooldown_until"] = now_post + _GROQ_ROUTINE_COOLDOWN
                tr["streak"] = 0
        else:
            # Non-routine result — clear any cooldown so we stay attentive
            _groq_routine_tracker.pop(tgid, None)
        return result
    except Exception as exc:
        print(f"[groq] error: {exc}", flush=True)
        if "429" in str(exc):
            _groq_backoff_until = time.time() + _GROQ_BACKOFF_SECS
            print(f"[groq] rate limited — backing off {_GROQ_BACKOFF_SECS}s", flush=True)
        return None


# ---------------------------------------------------------------------------
# TGID auto-identification — guesses agency name for unknown talkgroups
# ---------------------------------------------------------------------------

_TGID_ID_SYSTEM = """You are a P25 radio talkgroup analyst for the GATRRS trunked system covering Austin, TX and Travis County.

You will receive a radio transcript from an UNKNOWN talkgroup and your job is to guess which Austin/Travis County public safety agency this talkgroup belongs to, and suggest a short name for it.

Known agencies on GATRRS:
- APD: Austin Police Department (patrol, ops, dispatch, detective, SWAT)
- AFD: Austin Fire Department (fire suppression, EMS first response, Locution dispatch)
- TCEMS: Travis County EMS (paramedic units, ambulances)
- TCFD: Travis County Fire/EMS (suburban fire districts)
- TCSO: Travis County Sheriff's Office (patrol, jail, civil)
- UTPD: UT Austin Police
- DPS: Texas Dept of Public Safety (troopers, Capitol Police)
- ABIA: Austin-Bergstrom International Airport operations
- Cap Metro: Capital Metro transit police/operations
- Williamson/Hays: neighboring county agencies
- City utilities: Austin Water, Austin Energy (not public safety)

Respond ONLY with a JSON object:
{
  "guess": <short name for this talkgroup, e.g. "APD South Patrol" or "TCEMS Medic Ops">,
  "agency": <top-level agency abbreviation, e.g. "APD">,
  "confidence": <"HIGH" | "MED" | "LOW">,
  "reasoning": <one sentence explaining your guess based on the radio chatter>
}

If the transcript is too short or garbled to make any guess, set confidence to "LOW" and guess to null."""

# Minimum transcript length to attempt TGID identification
_TGID_ID_MIN_LEN = 15

# How many agreeing guesses before auto-confirming
_TGID_ID_CONFIRM_THRESHOLD = 3


def groq_identify_tgid(tgid: int, transcript: str) -> dict | None:
    """Ask Groq to guess what agency/role this unknown talkgroup belongs to.
    Stores the guess in tgid_guesses. Auto-confirms after threshold agreeing guesses."""
    if not GROQ_ENABLED or not transcript or len(transcript) < _TGID_ID_MIN_LEN:
        return None

    prompt = (
        f"Unknown talkgroup TGID {tgid} on GATRRS Austin/Travis County.\n"
        f"Radio transcript: {transcript}\n\n"
        f"What agency and role does this talkgroup belong to?"
    )

    try:
        user_msg = (
            f"TGID {tgid} on GATRRS Austin/Travis County P25 system.\n"
            f"Radio transcript: {transcript}\n\n"
            f"What Austin/Travis County public safety agency and role does this talkgroup belong to?"
        )
        raw = _call_groq_llm(_TGID_ID_SYSTEM, user_msg)

        guess     = raw.get("guess")
        agency    = raw.get("agency")
        reasoning = raw.get("reasoning", "")
        # Normalize confidence — Groq may return a float (0.0-1.0) or string
        raw_conf = raw.get("confidence", "LOW")
        if isinstance(raw_conf, (int, float)):
            conf = "HIGH" if raw_conf >= 0.75 else ("MED" if raw_conf >= 0.5 else "LOW")
        else:
            conf = str(raw_conf).upper() if str(raw_conf).upper() in ("HIGH", "MED", "LOW") else "LOW"

        if not guess:
            return None

        ts = time.time()
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO tgid_guesses (tgid, ts, guess, category, confidence, reasoning, transcript) "
            "VALUES (?,?,?,?,?,?,?)",
            (tgid, ts, guess, agency, conf, reasoning, transcript[:200])
        )
        conn.commit()

        # Check how many HIGH/MED-confidence agreeing guesses we have
        if conf in ("HIGH", "MED"):
            rows = conn.execute(
                "SELECT guess FROM tgid_guesses WHERE tgid=? AND confirmed=0 AND confidence IN ('HIGH','MED')",
                (tgid,)
            ).fetchall()
            guesses = [r[0] for r in rows]

            # Find the most common guess
            if len(guesses) >= _TGID_ID_CONFIRM_THRESHOLD:
                from collections import Counter
                top_guess, top_count = Counter(guesses).most_common(1)[0]
                if top_count >= _TGID_ID_CONFIRM_THRESHOLD:
                    # Auto-confirm — mark all guesses for this tgid as confirmed
                    conn.execute(
                        "UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid,)
                    )
                    conn.commit()
                    print(f"[tgid-id] AUTO-CONFIRMED tgid={tgid} → {top_guess!r} "
                          f"({top_count} agreeing guesses)", flush=True)
                    _notify_tgid_confirmed(tgid, top_guess, agency, top_count)

        conn.close()
        print(f"[tgid-id] tgid={tgid} guess={guess!r} conf={conf}", flush=True)
        return raw

    except Exception as exc:
        print(f"[tgid-id] error for tgid={tgid}: {exc}", flush=True)
        return None


def _notify_tgid_confirmed(tgid: int, name: str, agency: str, count: int):
    """Post a Talk message when a TGID gets auto-confirmed."""
    msg = (
        f"🔍 **Unknown talkgroup identified!**\n"
        f"TGID {tgid} → **{name}** (agency: {agency or 'unknown'})\n"
        f"Auto-confirmed from {count} agreeing Groq guesses.\n"
        f"Review at /api/tgid_guesses — run `/addtag {tgid} {name}` to write to tags file."
    )
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    payload = urllib.parse.urlencode({"message": msg}).encode()
    req = urllib.request.Request(
        f"{TALK_BASE}/chat/{TALK_ROOMS['general']}",
        data=payload,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[tgid-id] notify failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Incident detection engine
# ---------------------------------------------------------------------------

# Ordered by priority — first match wins for a given call
INCIDENT_KEYWORDS = [
    ("officer down",   "OFFICER DOWN"),
    ("10-99",          "OFFICER DOWN"),
    ("shots fired",    "SHOOTING"),
    ("shooting",       "SHOOTING"),
    ("person shot",    "SHOOTING"),
    ("subject shot",   "SHOOTING"),
    ("victim shot",    "SHOOTING"),
    ("shot victim",    "SHOOTING"),
    ("homicide",       "SHOOTING"),
    ("found dead",     "SHOOTING"),
    ("body found",     "SHOOTING"),
    ("gsw",            "SHOOTING"),   # EMS: gunshot wound
    ("gunshot",        "SHOOTING"),   # EMS: gunshot wound
    ("gun shot",       "SHOOTING"),
    ("stabbing",       "STABBING"),
    (" stab",          "STABBING"),
    ("assault",         "STABBING"),   # locution CAD code for stabbing/cutting victim
    ("aircraft",       "AIRCRAFT EMERGENCY"),
    ("mass casualty",  "MASS CASUALTY"),
    ("mci",            "MASS CASUALTY"),
    ("cardiac arrest", "EMS DISPATCH"),
    ("multiple patients", "EMS DISPATCH"),
    ("trauma",         "EMS DISPATCH"),
    ("structure fire", "STRUCTURE FIRE"),
    ("working fire",   "STRUCTURE FIRE"),
    ("fully involved", "STRUCTURE FIRE"),
    ("hazmat",         "HAZMAT"),
    ("chemical spill", "HAZMAT"),
    ("hostage",        "HOSTAGE/BARRICADE"),
    ("barricade",      "HOSTAGE/BARRICADE"),
    # Fatal crash — longer phrases before generic "crash" so they match first
    ("fatal crash",    "FATAL CRASH"),
    ("fatal accident", "FATAL CRASH"),
    ("fatality",       "FATAL CRASH"),
    ("start a dts",    "FATAL CRASH"),   # Austin APD Deceased Traffic Scene protocol
    ("crash",          "CRASH/COLLISION"),
    ("collision",      "CRASH/COLLISION"),
    ("rollover",       "CRASH/COLLISION"),
    # Medical Examiner / death scene indicators
    ("medical examiner", "DEATH INVESTIGATION"),
    ("jp responding",  "DEATH INVESTIGATION"),
    ("justice of the peace", "DEATH INVESTIGATION"),
    ("pronounce",      "DEATH INVESTIGATION"),
    ("pronounced at",  "DEATH INVESTIGATION"),
    ("death investigation", "DEATH INVESTIGATION"),
    ("signal 48",      "DEATH INVESTIGATION"),   # Texas LE code for death
]

# ---------------------------------------------------------------------------
# Escalation chain detection
# ---------------------------------------------------------------------------

# Ordered escalation stages — higher index = more serious
ESCALATION_STAGES = [
    ("welfare",      ["welfare check", "well-being check", "wbc", "check on subject"]),
    ("disturbance",  ["disturbance", "domestic", "fight", "altercation", "argument"]),
    ("pursuit",      ["pursuit", "foot chase", "fleeing", "chase"]),
    ("weapons",      ["weapon", "armed", "firearm", "gun", "knife", "rifle"]),
    ("backup",       ["need backup", "requesting backup", "all units", "code 3", "lights and sirens"]),
    ("tactical",     ["swat", "tac team", "tactical", "negotiat", "standoff", "barricaded"]),
    ("k9",           ["k-9", "k9", "canine", "dog track", "dog unit"]),
    ("air",          ["air1", "air 1", "helicopter", "aviation", "bird in the air"]),
]

ESCALATION_STAGE_NAMES = [s[0] for s in ESCALATION_STAGES]

# Severity ordering for itype upgrade decisions.
# Higher value = more urgent. _update_incident upgrades stored itype when new > current.
ITYPE_SEVERITY: dict[str, int] = {
    "CRASH/COLLISION":         1,
    "PEDESTRIAN INCIDENT":     2,
    "FIRE DISPATCH":           2,
    "TRANSIT INCIDENT":        2,
    "DEATH INVESTIGATION":     3,
    "FATAL CRASH":             4,
    "SHOOTING":                5,
    "STABBING":                5,
    "WEAPONS":                 5,
    "STRUCTURE FIRE":          5,
    "HAZMAT":                  5,
    "OFFICER DOWN":            6,
    "MASS CASUALTY":           6,
    "HOSTAGE/BARRICADE":       6,
    "AIRCRAFT EMERGENCY":      6,
}

# Compatible itype groups — incidents of these types merge even if itype differs.
# Key: new itype being detected. Value: set of existing itypes it can merge into.
ITYPE_MERGE_COMPAT: dict[str, set] = {
    "FATAL CRASH":         {"CRASH/COLLISION", "PEDESTRIAN INCIDENT", "DEATH INVESTIGATION"},
    "CRASH/COLLISION":     {"FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "PEDESTRIAN INCIDENT": {"CRASH/COLLISION", "FATAL CRASH"},
    "DEATH INVESTIGATION": {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
}

# ---------------------------------------------------------------------------
# TGID tier map — higher tier = more tactically specific channel.
# When escalation stage rises, _consider_hold switches to higher-tier TGIDs.
# Tier 0 = non-public-safety (water, parking, energy) — never hold for crime.
# Tier 1 = dispatch (initial report + coordinator traffic).
# Tier 2 = metro/field (unit coordination, pursuits).
# Tier 3 = tactical (SWAT, K9, air operations).
# ---------------------------------------------------------------------------
TGID_TIER: dict[int, int] = {
    **{tgid: 1 for tgid in range(960, 970)},   # APD Dispatch 1-10
    **{tgid: 2 for tgid in range(972, 988)},   # APD Metro 1-16
    **{tgid: 3 for tgid in [1000, 1001, 1002]}, # APD TAC 1-3
    1121: 1, 1122: 1,                            # AFD Dispatch 1-2
    1155: 2,                                     # AFD TAC
    1162: 1,                                     # TCFD Locution
    1371: 1, 1377: 1, 1378: 1,                  # AFD zonal (East/North/South)
    1471: 1, 1472: 2, 1473: 2,                  # ABIA Ops / Security / Fire
    1474: 2, 1480: 3, 1481: 3,                  # ABIA Police / Emerg / Alert
    989: 2,                                      # APD Air/K9
    **{tgid: 2 for tgid in range(1020, 1027)},  # APD Narc 1-7
    1274: 2,                                     # TCEMS SWAT
    2409: 3, 2410: 3,                            # TCSO SWAT 1-2
    5291: 2, 5292: 2,                            # Austin/Travis Interop 1-2
}

# Minimum tier required for each escalation stage.
# When a new clip arrives on a tgid whose tier >= this AND > current hold tier,
# _consider_hold switches the hold to follow the incident.
ESCALATION_MIN_TIER: dict[str, int] = {
    "welfare":     1,  # any dispatch is fine
    "disturbance": 1,
    "weapons":     1,
    "pursuit":     2,  # need metro — units are moving
    "backup":      2,
    "k9":          2,
    "tactical":    3,  # need TAC — SWAT/negotiators on scene
    "air":         3,
}

# Location match radius — calls within this many km link to an existing incident
INCIDENT_LOCATION_RADIUS_KM = 0.5


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km between two lat/lon points."""
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def _detect_escalation_stage(text: str) -> str | None:
    """Return the highest escalation stage keyword found in transcript."""
    text = text.lower()
    matched = None
    for stage, keywords in ESCALATION_STAGES:
        if any(kw in text for kw in keywords):
            matched = stage
    return matched  # returns highest-indexed match since we iterate in order


def _find_incident_by_location(lat: float, lon: float, ts: float) -> int | None:
    """Return active incident ID if a recent incident is within radius of this call."""
    if lat is None or lon is None:
        return None
    with _incident_lock:
        for inc_id, inc in _active_incidents.items():
            if inc.get("lat") is None or inc.get("lon") is None:
                continue
            if (ts - inc["ts_updated"]) > MULTIAGENCY_WINDOW_MIN * 60:
                continue
            dist = _haversine_km(lat, lon, inc["lat"], inc["lon"])
            if dist <= INCIDENT_LOCATION_RADIUS_KM:
                return inc_id
    return None


def _record_escalation(incident_id: int, stage: str, description: str, ts: float):
    """Store an escalation step and alert if the stage is higher than last recorded."""
    inc = _active_incidents.get(incident_id)
    if inc is None:
        return
    last_stage = inc.get("escalation_stage")
    last_idx   = ESCALATION_STAGE_NAMES.index(last_stage) if last_stage in ESCALATION_STAGE_NAMES else -1
    new_idx    = ESCALATION_STAGE_NAMES.index(stage)      if stage in ESCALATION_STAGE_NAMES else -1

    if new_idx <= last_idx:
        return  # not an escalation

    inc["escalation_stage"] = stage
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO incident_escalations (incident_id, ts, stage, description) VALUES (?,?,?,?)",
        (incident_id, ts, stage, description)
    )
    conn.commit()
    conn.close()

    # Build escalation chain narrative from DB
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ts, stage FROM incident_escalations WHERE incident_id=? ORDER BY ts",
        (incident_id,)
    ).fetchall()
    conn.close()
    chain = " → ".join(r[1].upper() for r in rows)

    itype    = inc["itype"]
    location = inc.get("location", "unknown location")
    msg = (f"🔺 ESCALATION: {itype} @ {location}\n"
           f"Chain: {chain}\n"
           f"Latest: {description}")
    print(f"[escalation] {msg}", flush=True)

    agencies_str = ", ".join(sorted(x for x in inc["agencies"] if x))
    cat = next(iter(inc["agencies"]), "APD")
    threading.Thread(target=send_dm_alert,
                     args=(itype, msg, location, agencies_str, cat), daemon=True).start()
    # Post escalation to incidents room
    threading.Thread(target=_post_escalation_to_talk,
                     args=(itype, location, chain, description, inc.get("room_tokens", [])),
                     daemon=True).start()


def _post_escalation_to_talk(itype: str, location: str, chain: str, latest: str, extra_rooms: list):
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    msg = (f"🔺 **ESCALATION — {itype}** @ {location}\n"
           f"**Chain:** {chain}\n"
           f"**Latest:** {latest}")
    for room in set([TALK_ROOMS["incidents"]] + extra_rooms):
        payload = urllib.parse.urlencode({"message": msg}).encode()
        req = urllib.request.Request(
            f"{TALK_BASE}/chat/{room}", data=payload,
            headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[escalation] post failed → {room}: {e}", flush=True)


# ---------------------------------------------------------------------------
# In-memory active incident state and lock are now owned by modules/incident_engine.py
# and imported at the top of this file.  _atak_markers remains here because
# it is used only by the ATAK CoT functions below.
# ---------------------------------------------------------------------------
_atak_markers: dict[int, str] = {}  # incident_id -> FTS uid, for deletion on clear

# analyze_for_incident is imported from modules.incident_engine at the top of
# this file.  The implementation has been fully ported there.
# See: modules/incident_engine.py :: analyze_for_incident()


# ---------------------------------------------------------------------------
# Persistent FTS connection — Battle Buddy connects once as a TAK client and
# keeps the socket open.  CoT sent over this connection is broadcast by FTS
# to all other connected clients (WinTAK, ATAK, etc.).
# ---------------------------------------------------------------------------
import ssl as _ssl_mod
import socket as _sock_mod

_fts_lock   = threading.Lock()
_fts_socket = None          # the live SSL socket, or None

_BB_SA_UID  = "BATTLEBUDDY-SERVER"
_BB_SA_XML  = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<event version='2.0' uid='{uid}' type='t-x-c-t' "
    "time='{t}' start='{t}' stale='{s}' how='m-g'>"
    "<point lat='0.0' lon='0.0' hae='0.0' ce='9999999.0' le='9999999.0'/>"
    "<detail>"
    "<contact callsign='BattleBuddy'/>"
    "<remarks>Austin P25 AI Monitor</remarks>"
    "</detail>"
    "</event>"
)

def _fts_build_ctx():
    ctx = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations("/opt/battlebuddy/certs/ca.pem")
    ctx.load_cert_chain("/opt/battlebuddy/certs/client.pem",
                        "/opt/battlebuddy/certs/client.key")
    ctx.check_hostname = False
    return ctx

def _fts_connect():
    """Open a fresh persistent SSL connection to FTS. Called under _fts_lock."""
    global _fts_socket
    try:
        if _fts_socket:
            try: _fts_socket.close()
            except Exception: pass
            _fts_socket = None
        raw = _sock_mod.create_connection((FTS_HOST, FTS_COT_PORT), timeout=10)
        _fts_socket = _fts_build_ctx().wrap_socket(raw)
        # Send our SA announcement so FTS registers us as a proper client
        now_dt  = datetime.now(timezone.utc)
        stale_dt = now_dt + __import__('datetime').timedelta(minutes=10)
        fmt = "%Y-%m-%dT%H:%M:%S.0Z"
        sa = _BB_SA_XML.format(uid=_BB_SA_UID,
                               t=now_dt.strftime(fmt),
                               s=stale_dt.strftime(fmt))
        _fts_socket.sendall(sa.encode("utf-8"))
        print("[atak] persistent connection established to FTS", flush=True)
    except Exception as exc:
        _fts_socket = None
        print(f"[atak] connect failed: {exc}", flush=True)

def _atak_send_cot(xml: str):
    """Send a CoT XML string over the persistent FTS connection, reconnecting if needed."""
    global _fts_socket
    if not FTS_ENABLED:
        return
    for attempt in range(2):
        with _fts_lock:
            if _fts_socket is None:
                _fts_connect()
            if _fts_socket is None:
                raise ConnectionError("FTS unreachable")
            try:
                _fts_socket.sendall(xml.encode("utf-8"))
                return
            except Exception as exc:
                print(f"[atak] send error (attempt {attempt+1}): {exc} — reconnecting", flush=True)
                _fts_socket = None
                if attempt == 0:
                    _fts_connect()

def _fts_keepalive_thread():
    """Send a SA heartbeat every 30 s to keep the FTS connection alive."""
    while True:
        time.sleep(30)
        try:
            now_dt   = datetime.now(timezone.utc)
            stale_dt = now_dt + __import__('datetime').timedelta(minutes=2)
            fmt = "%Y-%m-%dT%H:%M:%S.0Z"
            sa = _BB_SA_XML.format(uid=_BB_SA_UID,
                                   t=now_dt.strftime(fmt),
                                   s=stale_dt.strftime(fmt))
            _atak_send_cot(sa)
        except Exception as exc:
            print(f"[atak] keepalive error: {exc}", flush=True)


def _atak_resync_thread():
    """Re-post all active incident markers every 5 minutes.
    Ensures clients that connect after initial post receive current state."""
    while True:
        time.sleep(300)
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, itype, lat, lon, location, description FROM incidents"
                " WHERE status='active' AND location IS NOT NULL AND location != ''"
                " AND lat IS NOT NULL AND lon IS NOT NULL AND is_test=0"
            ).fetchall()
            conn.close()
            count = 0
            for row in rows:
                threading.Thread(
                    target=_atak_post_marker,
                    args=(row['id'], row['lat'], row['lon'], row['itype'],
                          row['location'], row['description']),
                    daemon=True
                ).start()
                count += 1
            if count:
                print(f"[atak] resync: re-posted {count} active marker(s)", flush=True)
        except Exception as exc:
            print(f"[atak] resync error: {exc}", flush=True)




# CoT type codes and colors per incident type
# type format: a-h-G (attitude-hostility-dimension)
# ARGB color as signed int: 0xFFRRGGBB cast to int32
# iconsetpath constants
_FEMA_FIRE_ICON      = "f8f7f666-8b28-4b57-9fbb-e48e61d33b79/Iron Sites/Fire Incident.png"
_GEOOPS_FIRE_ICON    = "83198b4872a8c34eb9c549da8a4de5a28f07821185b39a2277948f66c24ac17a/WildFire/Fire Location.png"
_RESPONDER_EMS_ICON  = "de450cbf-2ffc-47fb-bd2b-ba2db89b035e/Incident/EMS--Plain.png"
_HELO_EMS_ICON       = "de450cbf-2ffc-47fb-bd2b-ba2db89b035e/Incident/EMS--Plain.png"

_COT_PROFILE = {
    # itype                 cot_type         argb_color    stale_min  iconsetpath (None = default shape)
    "SHOOTING":           ("a-h-G",          -65536,       60,  None),   # red
    "STABBING":           ("a-h-G",          -65536,       60,  None),   # red
    "OFFICER DOWN":       ("a-h-G",          -65536,       60,  None),   # red
    "PURSUIT":            ("a-h-G-V",        -23296,       45,  None),   # orange, hostile vehicle
    "WEAPONS":            ("a-h-G",          -23296,       45,  None),   # orange
    "STRUCTURE FIRE":     ("a-n-G",          -14336,       60,  _FEMA_FIRE_ICON),
    "FIRE DISPATCH":      ("a-n-G",          -14336,       45,  _FEMA_FIRE_ICON),
    "FIRE ALARM":         ("a-n-G",          -256,         30,  _FEMA_FIRE_ICON),
    "FIRE/EMS DISPATCH":  ("a-n-G",          -14336,       45,  _FEMA_FIRE_ICON),
    "GRASS FIRE":         ("a-n-G",          -23296,       60,  _GEOOPS_FIRE_ICON),
    "CRASH/COLLISION":    ("b-m-p-s-p-i",    -23296,       30,  None),   # orange, incident point
    "FATAL CRASH":        ("a-h-G",           -65536,       60,  None),   # red, fatal incident
    "MASS CASUALTY":      ("a-h-G",          -65536,       90,  _RESPONDER_EMS_ICON),
    "EMS DISPATCH":        ("a-n-G",          -16711936,    45,  _RESPONDER_EMS_ICON),  # green, EMS
    "HAZMAT":             ("b-m-p-s-m",      -16711681,    60,  None),   # green, hazmat
    "AIR ASSET ACTIVE":   ("a-f-A-M-H-R",    -16776961,    30,  None),   # blue, rotary wing
    "AIR ASSET EMS":      ("a-f-A-C-H-R",    -16711936,    30,  _HELO_EMS_ICON),  # green civilian rotary
    "AIR ASSET ORBIT":    ("a-h-A-M-H-R",    -65536,       45,  None),            # red hostile rotary
    "DPS CAPITOL ACTIVATION": ("a-h-G",      -65536,       60,  None),   # red
    "MULTI-AGENCY RESPONSE":  ("a-h-G",      -65536,       60,  None),   # red
    # Traffic incident types
    "FLOODING":           ("b-m-p-s-p-i",  -16776961,    45,  None),   # blue
    "ROAD HAZARD":        ("b-m-p-s-p-i",  -23296,       30,  None),   # orange
    "PEDESTRIAN INCIDENT": ("a-h-G",        -65536,       45,  None),   # red, person involved
    "VEHICLE FIRE":       ("a-n-G",         -23296,       45,  _FEMA_FIRE_ICON),  # orange, fire icon
    "TRAFFIC SIGNAL ISSUE": ("b-m-p-s-p-i", -8355712,    20,  None),   # grey, low priority
    "STALLED VEHICLE":    ("b-m-p-s-p-i",  -8355712,     20,  None),   # grey, low priority
    "ABANDONED VEHICLE":  ("b-m-p-s-p-i",  -8355712,     20,  None),   # grey, low priority
    "TRAFFIC INCIDENT":   ("b-m-p-s-p-i",  -8355712,     20,  None),   # grey, catch-all
}
_COT_DEFAULT = ("b-m-p-s-p-i", -8355712, 30, None)  # grey incident point


def _atak_post_marker(incident_id: int, lat: float, lon: float, itype: str,
                      location: str | None, description: str | None = None):
    """Post a CoT marker to FTS port 8087 with proper type, color, and stale time."""
    if not FTS_ENABLED:
        return
    from datetime import timedelta
    cot_type, argb, stale_min, iconsetpath = _COT_PROFILE.get(itype, _COT_DEFAULT)
    uid      = f"BB-INC-{incident_id}"
    callsign = (f"{itype} @ {location}" if location else itype)[:60]
    remarks  = (description or callsign)[:200]
    now_dt   = datetime.now(timezone.utc)
    stale_dt = now_dt + timedelta(minutes=stale_min)
    fmt      = "%Y-%m-%dT%H:%M:%S.0Z"
    now      = now_dt.strftime(fmt)
    stale    = stale_dt.strftime(fmt)
    # ce/le: use 50m if we have a real address, 500m if approximate
    ce_le    = "50.0" if location else "500.0"
    usericon = (f"<usericon iconsetpath='{iconsetpath}'/>" if iconsetpath else '')
    xml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<event version='2.0' uid='{uid}' type='{cot_type}' "
        f"time='{now}' start='{now}' stale='{stale}' how='m-g'>"
        f"<point lat='{lat}' lon='{lon}' hae='0.0' ce='{ce_le}' le='{ce_le}'/>"
        f"<detail>"
        f"<contact callsign='{callsign}'/>"
        f"<color argb='{argb}'/>"
        f"{usericon}"
        f"<remarks>{remarks}</remarks>"
        f"</detail>"
        f"</event>"
    )
    try:
        _atak_send_cot(xml)
        _atak_markers[incident_id] = uid
        print(f"[atak] marker posted: {itype} id={incident_id} uid={uid}", flush=True)
    except Exception as exc:
        print(f"[atak] post_marker error: {exc}", flush=True)


def _atak_clear_marker(incident_id: int):
    """Delete the ATAK marker via CoT t-x-d-d when an incident is cleared."""
    uid = _atak_markers.pop(incident_id, None)
    if not uid or not FTS_ENABLED:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0Z")
    xml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<event version='2.0' uid='{uid}' type='t-x-d-d' "
        f"time='{now}' start='{now}' stale='{now}' how='m-g'>"
        f"<point lat='0.0' lon='0.0' hae='0.0' ce='9999999.0' le='9999999.0'/>"
        f"<detail/></event>"
    )
    try:
        _atak_send_cot(xml)
        print(f"[atak] marker cleared: id={incident_id} uid={uid}", flush=True)
    except Exception as exc:
        print(f"[atak] clear_marker error: {exc}", flush=True)


def _create_incident(itype: str, desc: str, call: dict, ts: float):
    cat    = call.get("category", "Unknown")
    tgid   = call.get("tgid")
    agencies = json.dumps([cat])
    tgids    = json.dumps([tgid])
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.execute(
        "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, "
        "location, lat, lon, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
        (ts, ts, itype, desc, agencies, tgids,
         call.get("location"), call.get("lat"), call.get("lon"))
    )
    inc_id = cur.lastrowid
    conn.commit()
    conn.close()
    _active_incidents[inc_id] = {
        "itype":            itype,
        "ts_updated":       ts,
        "agencies":         {cat},
        "tgids":            {tgid},
        "lat":              call.get("lat"),
        "lon":              call.get("lon"),
        "location":         call.get("location"),
        "escalation_stage": None,
        "room_tokens":      [],
    }
    print(f"[incident] NEW  {itype}: {desc}", flush=True)
    agencies_str = ", ".join(json.loads(agencies))
    location     = call.get("location")
    inc_data = dict(itype=itype, description=desc, agencies=agencies,
                    location=location, ts_start=ts)
    threading.Thread(target=create_deck_card, args=(inc_data,), daemon=True).start()
    threading.Thread(target=send_dm_alert,    args=(itype, desc, location, agencies_str, cat), daemon=True).start()
    threading.Thread(target=post_banner,      args=(itype, location, agencies_str), daemon=True).start()
    if call.get("location") and call.get("lat") is not None and call.get("lon") is not None:
        threading.Thread(target=_atak_post_marker,
                         args=(inc_id, call["lat"], call["lon"], itype, call.get("location"), desc),
                         daemon=True).start()
    if call.get("lat") is not None and call.get("lon") is not None:
        threading.Thread(target=_check_commute_alerts,
                         args=(inc_id, itype, call["lat"], call["lon"], desc),
                         daemon=True).start()


def _update_incident(inc_id: int, call: dict, ts: float, desc: str, new_itype: str | None = None):
    inc = _active_incidents[inc_id]
    inc["ts_updated"] = ts
    inc["agencies"].add(call.get("category"))
    inc["tgids"].add(call.get("tgid"))
    agencies = json.dumps(sorted(x for x in inc["agencies"] if x))
    tgids    = json.dumps(sorted(x for x in inc["tgids"]    if x is not None))
    # Upgrade itype if incoming event is more severe than current
    stored_itype = inc["itype"]
    if new_itype and ITYPE_SEVERITY.get(new_itype, 0) > ITYPE_SEVERITY.get(stored_itype, 0):
        print(f"[incident] UPGRADE id={inc_id}: {stored_itype} → {new_itype}", flush=True)
        inc["itype"] = new_itype
        stored_itype = new_itype
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE incidents SET ts_updated=?, agencies=?, tgids=?, description=?, itype=? WHERE id=?",
        (ts, agencies, tgids, desc, stored_itype, inc_id)
    )
    conn.commit()
    conn.close()
    print(f"[incident] UPD  {stored_itype} (id={inc_id}): {desc}", flush=True)


def incident_cleanup_thread():
    """Mark incidents as cleared when they've had no updates for their type's timeout."""
    while True:
        time.sleep(60)
        now = time.time()
        with _incident_lock:
            to_clear = [
                iid for iid, inc in _active_incidents.items()
                if now - inc["ts_updated"] >
                   INCIDENT_TIMEOUT_MINUTES.get(inc["itype"], _INCIDENT_TIMEOUT_DEFAULT) * 60
            ]
            for iid in to_clear:
                itype = _active_incidents[iid]["itype"]
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "UPDATE incidents SET status='cleared', ts_cleared=? WHERE id=?",
                    (time.time(), iid)
                )
                conn.commit()
                conn.close()
                del _active_incidents[iid]
                print(f"[incident] CLEAR {itype} (id={iid}) — no activity", flush=True)
                threading.Thread(target=clear_banner,        args=(itype,),    daemon=True).start()
                threading.Thread(target=_atak_clear_marker,  args=(iid,),      daemon=True).start()


# ---------------------------------------------------------------------------
# OP25 hold / skip control
# ---------------------------------------------------------------------------

_current_hold_tgid: int | None = None
_last_hold_activity: float = 0.0
_hold_lock = threading.Lock()


def _consider_hold(tgid: int, itype: str, escalation_stage: str | None = None):
    """Decide whether to hold or switch hold to tgid, using tier-based escalation logic.

    Tier rules:
    - No current hold → hold tgid immediately.
    - Same tgid already held → refresh activity timestamp only.
    - Different tgid → switch only if new tgid's tier is >= the escalation minimum
      AND strictly higher than the current hold's tier (follow the incident up the
      chain: dispatch → metro → tactical).
    - Broadcastify clips (tgid == 0) never drive a hold switch.
    """
    global _current_hold_tgid, _last_hold_activity
    if tgid == 0:
        return  # Broadcastify mixed stream — no TGID data, skip
    with _hold_lock:
        if _current_hold_tgid is None:
            _send_hold(tgid)
            return
        if _current_hold_tgid == tgid:
            _last_hold_activity = time.time()
            return
        if escalation_stage:
            min_tier = ESCALATION_MIN_TIER.get(escalation_stage, 1)
            new_tier = TGID_TIER.get(tgid, 0)
            cur_tier = TGID_TIER.get(_current_hold_tgid, 0)
            if new_tier >= min_tier and new_tier > cur_tier:
                print(f"[hold] ESCALATION {escalation_stage}: tier {cur_tier} TGID {_current_hold_tgid}"
                      f" → tier {new_tier} TGID {tgid}", flush=True)
                _send_hold(tgid)
                return
        # No upgrade warranted — keep current hold but refresh activity
        _last_hold_activity = time.time()


def _send_hold(tgid: int):
    global _current_hold_tgid, _last_hold_activity
    payload = json.dumps([{"command": "hold", "arg1": tgid, "arg2": 0}]).encode()
    try:
        req = urllib.request.Request(
            PI1_OP25_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        _current_hold_tgid = tgid
        _last_hold_activity = time.time()
        print(f"[hold] HOLD  TGID {tgid}", flush=True)
    except Exception as e:
        print(f"[hold] FAILED to hold TGID {tgid}: {e}", flush=True)


def _send_skip():
    global _current_hold_tgid
    payload = json.dumps([{"command": "skip", "arg1": 0, "arg2": 0}]).encode()
    try:
        req = urllib.request.Request(
            PI1_OP25_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        prev = _current_hold_tgid
        _current_hold_tgid = None
        print(f"[hold] SKIP  (released TGID {prev})", flush=True)
    except Exception as e:
        print(f"[hold] FAILED to release: {e}", flush=True)


def hold_watchdog_thread():
    """Release hold automatically when the held channel goes quiet."""
    while True:
        time.sleep(30)
        with _hold_lock:
            if (_current_hold_tgid is not None and
                    time.time() - _last_hold_activity > HOLD_RELEASE_MINUTES * 60):
                print(f"[hold] watchdog: releasing TGID {_current_hold_tgid} (timeout)", flush=True)
                if HOLD_ENABLED:
                    _send_skip()


# ---------------------------------------------------------------------------
# Pi / OP25 watchdog
# ---------------------------------------------------------------------------

PI_WATCHDOG_INTERVAL   = 60    # check every 60 seconds
PI_CALL_SILENCE_MINS   = 20     # first alert after 20 min silence
PI_ALERT_REPEAT_MINS   = 20    # repeat alert every 20 min while still down
PI_AUTORESTART_MINS    = 30    # SSH-restart OP25 if feed silent this long
PI_ALERT_USERS         = ["kevin"]  # Talk usernames to DM on outage
PI1_OP25_CMD_URL       = "http://radiodesk.ddns.net:8080/"  # OP25 command endpoint
PI1_SSH_HOST           = "radiodesk.ddns.net"
PI1_SSH_USER           = "pi"
PI1_SSH_KEY            = "/root/.ssh/id_ed25519"

_pi_was_down         = False
_op25_was_dead       = False
_op25_fail_count     = 0      # consecutive trunk-poll failures
_pi_fail_count       = 0      # consecutive Pi-unreachable failures
_calls_were_silent   = False
_last_call_ts        = time.time()   # updated on every received call
_last_silence_alert  = 0.0
_silence_alert_count = 0
_last_autorestart_ts = 0.0
_pi_command_queue    = []            # commands for Pi to fetch and execute


def _send_email_alert(subject: str, body: str):
    """Send an email alert via Mailgun."""
    try:
        creds = base64.b64encode(f"api:{MAILGUN_API_KEY}".encode()).decode()
        payload = urllib.parse.urlencode({
            "from":    MAILGUN_FROM,
            "to":      ALERT_EMAIL,
            "subject": subject,
            "text":    body,
        }).encode()
        req = urllib.request.Request(
            f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
            data=payload,
            headers={"Authorization": f"Basic {creds}"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=15)
        print(f"[email] sent to {ALERT_EMAIL}: {subject}", flush=True)
    except Exception as e:
        print(f"[email] failed: {e}", flush=True)


def _pi_watchdog_alert(msg: str):
    """Send a DM alert to watchdog users. Retries up to 5 times with backoff.
    Also sends email as a parallel channel."""
    print(f"[watchdog] ALERT: {msg}", flush=True)
    # Email runs in parallel — doesn't block Talk retries
    threading.Thread(target=_send_email_alert, args=(f"Battle Buddy: {msg[:60]}", msg), daemon=True).start()
    for username in PI_ALERT_USERS:
        sent = False
        for attempt in range(5):
            token = _get_or_create_dm_room(username)
            if not token:
                time.sleep(5)
                continue
            creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
            payload = urllib.parse.urlencode({"message": msg}).encode()
            req = urllib.request.Request(
                f"{TALK_BASE}/chat/{token}",
                data=payload,
                headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                         "Content-Type": "application/x-www-form-urlencoded"},
                method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                print(f"[watchdog] DM sent to {username}: {msg}", flush=True)
                sent = True
                break
            except Exception as e:
                print(f"[watchdog] DM attempt {attempt+1} failed for {username}: {e}", flush=True)
                time.sleep(10 * (attempt + 1))
        if not sent:
            print(f"[watchdog] CRITICAL: could not deliver alert to {username} after 5 attempts", flush=True)


def _pi_autorestart_op25():
    """Restart OP25 (op25-multi_rx) and call_recorder on Pi via SSH, with command queue fallback."""
    global _last_autorestart_ts
    now = time.time()
    if now - _last_autorestart_ts < 300:  # don't restart more than once per 5 min
        return
    _last_autorestart_ts = now
    print("[watchdog] AUTO-RESTART: SSHing to Pi to restart op25-multi_rx + call_recorder...", flush=True)
    cmd = (
        "sudo systemctl restart op25-multi_rx && sleep 5 && "
        "systemctl --user restart call_recorder && "
        "echo restarted"
    )
    try:
        result = subprocess.run(
            ["ssh", "-i", PI1_SSH_KEY,
             "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10",
             "-o", "BatchMode=yes",
             f"{PI1_SSH_USER}@{PI1_SSH_HOST}", cmd],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and "restarted" in result.stdout:
            print("[watchdog] AUTO-RESTART: SSH success — op25-multi_rx + call_recorder restarted", flush=True)
            _pi_watchdog_alert("🔄 BATTLE BUDDY: Auto-restarted OP25 + call_recorder via SSH — monitoring for recovery.")
        else:
            raise RuntimeError(result.stderr.strip() or f"rc={result.returncode}")
    except Exception as e:
        print(f"[watchdog] AUTO-RESTART: SSH failed ({e}), queuing command for Pi poller", flush=True)
        _pi_command_queue.append({"cmd": "restart_op25", "ts": now})
        _pi_watchdog_alert("🔄 BATTLE BUDDY: Queued OP25 restart — Pi will execute within 60s.")


def _poll_op25_trunk() -> bool:
    """Return True if OP25 responds with a trunk_update — confirms active decoding."""
    try:
        cmd = json.dumps([{"command": "update", "arg1": 0, "arg2": 0}]).encode()
        req = urllib.request.Request(
            PI1_OP25_CMD_URL, data=cmd,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return any(m.get("json_type") == "trunk_update" for m in resp)
    except Exception:
        return False


def pi_watchdog_thread():
    global _pi_was_down, _op25_was_dead, _calls_were_silent, _last_call_ts
    global _op25_fail_count, _pi_fail_count
    global _last_silence_alert, _silence_alert_count, _last_autorestart_ts
    while True:
        time.sleep(PI_WATCHDOG_INTERVAL)

        # --- Check 1: Pi HTTP reachable ---
        pi_up = False
        try:
            urllib.request.urlopen(PI1_OP25_URL, timeout=10)
            pi_up = True
        except Exception:
            pass

        if not pi_up:
            _pi_fail_count += 1
            if _pi_fail_count >= 2 and not _pi_was_down:
                _pi_was_down = True
                _pi_watchdog_alert(
                    f"⚠️ BATTLE BUDDY ALERT: Pi 1 (OP25) is UNREACHABLE at {PI1_OP25_URL} — radio feed is down."
                )
        else:
            _pi_fail_count = 0
            if _pi_was_down:
                _pi_was_down = False
                _pi_watchdog_alert("✅ Pi 1 (OP25) is back online — radio feed restored.")

        # --- Check 2: OP25 actively decoding (trunk_update) ---
        if pi_up:
            op25_active = _poll_op25_trunk()
            if not op25_active:
                _op25_fail_count += 1
                if _op25_fail_count >= 3 and not _op25_was_dead:
                    _op25_was_dead = True
                    _pi_watchdog_alert(
                        "⚠️ BATTLE BUDDY ALERT: Pi is up but OP25 is NOT returning trunk data — decoder may have crashed or lost the control channel."
                    )
            else:
                _op25_fail_count = 0
                if _op25_was_dead:
                    _op25_was_dead = False
                    _pi_watchdog_alert("✅ OP25 trunk decoder is active again — feed restored.")

        # --- Check 3: Calls received recently (repeat alerts + auto-restart) ---
        silence_secs = time.time() - _last_call_ts
        if silence_secs > PI_CALL_SILENCE_MINS * 60:
            now = time.time()
            since_last_alert = now - _last_silence_alert
            # First alert immediately; repeat every PI_ALERT_REPEAT_MINS
            if not _calls_were_silent or since_last_alert >= PI_ALERT_REPEAT_MINS * 60:
                _calls_were_silent = True
                _silence_alert_count += 1
                _last_silence_alert = now
                mins = int(silence_secs // 60)
                suffix = f" (reminder #{_silence_alert_count})" if _silence_alert_count > 1 else ""
                _pi_watchdog_alert(
                    f"⚠️ BATTLE BUDDY ALERT: No audio from OP25 for {mins} minutes{suffix} — check SDR or collector."
                )
            # Auto-restart if silent long enough and Pi is reachable
            if silence_secs > PI_AUTORESTART_MINS * 60 and pi_up:
                threading.Thread(target=_pi_autorestart_op25, daemon=True).start()
        elif _calls_were_silent:
            _calls_were_silent = False
            _silence_alert_count = 0
            _last_silence_alert = 0.0
            _pi_watchdog_alert("✅ Audio feed is active again — calls resuming.")


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# APD Press Release poller — Google News RSS
# NOTE: austintexas.gov/news is behind Incapsula CDN which hard-blocks the
# Contabo VPS IP (147.93.134.105). Do NOT attempt to scrape austintexas.gov
# directly from this server — it will always return 403. Google News RSS
# aggregates APD press releases from KXAN, KVUE, AAS, etc. with no bot-detect.
# ---------------------------------------------------------------------------

APD_NEWS_URL      = (
    "https://news.google.com/rss/search"
    "?q=APD+Austin+%22press+release%22+(homicide+OR+shooting+OR+stabbing)"
    "&hl=en-US&gl=US&ceid=US:en"
)
APD_NEWS_INTERVAL = 300   # poll every 5 minutes
_ARTICLE_MAX_AGE_SECS = 72 * 3600  # reject news articles older than 72h from radio-call matching

# Broader Google News search for Austin traffic fatalities — used to link
# crash articles to radio-detected incidents. No incident creation on no-match.
TRAFFIC_NEWS_URL = (
    "https://news.google.com/rss/search"
    "?q=Austin+Texas+(fatal+crash+OR+pedestrian+killed+OR+hit-and-run)"
    "&hl=en-US&gl=US&ceid=US:en"
)

# Maps article event type → compatible radio incident itypes for matching
_NEWS_ITYPE_COMPAT: dict[str, set] = {
    "SHOOTING":        {"SHOOTING", "OFFICER DOWN", "WEAPONS"},
    "STABBING":        {"STABBING", "WEAPONS"},
    "HOMICIDE":        {"SHOOTING", "STABBING", "OFFICER DOWN", "WEAPONS"},
    "WEAPONS":         {"WEAPONS", "SHOOTING", "STABBING"},
    "CRASH/COLLISION": {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "FATAL CRASH":     {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "STRUCTURE FIRE":  {"STRUCTURE FIRE", "FIRE DISPATCH"},
}

# Source site RSS feeds reachable from VPS — used to resolve real article URLs
_APD_SOURCE_RSS = {
    "kxan.com":          "https://www.kxan.com/news/local/feed/",
    "kvue.com":          "https://www.kvue.com/feeds/syndication/rss/news/local/",
    "austincurrent.org": "https://austincurrent.org/feed/",
}

# _APD_NEWS_SEEN replaced by apd_seen DB table (persistent across restarts)
_APD_NEWS_LOCK    = threading.Lock()

_APD_HEADLINE_KW  = [
    "homicide", "shooting", "shot", "stabbing", "robbery",
    "assault", "death", "body", "fatal", "critical", "officer",
    "arrest", "suspect", "murder", "aggravated",
]

def _apd_parse_rss(xml_text: str) -> list[dict]:
    """Parse Google News RSS feed; return list of {title, link}."""
    import xml.etree.ElementTree as ET
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[apd-news] RSS parse error: {e}", flush=True)
        return []
    channel = root.find("channel")
    if channel is None:
        return []
    seen = set()
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el  = item.find("link")
        if title_el is None or link_el is None:
            continue
        title      = (title_el.text or "").strip()
        link       = (link_el.text or "").strip()
        source_el  = item.find("source")
        source_url = source_el.get("url", "") if source_el is not None else ""
        pub_ts     = None
        pub_el     = item.find("pubDate")
        if pub_el is not None and pub_el.text:
            try:
                from email import utils as _eu
                _parsed = _eu.parsedate_tz(pub_el.text.strip())
                if _parsed:
                    pub_ts = float(_eu.mktime_tz(_parsed))
            except Exception:
                pass
        if title and link and link not in seen:
            seen.add(link)
            items.append({"title": title, "link": link,
                          "source_url": source_url, "pub_ts": pub_ts})
    return items

def _resolve_article_url(source_url: str, title: str, gnews_link: str) -> str:
    """
    Try to resolve the real article URL via the source site's RSS feed.
    Falls back to a browser-accessible Google News /articles/ URL.
    """
    import xml.etree.ElementTree as _ET
    from urllib.parse import urlparse as _urlparse
    # Strip "- Publisher Name" suffix that Google News appends to titles
    clean  = title.rsplit(" - ", 1)[0].lower().strip()
    domain = re.sub(r"^www\.", "", _urlparse(source_url).netloc)
    rss_url = _APD_SOURCE_RSS.get(domain)
    if rss_url and len(clean) > 20:
        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": "BattleBuddy/2.0"})
            xml_text = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
            root = _ET.fromstring(xml_text)
            ch = root.find("channel")
            if ch is not None:
                for item in ch.findall("item"):
                    t_el = item.find("title")
                    l_el = item.find("link")
                    if t_el is None or l_el is None:
                        continue
                    if clean[:40] in (t_el.text or "").lower():
                        real_url = (l_el.text or "").strip()
                        if real_url:
                            print(f"[apd-news] resolved via source RSS: {real_url}", flush=True)
                            return real_url
        except Exception as e:
            print(f"[apd-news] source RSS lookup failed ({domain}): {e}", flush=True)
    # Tier 2: Google Custom Search API — works for any source
    if GOOGLE_CSE_API_KEY and GOOGLE_CSE_ID:
        query_title = title.rsplit(" - ", 1)[0]  # strip publisher suffix
        from urllib.parse import urlparse as _up2
        src_domain = re.sub(r"^www\.", "", _up2(source_url).netloc) if source_url else ""
        site_filter = f"site:{src_domain} " if src_domain else ""
        import json as _json
        cse_params = urllib.parse.urlencode({
            "key": GOOGLE_CSE_API_KEY,
            "cx":  GOOGLE_CSE_ID,
            "q":   f'{site_filter}"{query_title[:80]}"',
            "num": "1",
        })
        cse_url = f"https://www.googleapis.com/customsearch/v1?{cse_params}"
        try:
            cse_req  = urllib.request.Request(cse_url, headers={"User-Agent": "BattleBuddy/2.0"})
            cse_resp = urllib.request.urlopen(cse_req, timeout=10).read().decode("utf-8")
            items    = _json.loads(cse_resp).get("items", [])
            if items:
                cse_link = items[0].get("link", "")
                if cse_link.startswith("http"):
                    print(f"[apd-news] resolved via Google CSE: {cse_link}", flush=True)
                    return cse_link
        except Exception as e:
            print(f"[apd-news] Google CSE lookup failed: {e}", flush=True)
    # Fallback: /rss/articles/ is RSS-only; /articles/ is browser-accessible
    return re.sub(r"[?&]oc=\d+", "", gnews_link.replace("/rss/articles/", "/articles/")).rstrip("?&")


def _apd_fetch_article(url: str) -> dict:
    """Fetch a news article URL (follows redirects), extract address and description.
    Tries the Pi5 residential-IP fetch agent first; falls back to direct fetch.
    """
    import re
    # Try residential Pi fetch first (bypasses datacenter IP blocks)
    pi_result = _pi_fetch(url)
    if pi_result:
        return pi_result
    # Fallback: direct fetch from VPS
    try:
        req  = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0.0.0 Safari/537.36"}
        )
        resp      = urllib.request.urlopen(req, timeout=15)
        final_url = resp.url
        html      = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[apd-news] article fetch failed {url}: {e}", flush=True)
        return {}

    # Strip tags for text extraction
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    # Look for address patterns: "1234 Some Street" or "1234 block of Some Street"
    addr_m = re.search(
        r"(\d{3,5}(?:\s+block\s+of)?\s+[A-Z][a-zA-Z0-9 ,.]+(?:Street|St|Avenue|Ave|Drive|Dr|"
        r"Road|Rd|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|Parkway|Pkwy|Highway|Hwy|"
        r"Loop|Trail|Trl|Pass|Crossing|Crossing|Place|Pl)(?:\s+(?:NW|NE|SW|SE|N|S|E|W))?)",
        text
    )
    address = addr_m.group(1).strip() if addr_m else None

    # Pull first 400 chars of body text after stripping nav/header noise
    body_m = re.search(r"Case Number[:\s]+(.*?)(?:Tips|Contact|Crime Stoppers)", text, re.DOTALL)
    summary = body_m.group(0)[:400].strip() if body_m else text[500:900].strip()

    return {"url": final_url, "address": address, "summary": summary}


def _pi_fetch(url: str, referer: str = "") -> dict:
    """Fetch a URL via the Pi5 fetch agent (residential IP, browser headers).
    Returns the same dict shape as _apd_fetch_article on success.
    Returns {} if Pi is unavailable — caller falls back to direct fetch.
    """
    if not PI_FETCH_ENABLED:
        return {}
    import json as _json
    payload = _json.dumps({"url": url, "referer": referer}).encode()
    req = urllib.request.Request(
        f"{PI_FETCH_URL}/fetch",
        data=payload,
        headers={
            "Authorization": f"Bearer {PI_FETCH_TOKEN}",
            "Content-Type":  "application/json",
        },
        method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = _json.loads(resp.read().decode("utf-8"))
        if data.get("status") != 200:
            return {}
        text = data.get("text", "")
        html = data.get("html", "")
        # Extract address from text (reuse same regex as _apd_fetch_article)
        addr_m = re.search(
            r"(\d{3,5}(?:\s+block\s+of)?\s+[A-Z][a-zA-Z0-9 ,.]+(?:Street|St|Avenue|Ave|"
            r"Drive|Dr|Road|Rd|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|"
            r"Parkway|Pkwy|Highway|Hwy|Loop|Trail|Trl|Pass|Place|Pl)(?:\s+(?:NW|NE|SW|SE|N|S|E|W))?)",
            text
        )
        address = addr_m.group(1).strip() if addr_m else None
        summary = text[:400].strip()
        return {
            "url":     data.get("final_url", url),
            "address": address,
            "summary": summary,
            "text":    text,
        }
    except Exception as e:
        print(f"[pi-fetch] {url[:60]} failed: {e}", flush=True)
        return {}


_ARTICLE_STOP_WORDS = {
    "a","an","the","and","or","in","on","at","of","to","is","was","are","were",
    "for","with","that","this","from","by","has","have","had","been","will","be",
    "it","its","as","up","out","after","police","apd","austin","texas","tx",
    "officer","officers","department","says","said","according","report",
    "reported","investigation","man","woman","near","over","into","between",
    "one","two","three","new","s","no","not","they","he","she","his","her",
}


def _match_article_to_incident(title: str, article_itype: str, article_ts: float) -> tuple:
    """Try to match a news article to a recent radio-detected incident.
    Returns (incident_id, score) or (None, 0).
    Searches incidents from the 48h window preceding the article.
    """
    compat = _NEWS_ITYPE_COMPAT.get(article_itype, {article_itype})
    placeholders = ",".join("?" * len(compat))
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        f"SELECT id, itype, description, location, ts_start FROM incidents "
        f"WHERE ts_start >= ? AND ts_start <= ? "
        f"AND itype IN ({placeholders}) "
        f"AND description NOT LIKE '%APD Press Release%' "
        f"ORDER BY ts_start DESC LIMIT 20",
        [article_ts - 48*3600, article_ts + 3600] + list(compat)
    ).fetchall()
    conn.close()
    if not rows:
        return None, 0
    # Extract location tokens from article title for scoring
    title_lower = title.lower()
    highways = set(re.findall(
        r"\b(?:i-?|ih-?|hwy\s*|fm\s*|us-?|sh-?|tx-?)\d+\b", title_lower))
    streets  = set(re.findall(
        r"[a-z]+ (?:street|st|avenue|ave|drive|dr|road|rd|lane|ln|boulevard|blvd"
        r"|way|parkway|pkwy|highway|loop|trail|pass)\b", title_lower))
    words    = {w for w in re.findall(r"[a-z0-9]+", title_lower)
                if len(w) > 3 and w not in _ARTICLE_STOP_WORDS}
    location_tokens = highways | streets
    best_id, best_score = rows[0][0], 0.5
    for inc_id, itype, desc, location, ts_start in rows:
        score = 0.5
        if itype == article_itype:
            score += 0.5
        combined = ((desc or "") + " " + (location or "")).lower()
        for token in location_tokens:
            if token in combined:
                score += 2.0
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\b", combined):
                score += 0.3
        if score > best_score:
            best_score = score
            best_id = inc_id
    # Single candidate: accept it (itype already filtered)
    if len(rows) == 1:
        return rows[0][0], max(best_score, 1.0)
    return (best_id, best_score) if best_score >= 1.0 else (None, 0)


def _store_article_link(incident_id: int | None, ts: float, headline: str,
                        url: str, source: str, snippet: str, score: float):
    """Insert a row into incident_articles and update incidents.article_url."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO incident_articles "
        "(incident_id, ts, headline, url, source, snippet, match_score) "
        "VALUES (?,?,?,?,?,?,?)",
        (incident_id, ts, headline, url, source, snippet[:300] if snippet else "", score)
    )
    if incident_id:
        conn.execute(
            "UPDATE incidents SET article_url=? WHERE id=? AND article_url IS NULL",
            (url, incident_id)
        )
    conn.commit()
    conn.close()


def apd_news_thread():
    """Poll APD press release page for new homicide/shooting announcements."""
    global _APD_NEWS_SEEN
    print("[apd-news] APD press release poller started", flush=True)
    while True:
        time.sleep(APD_NEWS_INTERVAL)
        try:
            req  = urllib.request.Request(
                APD_NEWS_URL,
                headers={"User-Agent": "BattleBuddy/2.0",
                         "Accept": "application/rss+xml, application/xml, text/xml"}
            )
            xml_text = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[apd-news] fetch error: {e}", flush=True)
            continue

        articles = _apd_parse_rss(xml_text)
        # Dedup against DB — persistent across restarts
        with _APD_NEWS_LOCK:
            conn_d = sqlite3.connect(DB_PATH)
            existing = {row[0] for row in conn_d.execute("SELECT url FROM apd_seen")}
            new_articles = [a for a in articles if a["link"] not in existing]
            if new_articles:
                conn_d.executemany(
                    "INSERT OR IGNORE INTO apd_seen (url, ts) VALUES (?,?)",
                    [(a["link"], time.time()) for a in new_articles]
                )
                conn_d.commit()
            conn_d.close()
        for article in new_articles:
            title = article["title"].lower()
            if not any(kw in title for kw in _APD_HEADLINE_KW):
                continue

            print(f"[apd-news] NEW: {article['title']}", flush=True)
            url     = _resolve_article_url(
                article.get("source_url", ""), article["title"], article["link"]
            )
            detail  = _apd_fetch_article(url)
            address = detail.get("address")
            summary = detail.get("summary", article["title"])

            lat, lon = None, None
            if address:
                coords = _geocode_address(address)
                if coords:
                    lat, lon = coords

            # Determine itype from title
            t = article["title"].lower()
            if "homicide" in t or "murder" in t:
                itype = "HOMICIDE"
            elif "fatal" in t and any(w in t for w in ("crash","accident","hit","pedestrian","collision")):
                itype = "FATAL CRASH"
            elif "shooting" in t or " shot" in t:
                itype = "SHOOTING"
            elif "stab" in t:
                itype = "STABBING"
            elif "robbery" in t or "aggravated assault" in t:
                itype = "WEAPONS"
            elif "crash" in t or "collision" in t or "pedestrian" in t:
                itype = "CRASH/COLLISION"
            else:
                itype = "SHOOTING"

            pub_ts = article.get("pub_ts")
            if not pub_ts:
                print(f"[news] SKIP apd_pr (no pub_ts): {article['title']}", flush=True)
                continue
            if time.time() - pub_ts > _ARTICLE_MAX_AGE_SECS:
                age_h = (time.time() - pub_ts) / 3600
                print(f"[news] SKIP apd_pr (stale {age_h:.1f}h): {article['title']}", flush=True)
                continue
            ts   = pub_ts
            desc = f"[APD Press Release] {article['title']}. {summary[:200]}"

            # Try to match article to an existing radio-detected incident
            matched_id, match_score = _match_article_to_incident(article["title"], itype, ts)

            if matched_id:
                # Article links to a radio incident — store the link and notify
                _store_article_link(matched_id, ts, article["title"], url, "apd_pr",
                                    summary[:300], match_score)
                print(f"[apd-news] LINKED: '{article['title']}' → incident {matched_id} "
                      f"(score={match_score:.1f})", flush=True)
                loc_str = f" @ {address}" if address else ""
                msg = (
                    f"\U0001f4f0 [PRESS COVERAGE] Radio incident #{matched_id} now in the news\n"
                    f"\U0001f4f0 {article['title']}\n"
                    f"\U0001f517 {url}\n"
                    f"\U0001f4cd{loc_str}"
                )
                payload = json.dumps({"message": msg}).encode()
                creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
                headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                           "Content-Type": "application/json"}
                for room in [TALK_ROOMS["apd"], TALK_ROOMS["incidents"]]:
                    req2 = urllib.request.Request(
                        f"{TALK_BASE}/chat/{room}",
                        data=payload, headers=headers, method="POST"
                    )
                    try:
                        urllib.request.urlopen(req2, timeout=10)
                    except Exception as e:
                        print(f"[apd-news] Talk post (match) failed: {e}", flush=True)
            else:
                # No radio match — create a new incident from the press release
                conn = sqlite3.connect(DB_PATH)
                cur  = conn.execute(
                    "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, "
                    "tgids, location, lat, lon, article_url, status) VALUES (?,?,?,?,?,?,?,?,?,?,'active')",
                    (ts, ts, itype, desc, '["APD"]', '[]',
                     address, lat, lon, url)
                )
                inc_id = cur.lastrowid
                conn.commit()
                conn.close()
                _store_article_link(inc_id, ts, article["title"], url, "apd_pr",
                                    summary[:300], 0.0)
                loc_str  = f" @ {address}" if address else ""
                msg = (
                    f"\U0001f6a8 [APD PRESS RELEASE] {article['title']}\n"
                    f"\U0001f517 {url}\n"
                    f"\U0001f4cd{loc_str}\n"
                    f"{summary[:300]}"
                )
                payload = json.dumps({"message": msg}).encode()
                creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
                headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                           "Content-Type": "application/json"}
                for room in [TALK_ROOMS["apd"], TALK_ROOMS["incidents"]]:
                    req2 = urllib.request.Request(
                        f"{TALK_BASE}/chat/{room}",
                        data=payload, headers=headers, method="POST"
                    )
                    try:
                        urllib.request.urlopen(req2, timeout=10)
                    except Exception as e:
                        print(f"[apd-news] Talk post failed: {e}", flush=True)
                threading.Thread(
                    target=send_dm_alert,
                    args=(itype, desc, address, "APD", "APD"),
                    daemon=True
                ).start()
                if lat is not None and lon is not None:
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(inc_id, lat, lon, itype, address, desc),
                        daemon=True
                    ).start()

        # --- Traffic/crash news — link to existing radio incidents only -------
        try:
            treq = urllib.request.Request(
                TRAFFIC_NEWS_URL,
                headers={"User-Agent": "BattleBuddy/2.0",
                         "Accept": "application/rss+xml, application/xml, text/xml"}
            )
            txml_text = urllib.request.urlopen(treq, timeout=15).read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[traffic-news] fetch error: {e}", flush=True)
            txml_text = None

        if txml_text:
            tarticles = _apd_parse_rss(txml_text)
            with _APD_NEWS_LOCK:
                conn_t = sqlite3.connect(DB_PATH)
                t_existing = {row[0] for row in conn_t.execute("SELECT url FROM apd_seen")}
                t_new = [a for a in tarticles if a["link"] not in t_existing]
                if t_new:
                    conn_t.executemany(
                        "INSERT OR IGNORE INTO apd_seen (url, ts) VALUES (?,?)",
                        [(a["link"], time.time()) for a in t_new]
                    )
                    conn_t.commit()
                conn_t.close()
            for ta in t_new:
                ttitle = ta["title"].lower()
                if not any(kw in ttitle for kw in
                           ("fatal", "killed", "pedestrian", "hit-and-run", "deadly")):
                    continue
                turl = _resolve_article_url(
                    ta.get("source_url", ""), ta["title"], ta["link"])
                art_itype = "FATAL CRASH" if any(
                    w in ttitle for w in ("fatal","killed","dead","deadly")) else "CRASH/COLLISION"
                tts = ta.get("pub_ts")
                if not tts:
                    print(f"[news] SKIP traffic-news (no pub_ts): {ta['title']}", flush=True)
                    continue
                if time.time() - tts > _ARTICLE_MAX_AGE_SECS:
                    age_h = (time.time() - tts) / 3600
                    print(f"[news] SKIP traffic-news (stale {age_h:.1f}h): {ta['title']}", flush=True)
                    continue
                t_inc_id, t_score = _match_article_to_incident(ta["title"], art_itype, tts)
                if t_inc_id:
                    t_detail  = _apd_fetch_article(turl)
                    t_snippet = t_detail.get("summary", "")
                    t_address = t_detail.get("address")
                    if t_address:
                        t_coords = _geocode_address(t_address)
                        if t_coords:
                            conn_ta = sqlite3.connect(DB_PATH)
                            conn_ta.execute(
                                "UPDATE incidents SET location=?, lat=?, lon=? "
                                "WHERE id=? AND (location IS NULL OR location='')",
                                (t_address, t_coords[0], t_coords[1], t_inc_id)
                            )
                            conn_ta.commit()
                            conn_ta.close()
                    _store_article_link(t_inc_id, tts, ta["title"], turl,
                                        "traffic-news", t_snippet, t_score)
                    print(f"[traffic-news] LINKED: '{ta['title']}' → "
                          f"incident {t_inc_id} (score={t_score:.1f})"
                          f"{f' addr={t_address}' if t_address else ''}", flush=True)


# ---------------------------------------------------------------------------
# ADS-B air asset tracker — adsb.lol (no rate limit, 30s poll)
# Detects helicopters and law enforcement aircraft over Austin
# Stores 30-min position trails in aircraft_positions table
# ---------------------------------------------------------------------------

# adsb.lol /v2/lat/{lat}/lon/{lon}/dist/{nm} — aircraft within dist nautical miles
_ADSB_LOL_URL    = "https://api.adsb.lol/v2/lat/30.2672/lon/-97.7431/dist/52"
ADSB_INTERVAL    = 30    # poll every 30 seconds
ADSB_MAX_ALT_FT  = 5000  # only track aircraft below 5,000 ft AGL
ADSB_TRAIL_SECS  = 1800  # 30 minutes of trail history
ADSB_REFRACTORY  = 1800  # 30 min before re-alerting same aircraft

# Known Austin-area LEO / EMS air assets (icao24 hex → (label, is_leo))
KNOWN_AIR_ASSETS = {
    "a820f8": ("APD Air1 (N6227)",         True),   # Eurocopter AS350B3 — LEO
    "a064fb": ("APD Air Support (N1240W)", True),   # Eurocopter EC120B — LEO
    "a33eb6": ("STAR Flight 2 (N308TC)",   False),  # Leonardo AW169 — EMS
    "a3426d": ("STAR Flight 3 (N309TC)",   False),  # Leonardo AW169 — EMS
}

_adsb_seen       : dict[str, float] = {}   # icao24 → last alert timestamp
_adsb_orbit_seen : dict[str, float] = {}   # icao24 → last orbit-alert timestamp

# ─────────────────────────────────────────────────────────────────────────────
# Reddit citizen intel poller
# ─────────────────────────────────────────────────────────────────────────────
_REDDIT_INTERVAL = 300   # 5 minutes
_REDDIT_FEEDS = [
    "https://www.reddit.com/r/Austin/new.rss",
]
_REDDIT_HIGH_KW = {
    "standoff", "barricade", "swat", "shooter", "shooting", "shots fired",
    "shots", "hostage", "suspect", "armed", "pursuit", "chase", "evacuate",
    "lockdown", "explosion", "stabbing", "homicide", "murder",
    "police activity", "crime scene", "avoid the area",
}
_REDDIT_MEDIUM_KW = {
    "police", "apd", "afd", "crash", "accident", "fire", "smoke", "blocked",
    "road closed", "emergency", "cop", "cops", "officer", "helicopter",
    "air1", "star flight",
}

def _reddit_matches(title, body):
    text = (title + " " + (body or "")).lower()
    hi   = [kw for kw in _REDDIT_HIGH_KW   if kw in text]
    med  = [kw for kw in _REDDIT_MEDIUM_KW if kw in text]
    all_kw = hi + [m for m in med if m not in hi]
    return bool(hi), bool(all_kw), ",".join(all_kw)


def _reddit_match_incident(title, body, ts):
    """Score a reddit post against incidents within ±4h. Returns (incident_id, score) or (None, 0)."""
    text = (title + " " + (body or "")).lower()
    window = 4 * 3600
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, ts_start, itype, description, location FROM incidents "
        "WHERE ts_start BETWEEN ? AND ? AND is_test=0",
        (ts - window, ts + window)
    ).fetchall()
    conn.close()

    _TYPE_KW = {
        "SHOOTING":        ["shooting","shot","shots","fired","gun","gunshot","bullet","gunfire"],
        "STABBING":        ["stabbing","stabbed","knife","stab"],
        "CRASH/COLLISION": ["crash","accident","collision","wreck"],
        "STRUCTURE FIRE":  ["fire","smoke","burning","flames","blaze"],
        "HOMICIDE":        ["murder","homicide","killed","dead","body found"],
        "AIR ASSET ACTIVE":["helicopter","air1","star flight","chopper","aircraft"],
        "PURSUIT":         ["pursuit","chase","fleeing","high speed"],
        "OFFICER DOWN":    ["officer down","officer shot","cop shot"],
    }

    best_score, best_id = 0.0, None
    for inc_id, ts_start, itype, description, location in rows:
        score = 0.0
        for kw in _TYPE_KW.get(itype, []):
            if kw in text:
                score += 4
                break
        if location:
            for lw in (w.lower().strip(".,") for w in location.split() if len(w) > 4):
                if lw in text:
                    score += 6
        if description:
            dw = {w.lower().strip(".,") for w in description.split() if len(w) > 5}
            score += min(len(dw & set(text.split())) * 1.5, 6)
        diff = abs(ts - ts_start) / 3600
        score += 5 if diff < 0.5 else (3 if diff < 1 else (1 if diff < 2 else 0))
        if score > best_score:
            best_score, best_id = score, inc_id

    return (best_id, round(best_score, 1)) if best_score >= 8 else (None, 0.0)

def reddit_intel_thread():
    import xml.etree.ElementTree as _ET
    import html as _html
    print("[reddit] citizen intel poller started", flush=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS reddit_intel (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        post_id TEXT UNIQUE,
        subreddit TEXT,
        title TEXT,
        url TEXT,
        author TEXT,
        body TEXT,
        keywords TEXT,
        notified INTEGER DEFAULT 0
    )""")
    for _col_sql in [
        "ALTER TABLE reddit_intel ADD COLUMN incident_id INTEGER",
        "ALTER TABLE reddit_intel ADD COLUMN match_score REAL DEFAULT 0",
    ]:
        try: conn.execute(_col_sql)
        except Exception: pass
    conn.commit()
    conn.close()

    while True:
        for feed_url in _REDDIT_FEEDS:
            try:
                req = urllib.request.Request(
                    feed_url,
                    headers={"User-Agent": "BattleBuddy/2.0 (contact: admin@battlebuddy.news)"}
                )
                xml_bytes = urllib.request.urlopen(req, timeout=15).read()
                root = _ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
            except Exception as e:
                print(f"[reddit] fetch error {feed_url}: {e}", flush=True)
                continue

            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns)
            subreddit = feed_url.split("/r/")[1].split("/")[0]

            for entry in entries:
                post_id_raw = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
                post_id = post_id_raw.split("_")[-1] if "_" in post_id_raw else post_id_raw
                title   = _html.unescape((entry.findtext("atom:title", default="", namespaces=ns) or "").strip())
                link_el = entry.find("atom:link[@rel='alternate']", ns)
                url     = link_el.attrib.get("href", "") if link_el is not None else ""
                if not url:
                    # Fallback: some Reddit Atom entries omit rel=alternate or use a bare <link href=...>.
                    any_link = entry.find("atom:link", ns)
                    if any_link is not None:
                        url = any_link.attrib.get("href", "") or ""
                if not url and post_id:
                    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
                author_el = entry.find("atom:author/atom:name", ns)
                author  = author_el.text.strip() if author_el is not None else ""
                content_el = entry.find("atom:content", ns)
                body_html  = (content_el.text or "") if content_el is not None else ""
                body = re.sub(r"<[^>]+>", " ", body_html)
                body = _html.unescape(body).strip()[:800]

                if not post_id or not title:
                    continue

                hi, matched, keywords = _reddit_matches(title, body)
                if not matched:
                    continue

                conn = sqlite3.connect(DB_PATH)
                existing = conn.execute(
                    "SELECT notified FROM reddit_intel WHERE post_id=?", (post_id,)
                ).fetchone()

                if existing is None:
                    conn.execute(
                        "INSERT INTO reddit_intel (ts,post_id,subreddit,title,url,author,body,keywords,notified) "
                        "VALUES (?,?,?,?,?,?,?,?,0)",
                        (time.time(), post_id, subreddit, title, url, author, body[:500], keywords)
                    )
                    conn.commit()
                    conn.close()
                    print(f"[reddit] NEW {'HI' if hi else 'med'}: {title[:80]}", flush=True)
                    # cross-reference against incidents
                    inc_id, inc_score = _reddit_match_incident(title, body, time.time())
                    if inc_id:
                        _c = sqlite3.connect(DB_PATH)
                        _c.execute("UPDATE reddit_intel SET incident_id=?,match_score=? WHERE post_id=?",
                                   (inc_id, inc_score, post_id))
                        _c.commit(); _c.close()
                        print(f"[reddit] matched post {post_id} → incident #{inc_id} (score {inc_score})", flush=True)

                    if hi:
                        msg = (
                            f"Reddit Citizen Report — r/{subreddit}\n"
                            f"{title}\n"
                            f"Keywords: {keywords}\n"
                            f"{url}"
                        )
                        threading.Thread(
                            target=send_dm_alert,
                            args=("CITIZEN REPORT", msg, title, "Reddit", "general"),
                            daemon=True
                        ).start()
                        conn2 = sqlite3.connect(DB_PATH)
                        conn2.execute("UPDATE reddit_intel SET notified=1 WHERE post_id=?", (post_id,))
                        conn2.commit()
                        conn2.close()
                else:
                    conn.close()

        time.sleep(_REDDIT_INTERVAL)


_adsb_lock       = threading.Lock()


def _adsb_check_orbit(icao24: str, now: float) -> bool:
    """Return True if icao24 has been orbiting (circling/hovering) in the last 5 minutes."""
    import math
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT lat, lon, heading FROM aircraft_positions "
            "WHERE icao24=? AND ts >= ? ORDER BY ts",
            (icao24, now - 300)
        ).fetchall()
        conn.close()
    except Exception:
        return False

    if len(rows) < 6:
        return False

    lats = [r[0] for r in rows]
    lons = [r[1] for r in rows]
    headings = [r[2] for r in rows if r[2] is not None]

    clat = sum(lats) / len(lats)
    clon = sum(lons) / len(lons)

    def _km(la, lo, lb, lb2):
        R = 6371.0
        dlat = math.radians(lb - la)
        dlon = math.radians(lb2 - lo)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(la)) * math.cos(math.radians(lb)) * math.sin(dlon/2)**2
        return R * 2 * math.asin(math.sqrt(a))

    max_dist = max(_km(clat, clon, la, lo) for la, lo in zip(lats, lons))
    if max_dist > 1.2:
        return False

    if len(headings) < 5:
        return False
    min_h = min(headings)
    max_h = max(headings)
    span = max_h - min_h
    return span >= 180


def adsb_air_asset_thread():
    """Poll adsb.lol every 30s for low-altitude helicopters over Austin.

    Stores every position in aircraft_positions (30-min trail).
    Alerts on first detection of each aircraft (refractory 30 min).
    """
    print("[adsb] ADS-B air asset tracker started (adsb.lol)", flush=True)

    # Ensure table exists at thread start in case init_db ran before schema update
    _c = sqlite3.connect(DB_PATH)
    _c.execute("""CREATE TABLE IF NOT EXISTS aircraft_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        icao24 TEXT NOT NULL,
        callsign TEXT,
        lat REAL NOT NULL,
        lon REAL NOT NULL,
        alt_ft INTEGER,
        heading REAL,
        speed_kts REAL,
        is_leo INTEGER DEFAULT 0,
        label TEXT
    )""")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_ts ON aircraft_positions(ts)")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_icao ON aircraft_positions(icao24, ts)")
    _c.commit()
    _c.close()

    while True:
        time.sleep(ADSB_INTERVAL)
        try:
            req  = urllib.request.Request(
                _ADSB_LOL_URL,
                headers={"User-Agent": "BattleBuddy/2.0", "Accept": "application/json"}
            )
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        except Exception as e:
            print(f"[adsb] fetch error: {e}", flush=True)
            continue

        aircraft = data.get("ac") or []
        now = time.time()

        # Prune positions older than trail window
        try:
            _pc = sqlite3.connect(DB_PATH)
            _pc.execute("DELETE FROM aircraft_positions WHERE ts < ?", (now - ADSB_TRAIL_SECS,))
            _pc.commit()
            _pc.close()
        except Exception:
            pass

        for ac in aircraft:
            icao24   = (ac.get("hex") or "").strip().lower()
            callsign = (ac.get("flight") or ac.get("r") or "").strip()
            lat      = ac.get("lat")
            lon      = ac.get("lon")
            alt_ft   = ac.get("alt_baro")   # feet in adsb.lol
            heading  = ac.get("track")
            speed_kts = ac.get("gs")        # ground speed knots
            on_ground = ac.get("alt_baro") == "ground" or ac.get("on_ground") == 1

            if not icao24 or lat is None or lon is None:
                continue
            if on_ground:
                continue

            # alt_baro can be "ground" string or a number
            if isinstance(alt_ft, str):
                continue
            if alt_ft is None or alt_ft > ADSB_MAX_ALT_FT:
                continue

            # adsb.lol category field: "A7"=helicopter, "A1"-"A3"=fixed-wing light/medium
            category = (ac.get("category") or "").upper()
            is_helo  = category.startswith("A7") or category.startswith("B")

            label, is_leo = KNOWN_AIR_ASSETS.get(icao24, (None, False))

            # Only track unknown aircraft if it's a helicopter
            if label is None and not is_helo:
                continue

            # Store position for trail
            try:
                _tc = sqlite3.connect(DB_PATH)
                _tc.execute(
                    "INSERT INTO aircraft_positions (ts,icao24,callsign,lat,lon,alt_ft,heading,speed_kts,is_leo,label) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now, icao24, callsign or None, lat, lon,
                     int(alt_ft) if alt_ft else None,
                     heading, speed_kts, 1 if is_leo else 0,
                     label or (callsign or f"ICAO {icao24}"))
                )
                _tc.commit()
                _tc.close()
            except Exception as e:
                print(f"[adsb] db write error: {e}", flush=True)

            # Alert on first detection (refractory)
            with _adsb_lock:
                last_alert = _adsb_seen.get(icao24, 0)
                if now - last_alert < ADSB_REFRACTORY:
                    continue
                _adsb_seen[icao24] = now

            cs_str  = f" ({callsign})" if callsign else ""
            lbl_str = label or "Unknown Helicopter"

            if not is_leo:
                desc = (
                    f"[ADS-B] {lbl_str}{cs_str} airborne over Austin — "
                    f"{int(alt_ft)}ft AGL, ICAO {icao24}"
                )
                print(f"[adsb] NON-LEO HELO (map only): {desc}", flush=True)
                synthetic_id = -int(now * 1000) % 2147483647
                threading.Thread(
                    target=_atak_post_marker,
                    args=(synthetic_id, lat, lon, "AIR ASSET EMS", lbl_str + cs_str, desc),
                    daemon=True
                ).start()
                continue

            desc = (
                f"[ADS-B] {lbl_str}{cs_str} detected over Austin — "
                f"{int(alt_ft)}ft AGL, ICAO {icao24}"
            )
            print(f"[adsb] LEO AIR ASSET: {desc}", flush=True)

            conn = sqlite3.connect(DB_PATH)
            cur  = conn.execute(
                "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, "
                "tgids, location, lat, lon, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
                (now, now, "AIR ASSET ACTIVE", desc, '["APD"]', '[]',
                 "Austin airspace", lat, lon)
            )
            inc_id = cur.lastrowid
            conn.commit()
            conn.close()

            msg = (
                f"\U0001f681 LEO AIR ASSET: {lbl_str}{cs_str}\n"
                f"Altitude: {int(alt_ft)}ft | ICAO: {icao24}\n"
                f"Position: {lat:.4f}, {lon:.4f}"
            )
            payload = json.dumps({"message": msg}).encode()
            creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
            headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                       "Content-Type": "application/json"}
            req2 = urllib.request.Request(
                f"{TALK_BASE}/chat/{TALK_ROOMS['apd']}",
                data=payload, headers=headers, method="POST"
            )
            try:
                urllib.request.urlopen(req2, timeout=10)
            except Exception as e:
                print(f"[adsb] Talk post failed: {e}", flush=True)

            threading.Thread(
                target=_atak_post_marker,
                args=(inc_id, lat, lon, "AIR ASSET ACTIVE", "Austin airspace", desc),
                daemon=True
            ).start()

        # ── Orbit detection pass ──────────────────────────────────────────────
        orbit_now = time.time()
        try:
            orb_conn = sqlite3.connect(DB_PATH)
            leo_icaos = orb_conn.execute(
                "SELECT DISTINCT icao24 FROM aircraft_positions WHERE is_leo=1 AND ts >= ?",
                (orbit_now - 300,)
            ).fetchall()
            orb_conn.close()
        except Exception:
            leo_icaos = []

        for (oicao,) in leo_icaos:
            with _adsb_lock:
                if orbit_now - _adsb_orbit_seen.get(oicao, 0) < ADSB_REFRACTORY:
                    continue

            if not _adsb_check_orbit(oicao, orbit_now):
                continue

            with _adsb_lock:
                _adsb_orbit_seen[oicao] = orbit_now

            try:
                _oc = sqlite3.connect(DB_PATH)
                orow = _oc.execute(
                    "SELECT lat, lon, alt_ft, label, callsign FROM aircraft_positions "
                    "WHERE icao24=? ORDER BY ts DESC LIMIT 1", (oicao,)
                ).fetchone()
                _oc.close()
            except Exception:
                continue
            if not orow:
                continue

            olat, olon, oalt, olabel, ocallsign = orow
            olbl = olabel or f"ICAO {oicao}"
            ocs  = f" ({ocallsign})" if ocallsign else ""
            odesc = (
                f"[ADS-B ORBIT] {olbl}{ocs} circling over Austin — "
                f"possible ground surveillance. {int(oalt or 0)}ft AGL, ICAO {oicao}"
            )
            print(f"[adsb] ORBIT DETECTED: {odesc}", flush=True)

            orbit_conn = sqlite3.connect(DB_PATH)
            ocur = orbit_conn.execute(
                "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, "
                "tgids, location, lat, lon, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
                (orbit_now, orbit_now, "AIR ASSET ORBIT", odesc,
                 '["APD"]', '[]', "Austin airspace", olat, olon)
            )
            orbit_inc_id = ocur.lastrowid
            orbit_conn.commit()
            orbit_conn.close()

            omsg = (
                f"\U0001f6a8 LEO HELICOPTER ORBITING: {olbl}{ocs}\n"
                f"Altitude: {int(oalt or 0)}ft | ICAO: {oicao}\n"
                f"Position: {olat:.4f}, {olon:.4f}\n"
                f"Aircraft circling — likely observing ground target."
            )
            opayload = json.dumps({"message": omsg}).encode()
            ocreds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
            oheaders = {"Authorization": f"Basic {ocreds}", "OCS-APIRequest": "true",
                        "Content-Type": "application/json"}
            for room in [TALK_ROOMS["apd"], TALK_ROOMS["incidents"]]:
                oreq = urllib.request.Request(
                    f"{TALK_BASE}/chat/{room}",
                    data=opayload, headers=oheaders, method="POST"
                )
                try:
                    urllib.request.urlopen(oreq, timeout=10)
                except Exception as e:
                    print(f"[adsb] orbit Talk post failed: {e}", flush=True)

            threading.Thread(
                target=send_dm_alert,
                args=("AIR ASSET ORBIT", odesc, "Austin airspace", "APD", "APD"),
                daemon=True
            ).start()

            threading.Thread(
                target=_atak_post_marker,
                args=(orbit_inc_id, olat, olon, "AIR ASSET ORBIT", "Austin airspace", odesc),
                daemon=True
            ).start()

# Austin Open Data — AFD Real-Time Fire Incidents poller
# ---------------------------------------------------------------------------

AFD_OPEN_DATA_URL = (
    "https://data.austintexas.gov/resource/wpu4-x69d.json"
    "?$where=traffic_report_status='ACTIVE'&$limit=50"
)
AFD_POLL_INTERVAL = 60  # seconds

# Maps issue_reported prefixes → our internal itype
_AFD_ITYPE_MAP = {
    "STRUCTURE":  "STRUCTURE FIRE",
    "FIRE":       "STRUCTURE FIRE",
    "GRASS":      "GRASS FIRE",
    "WILDLAND":   "GRASS FIRE",
    "AIRCRAFT":   "AIRCRAFT EMERGENCY",
    "HANGER":     "STRUCTURE FIRE",
    "HANGAR":     "STRUCTURE FIRE",
    "EXPLOSION":  "EXPLOSION",
    "HAZMAT":     "HAZMAT",
    "ALARM":      "FIRE ALARM",
    "ALARMM":     "FIRE ALARM",
}

_afd_active_ids: dict[str, dict] = {}   # report_id → AFD incident dict
_afd_lock = threading.Lock()


def _afd_issue_to_itype(issue: str) -> str:
    """Map AFD issue_reported string to a BB itype."""
    prefix = issue.split()[0].upper().rstrip("-")
    return _AFD_ITYPE_MAP.get(prefix, "FIRE/EMS DISPATCH")


def _afd_post_to_talk(incident: dict, itype: str, matched_bb_id: int | None):
    """Post an AFD Open Data incident to the fire-ems Talk room."""
    address  = incident.get("address", "Unknown address")
    issue    = incident.get("issue_reported", "Unknown")
    pub_dt   = incident.get("published_date", "")[:16].replace("T", " ")
    lat      = incident.get("latitude")
    lon      = incident.get("longitude")
    coords   = f" ({lat}, {lon})" if lat and lon else ""

    if matched_bb_id:
        msg = (
            f"📡 [AFD API CONFIRM] Scanner incident #{matched_bb_id} confirmed via city dispatch feed\n"
            f"📍 {address}{coords}\n"
            f"🚒 {issue} — dispatched {pub_dt}"
        )
    else:
        msg = (
            f"🚨 [AFD DISPATCH — scanner missed] {itype}\n"
            f"📍 {address}{coords}\n"
            f"🚒 {issue} — dispatched {pub_dt}"
        )

    payload = json.dumps({"message": msg}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}
    room_token = TALK_ROOMS["fire-ems"]
    url  = f"{TALK_BASE}/chat/{room_token}"
    req  = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[afd] posted to fire-ems: {issue} @ {address}", flush=True)
    except Exception as e:
        print(f"[afd] Talk post failed: {e}", flush=True)


def afd_open_data_thread():
    """Poll Austin Open Data for active AFD incidents and cross-reference with scanner."""
    print("[afd] AFD Open Data poller started", flush=True)
    while True:
        time.sleep(AFD_POLL_INTERVAL)
        try:
            req  = urllib.request.Request(AFD_OPEN_DATA_URL,
                                          headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                incidents = json.loads(resp.read())
        except Exception as e:
            print(f"[afd] fetch error: {e}", flush=True)
            continue

        now = time.time()
        with _afd_lock:
            current_ids = {inc["traffic_report_id"] for inc in incidents}

            # Detect incidents that just went ARCHIVED (were active, now gone)
            cleared = set(_afd_active_ids.keys()) - current_ids
            for rid in cleared:
                old = _afd_active_ids.pop(rid)
                print(f"[afd] CLEARED: {old.get('issue_reported')} @ {old.get('address')}", flush=True)
                afd_mid = old.get("atak_marker_id")
                if afd_mid is not None:
                    threading.Thread(target=_atak_clear_marker, args=(afd_mid,), daemon=True).start()

            # Process new active incidents
            for inc in incidents:
                rid = inc["traffic_report_id"]
                if rid in _afd_active_ids:
                    continue  # already processed

                _afd_active_ids[rid] = inc
                itype   = _afd_issue_to_itype(inc.get("issue_reported", ""))
                lat     = float(inc["latitude"])  if inc.get("latitude")  else None
                lon     = float(inc["longitude"]) if inc.get("longitude") else None
                address = inc.get("address", "")

                # Check if a scanner incident is already tracking this location
                matched_id = None
                if lat is not None and lon is not None:
                    with _incident_lock:
                        for iid, bb_inc in _active_incidents.items():
                            blat = bb_inc.get("lat")
                            blon = bb_inc.get("lon")
                            if blat is None or blon is None:
                                continue
                            if _haversine_km(lat, lon, blat, blon) < 0.5:
                                matched_id = iid
                                break

                print(f"[afd] NEW {'(matched #'+str(matched_id)+')' if matched_id else '(unmatched)'}: "
                      f"{inc.get('issue_reported')} @ {address}", flush=True)

                threading.Thread(
                    target=_afd_post_to_talk,
                    args=(inc, itype, matched_id),
                    daemon=True
                ).start()

                # If unmatched and has a real geocoded address, post an ATAK marker too
                if matched_id is None and address and lat is not None and lon is not None:
                    # Use a negative sentinel incident_id to avoid colliding with real ones
                    afd_marker_id = hash(rid) % 100000 * -1
                    _afd_active_ids[rid]["atak_marker_id"] = afd_marker_id
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(afd_marker_id, lat, lon, itype, address),
                        daemon=True
                    ).start()


# Austin Open Data — Real-Time Traffic Incidents poller
# ---------------------------------------------------------------------------

TRAFFIC_OPEN_DATA_URL = (
    "https://data.austintexas.gov/resource/dx9v-zd7x.json"
    "?$where=traffic_report_status='ACTIVE'&$limit=100"
)
TRAFFIC_POLL_INTERVAL = 60  # seconds

_TRAFFIC_ITYPE_MAP = {
    "CRASH":       "CRASH/COLLISION",
    "COLLISION":   "CRASH/COLLISION",
    "VEHICLE":     "CRASH/COLLISION",
    "MOTORCYCLE":  "CRASH/COLLISION",
    "BICYCLE":     "CRASH/COLLISION",
    "PEDESTRIAN":  "PEDESTRIAN INCIDENT",
    "STALLED":     "STALLED VEHICLE",
    "ABANDONED":   "ABANDONED VEHICLE",
    "ROAD":        "ROAD HAZARD",
    "DEBRIS":      "ROAD HAZARD",
    "FLOODING":    "FLOODING",
    "FLOODED":     "FLOODING",
    "SIGNAL":      "TRAFFIC SIGNAL ISSUE",
    "FIRE":        "VEHICLE FIRE",
    "HAZMAT":      "HAZMAT",
    "SPILL":       "HAZMAT",
    "BRIDGE":      "ROAD HAZARD",
    "ANIMAL":      "ROAD HAZARD",
}

# Types worth posting to Talk (suppress stalls/abandoned to reduce noise)
_TRAFFIC_TALK_ITYPES = {
    "CRASH/COLLISION", "PEDESTRIAN INCIDENT", "FLOODING",
    "VEHICLE FIRE", "HAZMAT", "ROAD HAZARD",
}

_traffic_active_ids: dict[str, dict] = {}
_traffic_lock = threading.Lock()


def _traffic_issue_to_itype(issue: str) -> str:
    """Map traffic issue_reported string to a BB itype."""
    prefix = issue.split()[0].upper().rstrip("-")
    return _TRAFFIC_ITYPE_MAP.get(prefix, "TRAFFIC INCIDENT")


def _traffic_post_to_talk(incident: dict, itype: str, matched_bb_id: int | None):
    """Post a traffic incident to the incidents Talk room."""
    address = incident.get("address", "Unknown address")
    issue   = incident.get("issue_reported", "Unknown")
    pub_dt  = incident.get("published_date", "")[:16].replace("T", " ")
    agency  = incident.get("agency", "").strip()
    lat     = incident.get("latitude")
    lon     = incident.get("longitude")
    coords  = f" ({lat}, {lon})" if lat and lon else ""

    if matched_bb_id:
        msg = (
            f"[TRAFFIC API CONFIRM] Scanner incident #{matched_bb_id} confirmed via city feed\n"
            f"Address: {address}{coords}\n"
            f"Type: {issue} ({agency}) - dispatched {pub_dt}"
        )
    else:
        msg = (
            f"[TRAFFIC DISPATCH] {itype}\n"
            f"Address: {address}{coords}\n"
            f"Type: {issue} ({agency}) - dispatched {pub_dt}"
        )

    payload = json.dumps({"message": msg}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}
    room_token = TALK_ROOMS["incidents"]
    url  = f"{TALK_BASE}/chat/{room_token}"
    req  = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[traffic] posted to incidents: {issue} @ {address}", flush=True)
    except Exception as e:
        print(f"[traffic] Talk post failed: {e}", flush=True)


def traffic_open_data_thread():
    """Poll Austin Open Data for active traffic incidents and cross-reference with scanner."""
    print("[traffic] Traffic Open Data poller started", flush=True)
    while True:
        time.sleep(TRAFFIC_POLL_INTERVAL)
        try:
            req = urllib.request.Request(TRAFFIC_OPEN_DATA_URL,
                                         headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                incidents = json.loads(resp.read())
        except Exception as e:
            print(f"[traffic] fetch error: {e}", flush=True)
            continue

        with _traffic_lock:
            current_ids = {inc["traffic_report_id"] for inc in incidents}

            # Detect incidents that just went ARCHIVED (were active, now gone)
            cleared = set(_traffic_active_ids.keys()) - current_ids
            for rid in cleared:
                old = _traffic_active_ids.pop(rid)
                print(f"[traffic] CLEARED: {old.get('issue_reported')} @ {old.get('address')}", flush=True)
                t_mid = old.get("atak_marker_id")
                if t_mid is not None:
                    threading.Thread(target=_atak_clear_marker, args=(t_mid,), daemon=True).start()

            # Process new active incidents
            for inc in incidents:
                rid = inc["traffic_report_id"]
                if rid in _traffic_active_ids:
                    continue  # already processed

                _traffic_active_ids[rid] = inc
                itype   = _traffic_issue_to_itype(inc.get("issue_reported", ""))
                lat     = float(inc["latitude"])  if inc.get("latitude")  else None
                lon     = float(inc["longitude"]) if inc.get("longitude") else None
                address = inc.get("address", "")

                if lat is None or lon is None:
                    print(f"[traffic] skipping (no coords): {inc.get('issue_reported')} @ {address}", flush=True)
                    continue

                # Cross-reference against active scanner incidents
                matched_id = None
                with _incident_lock:
                    for iid, bb_inc in _active_incidents.items():
                        blat = bb_inc.get("lat")
                        blon = bb_inc.get("lon")
                        if blat is None or blon is None:
                            continue
                        if _haversine_km(lat, lon, blat, blon) < 0.5:
                            matched_id = iid
                            break

                print(f"[traffic] NEW {'(matched #'+str(matched_id)+')' if matched_id else '(unmatched)'}: "
                      f"{inc.get('issue_reported')} @ {address}", flush=True)

                # Post to Talk for significant types or scanner cross-references
                if itype in _TRAFFIC_TALK_ITYPES or matched_id is not None:
                    threading.Thread(
                        target=_traffic_post_to_talk,
                        args=(inc, itype, matched_id),
                        daemon=True
                    ).start()

                # ATAK marker for all unmatched incidents
                # Offset range -(100001..200000) avoids collision with AFD range -(0..99999)
                if matched_id is None:
                    t_marker_id = -(abs(hash(rid)) % 100000) - 100001
                    _traffic_active_ids[rid]["atak_marker_id"] = t_marker_id
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(t_marker_id, lat, lon, itype, address),
                        daemon=True
                    ).start()



# ---------------------------------------------------------------------------
# ATXFloods — Low-water-crossing closures poller (api.atxfloods.com)
# ---------------------------------------------------------------------------

ATXFLOODS_URL = "https://api.atxfloods.com/api/crossings"
ATXFLOODS_POLL_INTERVAL = 300  # 5 minutes

_atxfloods_state: dict[int, dict] = {}
_atxfloods_lock = threading.Lock()


def _atxfloods_post_to_talk(crossing: dict, new_status: str, old_status):
    name    = crossing.get("name", "?")
    jur     = crossing.get("jurisdiction", "?")
    addr    = crossing.get("address", "")
    lat     = crossing.get("lat")
    lon     = crossing.get("lon")
    coords  = f" ({lat}, {lon})" if lat and lon else ""
    comment = (crossing.get("comment") or "").strip()
    verb    = {"closed": "CLOSED", "caution": "CAUTION", "open": "REOPENED"}.get(
        new_status, new_status.upper()
    )
    lines = [f"[FLOODING {verb}] {name} ({jur})", f"{addr}{coords}"]
    if comment:
        lines.append(f"Note: {comment}")
    if old_status:
        lines.append(f"State: {old_status} -> {new_status}")
    msg = "\n".join(lines)

    payload = json.dumps({"message": msg}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}
    room_token = TALK_ROOMS["incidents"]
    url  = f"{TALK_BASE}/chat/{room_token}"
    req  = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[atxfloods] posted: {verb} {name}", flush=True)
    except Exception as e:
        print(f"[atxfloods] Talk post failed: {e}", flush=True)


def atxfloods_thread():
    """Poll ATXFloods and alert on state transitions (first sighting silent)."""
    print("[atxfloods] ATXFloods poller started", flush=True)
    while True:
        try:
            req = urllib.request.Request(ATXFLOODS_URL,
                                         headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read())
        except Exception as e:
            print(f"[atxfloods] fetch error: {e}", flush=True)
            time.sleep(ATXFLOODS_POLL_INTERVAL)
            continue

        crossings = payload.get("attributes", []) if isinstance(payload, dict) else []
        if not crossings:
            print("[atxfloods] empty response", flush=True)
            time.sleep(ATXFLOODS_POLL_INTERVAL)
            continue

        transitions = 0
        with _atxfloods_lock:
            for c in crossings:
                try:
                    cid = int(c["id"])
                except (KeyError, ValueError, TypeError):
                    continue
                status = (c.get("status") or "").lower()
                if status not in ("open", "closed", "caution"):
                    continue

                prev = _atxfloods_state.get(cid)
                if prev is None:
                    # First sighting — seed state silently, no alert, no marker
                    _atxfloods_state[cid] = {"status": status, "marker_id": None}
                    continue
                if prev["status"] == status:
                    continue

                old_status = prev["status"]
                prev["status"] = status
                transitions += 1
                _atxfloods_post_to_talk(c, status, old_status)

                try:
                    lat = float(c["lat"]); lon = float(c["lon"])
                except (KeyError, ValueError, TypeError):
                    lat = lon = None

                if status == "open" and prev.get("marker_id") is not None:
                    threading.Thread(target=_atak_clear_marker,
                                     args=(prev["marker_id"],), daemon=True).start()
                    prev["marker_id"] = None
                elif status in ("closed", "caution") and lat is not None and lon is not None:
                    marker_id = -(abs(cid) % 100000) - 200001
                    prev["marker_id"] = marker_id
                    label = f"{c.get('name','')} {c.get('address','')}".strip()
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(marker_id, lat, lon, "FLOODING", label),
                        daemon=True,
                    ).start()

        if transitions:
            print(f"[atxfloods] {transitions} state transition(s) this cycle", flush=True)
        time.sleep(ATXFLOODS_POLL_INTERVAL)




# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Austin major events — weekly "this week in Austin" digest
# ---------------------------------------------------------------------------
# Reads /opt/battlebuddy/austin_major_events.json and posts a summary to the
# incidents Talk room when events are within 7 days. File is re-read every
# poll cycle — edits take effect without a service restart.
# ---------------------------------------------------------------------------

AUSTIN_EVENTS_JSON   = "/opt/battlebuddy/austin_major_events.json"
AUSTIN_EVENTS_STATE  = "/opt/battlebuddy/austin_events_state.json"
AUSTIN_EVENTS_POLL   = 6 * 3600   # 6 hours
AUSTIN_EVENTS_WINDOW = 7          # days


def _austin_events_load():
    try:
        with open(AUSTIN_EVENTS_JSON) as fh:
            return json.load(fh)
    except Exception as e:
        print(f"[events] load failed: {e}", flush=True)
        return {"events": []}


def _austin_events_upcoming(doc, today):
    from datetime import date as _date
    horizon = today + timedelta(days=AUSTIN_EVENTS_WINDOW)
    out = []
    for ev in doc.get("events", []):
        try:
            s_ = _date.fromisoformat(ev["start"])
            e_ = _date.fromisoformat(ev.get("end") or ev["start"])
        except Exception:
            continue
        if s_ <= horizon and e_ >= today:
            out.append(ev)
    out.sort(key=lambda x: x.get("start", ""))
    return out


def _austin_events_state_load():
    try:
        with open(AUSTIN_EVENTS_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {"last_post_date": None, "last_event_ids": []}


def _austin_events_state_save(state):
    try:
        with open(AUSTIN_EVENTS_STATE, "w") as fh:
            json.dump(state, fh)
    except Exception as e:
        print(f"[events] state save failed: {e}", flush=True)


def _austin_events_format(events, today):
    if not events:
        return None
    lines = [f"📅 This week in Austin (window: {today.isoformat()} + {AUSTIN_EVENTS_WINDOW} days):"]
    for ev in events:
        start = ev.get("start", "?")
        end   = ev.get("end") or start
        rng   = start if end == start else f"{start} → {end}"
        extras = []
        tier = ev.get("tier")
        if tier == "major":
            extras.append("MAJOR regional impact")
        elif tier == "large":
            extras.append("large impact")
        if ev.get("blast_radius_mi"):
            extras.append(f"{ev['blast_radius_mi']}mi radius")
        if ev.get("venue"):
            extras.append(ev["venue"])
        tail = f"  ({', '.join(extras)})" if extras else ""
        lines.append(f"  • {rng}  {ev.get('name','?')}{tail}")
    return "\n".join(lines)


def _austin_events_post_to_talk(msg):
    payload = json.dumps({"message": msg}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}
    url = f"{TALK_BASE}/chat/{TALK_ROOMS['incidents']}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        print("[events] weekly summary posted", flush=True)
    except Exception as e:
        print(f"[events] Talk post failed: {e}", flush=True)


def austin_events_thread():
    """Post a 'this week in Austin' digest when the 7-day window changes
    or when >=7 days have passed since the last post."""
    print("[events] Austin major events poller started", flush=True)
    try:
        import zoneinfo
        austin_tz = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        austin_tz = None
    from datetime import datetime as _dt, date as _date
    while True:
        try:
            now_austin = _dt.now(austin_tz) if austin_tz else _dt.now()
            today = now_austin.date()
            doc = _austin_events_load()
            events = _austin_events_upcoming(doc, today)
            state = _austin_events_state_load()
            current_ids = [e.get("id") for e in events]
            last_date_s = state.get("last_post_date")
            last_ids    = state.get("last_event_ids", [])
            days_since = None
            if last_date_s:
                try:
                    days_since = (today - _date.fromisoformat(last_date_s)).days
                except Exception:
                    days_since = None

            should_post = False
            reason = None
            if events and current_ids != last_ids:
                should_post = True
                reason = "event list changed"
            elif events and (days_since is None or days_since >= 7):
                should_post = True
                reason = "weekly cadence"

            if should_post:
                msg = _austin_events_format(events, today)
                if msg:
                    _austin_events_post_to_talk(msg)
                    _austin_events_state_save({
                        "last_post_date": today.isoformat(),
                        "last_event_ids": current_ids,
                    })
                    print(f"[events] posted ({reason}): {len(events)} events", flush=True)
            else:
                print(f"[events] quiet: {len(events)} events in window, "
                      f"last posted {last_date_s} ({days_since}d ago)", flush=True)
        except Exception as e:
            print(f"[events] cycle error: {e}", flush=True)
        time.sleep(AUSTIN_EVENTS_POLL)


# ---------------------------------------------------------------------------
# Austin PD CAD — Retrospective enrichment poller
# ---------------------------------------------------------------------------
# Polls the APD Computer Aided Dispatch open data feed (~2 week lag) and
# cross-references it against scanner incidents to:
#   1. Enrich incidents with CAD final description, mental health flag,
#      disposition, sector, and council district
#   2. Harvest TGID→sector hints for unknown talkgroup identification
# ---------------------------------------------------------------------------

APD_CAD_URL = (
    "https://data.austintexas.gov/resource/22de-7rzg.json"
    "?$where=response_datetime>{lookback}"
    "&$order=response_datetime+DESC"
    "&$limit=5000"
)
APD_CAD_POLL_INTERVAL = 6 * 3600   # every 6 hours
APD_CAD_LOOKBACK_DAYS = 21  # dataset lags ~15 days; 21 gives comfortable headroom

# Maps CAD initial_problem_category → BB itype (for match confidence scoring)
_CAD_CATEGORY_MAP = {
    "Shoot/Stab":                  "SHOOTING",
    "Homicide":                    "SHOOTING",
    "Aggravated Assault":          "STABBING",
    "Weapons/Firearms Violations": "WEAPONS",
    "Robbery":                     "WEAPONS",
    "Bomb/Explosives":             "EXPLOSION",
    "Arson":                       "STRUCTURE FIRE",
    "Crashes":                     "CRASH/COLLISION",
    "Traffic Stop/Hazard":         "CRASH/COLLISION",
    "DUI/DWI":                     "CRASH/COLLISION",
    "Evading/Resisting Arrest":    "PURSUIT",
}

# Categories worth harvesting TGIDs for (skip noise categories)
_CAD_HARVEST_CATEGORIES = {
    "Shoot/Stab", "Homicide", "Aggravated Assault",
    "Weapons/Firearms Violations", "Robbery", "Bomb/Explosives",
    "Arson", "Crashes", "Evading/Resisting Arrest",
}


def _cad_init_db():
    """Create apd_cad and tgid_sector_hints tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS apd_cad (
            incident_number      TEXT PRIMARY KEY,
            response_ts          REAL,
            call_closed_ts       REAL,
            sector               TEXT,
            council_district     TEXT,
            priority_level       TEXT,
            initial_description  TEXT,
            initial_category     TEXT,
            final_description    TEXT,
            final_category       TEXT,
            mental_health_flag   TEXT,
            disposition          TEXT,
            geoid                TEXT,
            matched_incident_id  INTEGER,
            match_confidence     TEXT,
            fetched_ts           REAL
        );
        CREATE TABLE IF NOT EXISTS tgid_sector_hints (
            tgid        INTEGER,
            sector      TEXT,
            hit_count   INTEGER DEFAULT 1,
            last_seen   REAL,
            PRIMARY KEY (tgid, sector)
        );
        CREATE INDEX IF NOT EXISTS idx_apd_cad_response_ts
            ON apd_cad(response_ts);
        CREATE INDEX IF NOT EXISTS idx_apd_cad_unmatched
            ON apd_cad(matched_incident_id)
            WHERE matched_incident_id IS NULL;
    """)
    conn.commit()
    conn.close()
    print("[cad] DB tables ready", flush=True)


def _cad_fetch_and_store():
    """Fetch CAD records from the last 14 days and upsert into apd_cad."""
    lookback_dt = (datetime.now(timezone.utc) - timedelta(days=APD_CAD_LOOKBACK_DAYS))
    lookback_str = lookback_dt.strftime("'%Y-%m-%dT%H:%M:%S'")
    url = APD_CAD_URL.format(lookback=lookback_str)

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            records = json.loads(resp.read())
    except Exception as e:
        print(f"[cad] fetch error: {e}", flush=True)
        return 0

    def parse_ts(dt_str):
        if not dt_str:
            return None
        try:
            return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=None).timestamp() - time.timezone
        except Exception:
            return None

    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    upserted = 0
    for r in records:
        incident_number = r.get("incident_number")
        if not incident_number:
            continue
        conn.execute("""
            INSERT INTO apd_cad
                (incident_number, response_ts, call_closed_ts, sector,
                 council_district, priority_level, initial_description,
                 initial_category, final_description, final_category,
                 mental_health_flag, disposition, geoid, fetched_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(incident_number) DO UPDATE SET
                final_description = excluded.final_description,
                final_category    = excluded.final_category,
                disposition       = excluded.disposition,
                fetched_ts        = excluded.fetched_ts
        """, (
            incident_number,
            parse_ts(r.get("response_datetime")),
            parse_ts(r.get("call_closed_datetime")),
            r.get("sector"),
            r.get("council_district"),
            r.get("priority_level"),
            r.get("initial_problem_description"),
            r.get("initial_problem_category"),
            r.get("final_problem_description"),
            r.get("final_problem_category"),
            r.get("mental_health_flag"),
            r.get("call_disposition_description"),
            r.get("geoid"),
            now,
        ))
        upserted += 1
    conn.commit()
    conn.close()
    print(f"[cad] upserted {upserted} records ({len(records)} fetched)", flush=True)
    return upserted


def _cad_match_and_harvest():
    """
    Match unmatched CAD records against scanner incidents.
    On match: enrich the incident row and harvest TGID→sector hints.
    """
    MATCH_WINDOW = 600   # ±10 minutes in seconds
    TGID_WINDOW_PRE  = 300  # seconds before CAD response_ts to include calls
    TGID_WINDOW_POST = 120  # seconds after call_closed_ts to include calls

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Fetch unmatched CAD records that have been in the DB long enough to have
    # corresponding scanner data (response_ts < now - 2h to avoid partial incidents)
    cutoff = time.time() - 7200
    cad_rows = conn.execute("""
        SELECT * FROM apd_cad
        WHERE matched_incident_id IS NULL
          AND response_ts IS NOT NULL
          AND response_ts < ?
        ORDER BY response_ts DESC
        LIMIT 2000
    """, (cutoff,)).fetchall()

    # Pre-load scanner incidents already claimed by a prior CAD match
    # so we enforce one CAD row per scanner incident.
    claimed_ids = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT matched_incident_id FROM apd_cad "
            "WHERE matched_incident_id IS NOT NULL"
        ).fetchall()
    }

    matched = 0
    harvested_hints = 0

    for cad in cad_rows:
        response_ts  = cad["response_ts"]
        sector       = cad["sector"]
        init_cat     = cad["initial_category"] or ""
        bb_itype     = _CAD_CATEGORY_MAP.get(init_cat)
        call_closed  = cad["call_closed_ts"] or (response_ts + 1800)

        # Find scanner incidents within time window where APD was involved
        candidates = conn.execute("""
            SELECT id, itype, agencies, ts_start FROM incidents
            WHERE ts_start BETWEEN ? AND ?
              AND agencies LIKE '%APD%'
              AND is_test = 0
            ORDER BY ABS(ts_start - ?) ASC
            LIMIT 5
        """, (response_ts - MATCH_WINDOW, response_ts + MATCH_WINDOW, response_ts)
        ).fetchall()

        best_match_id   = None
        best_confidence = None

        for inc in candidates:
            if inc["id"] in claimed_ids:
                continue
            inc_itype = inc["itype"] or ""
            # High confidence: type matches
            if bb_itype and inc_itype == bb_itype:
                best_match_id   = inc["id"]
                best_confidence = "high"
                break
            # Time-only confidence: within window, right agency, no type match
            if best_match_id is None:
                best_match_id   = inc["id"]
                best_confidence = "time_only"

        # Update CAD record with match result
        # Unique index on matched_incident_id prevents two CAD rows claiming the same incident.
        # Catch constraint violation and treat as no-match for this CAD record.
        if best_match_id:
            try:
                conn.execute("""
                    UPDATE apd_cad
                    SET matched_incident_id = ?, match_confidence = ?
                    WHERE incident_number = ?
                """, (best_match_id, best_confidence, cad["incident_number"]))
                matched += 1
                claimed_ids.add(best_match_id)
            except sqlite3.IntegrityError:
                best_match_id = None
                conn.execute("""
                    UPDATE apd_cad SET matched_incident_id = NULL, match_confidence = NULL
                    WHERE incident_number = ?
                """, (cad["incident_number"],))
        else:
            conn.execute("""
                UPDATE apd_cad
                SET matched_incident_id = NULL, match_confidence = NULL
                WHERE incident_number = ?
            """, (cad["incident_number"],))
            # Enrich the scanner incident only on high-confidence matches
            if best_confidence == "high":
                conn.execute("""
                    UPDATE incidents SET
                        description = description || ' [CAD: ' || ? || ', ' || ? || ', sector ' || ? || ']'
                    WHERE id = ? AND description NOT LIKE '%[CAD:%'
                """, (
                    cad["final_description"] or cad["initial_description"] or "",
                    cad["disposition"] or "",
                    sector or "?",
                    best_match_id,
                ))

        # Harvest TGID hints regardless of incident match, for worthwhile categories
        if sector and init_cat in _CAD_HARVEST_CATEGORIES:
            tgid_window_start = response_ts - TGID_WINDOW_PRE
            tgid_window_end   = call_closed + TGID_WINDOW_POST
            tgid_rows = conn.execute("""
                SELECT tgid, COUNT(*) as call_count
                FROM calls
                WHERE ts BETWEEN ? AND ?
                  AND tgid IS NOT NULL
                  AND tgid > 0
                GROUP BY tgid
                HAVING call_count >= 2
            """, (tgid_window_start, tgid_window_end)).fetchall()

            for tr in tgid_rows:
                tgid = tr["tgid"]
                # Skip already-tagged/ignored TGIDs — harvest is for unknown discovery
                if tgid in TGID_META or tgid in IGNORE_TGIDS:
                    continue
                conn.execute("""
                    INSERT INTO tgid_sector_hints (tgid, sector, hit_count, last_seen)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(tgid, sector) DO UPDATE SET
                        hit_count = hit_count + 1,
                        last_seen = excluded.last_seen
                """, (tgid, sector, response_ts))
                harvested_hints += 1

    conn.commit()
    conn.close()
    print(f"[cad] match run: {matched}/{len(cad_rows)} matched, "
          f"{harvested_hints} TGID hints harvested", flush=True)


def apd_cad_thread():
    """Retrospective CAD enrichment — poll every 6 hours, match and harvest."""
    print("[cad] APD CAD enrichment poller started", flush=True)
    _cad_init_db()
    while True:
        _cad_fetch_and_store()
        _cad_match_and_harvest()
        time.sleep(APD_CAD_POLL_INTERVAL)



# ---------------------------------------------------------------------------
# Sitrep generator
# ---------------------------------------------------------------------------

def build_sitrep(minutes=60) -> str:
    calls     = calls_for_sitrep(minutes)
    incidents = [i for i in active_incidents() if not i.get("is_test")]

    lines = [
        f"SITUATION REPORT — last {minutes} min — {datetime.now(_CDT).strftime('%Y-%m-%d %H:%M %Z')}",
        f"Total calls: {len(calls)}",
    ]

    # --- Active incidents (real only) ---
    if incidents:
        lines.append("")
        lines.append("*** ACTIVE INCIDENTS ***")
        for inc in incidents:
            age     = int((time.time() - inc["ts_start"]) / 60)
            updated = int((time.time() - inc["ts_updated"]) / 60)
            agencies = ", ".join(json.loads(inc["agencies"] or "[]"))
            loc = f" @ {inc['location']}" if inc.get("location") else ""
            lines.append(
                f"  [{inc['itype']}]{loc} — started {age}m ago, "
                f"last activity {updated}m ago — agencies: {agencies}"
            )
            lines.append(f"  {inc['description']}")
        lines.append("*** END ACTIVE INCIDENTS ***")
    else:
        lines.append("  No active incidents.")

    if not calls:
        lines.append(f"\nNo calls in the last {minutes} minutes.")
        return "\n".join(lines)

    # --- HIGH priority calls ---
    high_calls = [
        c for c in calls
        if (c.get("groq") or {}).get("priority") == "HIGH"
        or any(k in (c.get("transcript") or "").lower() for k in _HIGH_KW)
    ]
    if high_calls:
        lines.append("")
        lines.append("*** HIGH PRIORITY ***")
        for c in high_calls[:10]:
            ts  = datetime.fromtimestamp(c["ts"]).strftime("%H:%M")
            loc = f" @ {c['location']}" if c.get("location") else ""
            txt = (c.get("transcript") or "(no transcript)")[:150]
            groq_desc = (c.get("groq") or {}).get("description", "")
            lines.append(f"  🔴 {ts} {c['tag'] or c['tgid']}{loc}: {txt}")
            if groq_desc:
                lines.append(f"     → {groq_desc}")
        lines.append("")

    # --- MED priority calls ---
    med_calls = [
        c for c in calls
        if c not in high_calls
        and (
            (c.get("groq") or {}).get("priority") == "MED"
            or any(k in (c.get("transcript") or "").lower() for k in _MED_KW)
        )
    ]
    if med_calls:
        lines.append("*** NOTABLE ***")
        for c in med_calls[:10]:
            ts  = datetime.fromtimestamp(c["ts"]).strftime("%H:%M")
            loc = f" @ {c['location']}" if c.get("location") else ""
            txt = (c.get("transcript") or "(no transcript)")[:120]
            lines.append(f"  🟡 {ts} {c['tag'] or c['tgid']}{loc}: {txt}")
        lines.append("")

    # --- Call volume by agency ---
    by_cat: dict[str, int] = {}
    for c in calls:
        cat = c["category"] or "Unknown"
        by_cat[cat] = by_cat.get(cat, 0) + 1

    lines.append("*** CALL VOLUME ***")
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Nextcloud Talk poster
# ---------------------------------------------------------------------------

# Unit/callsign patterns common in P25 traffic
_UNIT_PATTERNS = [
    re.compile(r'\b((?:engine|truck|medic|rescue|battalion|squad|ladder|unit)\s+\d{1,3})\b', re.I),
    re.compile(r'\b([A-Z][a-z]+\s+\d{1,3})\b'),          # Adam 21, Baker 45
    re.compile(r'\b([A-Z]-?\d{2,3})\b'),                  # A-21, B45
    re.compile(r'\bunit[s]?\s+(\d{1,4})\b', re.I),
]

_HIGH_PRIORITY = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "AIRCRAFT EMERGENCY",
    "MASS CASUALTY", "STRUCTURE FIRE", "HOSTAGE/BARRICADE",
}
_MED_PRIORITY = {
    "CRASH/COLLISION", "HAZMAT", "FIRE DISPATCH",
    "MULTI-AGENCY RESPONSE", "APD SURGE", "TRANSIT INCIDENT", "AIRPORT EMERGENCY",
}
_HIGH_KW = ["officer down", "shots fired", "shooting", "stabbing",
            "structure fire", "mass casualty", "hostage", "barricade", "10-99",
            "homicide", "body found", "found dead", "death investigation", "medical examiner"]
_MED_KW  = ["crash", "collision", "hazmat", "fire", "rollover", "working fire"]


def _extract_units(transcript: str) -> list[str]:
    found, seen = [], set()
    for pat in _UNIT_PATTERNS:
        for m in pat.finditer(transcript):
            unit = m.group(1).strip()
            key  = unit.lower()
            if key not in seen:
                seen.add(key)
                found.append(unit)
    return found[:6]


def post_to_talk(call: dict):
    if not TALK_ENABLED:
        return

    ts         = datetime.fromtimestamp(call["ts"]).strftime("%H:%M")
    tag        = call.get("tag") or f"TGID {call.get('tgid')}"
    cat        = call.get("category", "Unknown")
    loc        = f" @ {call['location']}" if call.get("location") else ""
    transcript = call.get("transcript") or "(no transcript)"
    tgid       = call.get("tgid")
    text_lower = transcript.lower()

    # Only post to Talk for genuinely high-danger calls — officer down, shots fired, etc.
    # Incident-level alerts are handled by send_dm_alert when an incident is created.
    # This prevents routine chatter from flooding the Talk room.
    groq_pri_early = (call.get("groq") or {}).get("priority", "NONE")
    has_high_kw = any(k in text_lower for k in _HIGH_KW)
    if groq_pri_early != "HIGH" and not has_high_kw:
        return

    # --- Incident linkage ---
    incident_line = ""
    matched_itype = None
    with _incident_lock:
        for inc in _active_incidents.values():
            if tgid in inc.get("tgids", set()) or cat in inc.get("agencies", set()):
                age = int((time.time() - inc["ts_updated"]) / 60)
                matched_itype = inc["itype"]
                incident_line = f"\n⚡ INCIDENT: {inc['itype']} — active {age}m"
                break

    # All calls reaching this point are high-priority by definition (gated above)
    priority = "🔴"

    # --- Unit extraction ---
    units = _extract_units(transcript)
    units_line = f"\nUnits: {', '.join(units)}" if units else ""

    # --- Air asset context ---
    air_line = ""
    air_context = detect_air_asset(tgid, transcript, cat)
    if air_context:
        air_line = f"\n🚁 AIR: {air_context}"

    # --- DPS asset/Capitol context ---
    dps_line = ""
    if cat == "DPS" or mentions_dps(transcript):
        assets = detect_dps_assets(transcript)
        capitol = is_capitol_area(transcript, call.get("location"))
        parts = []
        if assets:
            parts.append(", ".join(assets))
        if capitol:
            parts.append("Capitol area")
        if parts:
            dps_line = f"\n🏛 DPS: {' — '.join(parts)}"

    message = (
        f"{priority} [{ts}] {cat} — {tag}{loc}"
        f"{incident_line}"
        f"{air_line}"
        f"{dps_line}"
        f"{units_line}"
        f"\n\"{transcript}\""
    )

    payload = json.dumps({"message": message}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}

    for room_token in _room_for_call(call, priority):
        url = f"{TALK_BASE}/chat/{room_token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f"[talk] posted {priority} {tag} → {room_token}: {transcript[:50]}", flush=True)
        except Exception as e:
            print(f"[talk] post failed → {room_token}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Announcement banner — site-wide breaking alert for the most serious incidents
# ---------------------------------------------------------------------------

BANNER_BASE = "https://kevcloud.ddns.net/index.php/apps/announcementbanner/banners"

# Only these incident types trigger a site-wide banner
BANNER_ITYPES = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "MASS CASUALTY",
    "STRUCTURE FIRE", "HOSTAGE/BARRICADE", "AIRCRAFT EMERGENCY",
    "AIR ASSET ACTIVE",
}

_active_banner_id: str | None = None
_banner_lock = threading.Lock()


def _banner_api(path: str = "", data: dict | None = None, method: str | None = None):
    if method is None:
        method = "POST" if data is not None else "GET"
    url = BANNER_BASE + (f"/{path}" if path else "")
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/json"},
        method=method
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def post_banner(itype: str, location: str | None, agencies: str):
    """Post a site-wide breaking banner for serious incidents."""
    global _active_banner_id
    if itype not in BANNER_ITYPES:
        return
    loc_str = f" @ {location}" if location else ""
    message  = f"🔴 BREAKING: {itype}{loc_str} — {agencies} responding"
    with _banner_lock:
        try:
            # Remove previous banner if one exists
            if _active_banner_id:
                _banner_api(_active_banner_id, method="DELETE")
            result = _banner_api(data={
                "enabled": True, "message": message, "variant": "danger",
                "dismissible": False, "readMoreText": "", "readMoreUrl": "",
                "scheduleStart": "", "scheduleEnd": "",
                "audienceTarget": "all", "audienceGroups": [],
                "targetAppMode": "all", "targetApps": [],
            })
            _active_banner_id = result.get("id")
            print(f"[banner] posted: {message}", flush=True)
        except Exception as e:
            print(f"[banner] failed: {e}", flush=True)


def clear_banner(itype: str):
    """Remove the site-wide banner when an incident clears."""
    global _active_banner_id
    if itype not in BANNER_ITYPES:
        return
    with _banner_lock:
        if _active_banner_id:
            try:
                _banner_api(_active_banner_id, method="DELETE")
                print(f"[banner] cleared for {itype}", flush=True)
                _active_banner_id = None
            except Exception as e:
                print(f"[banner] clear failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# DM alerts — push 🔴 incidents directly to subscribed users
# ---------------------------------------------------------------------------

_dm_room_cache: dict[str, str] = {}   # username → 1:1 room token


def _get_or_create_dm_room(username: str) -> str | None:
    """Return the Talk 1:1 room token for a user, creating it if needed."""
    if username in _dm_room_cache:
        return _dm_room_cache[username]
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    # Room creation requires API v4; chat posting uses v1 (TALK_BASE)
    room_url = TALK_BASE.replace("/api/v1", "/api/v4") + "/room"
    payload = urllib.parse.urlencode({"roomType": 1, "invite": username}).encode()
    req = urllib.request.Request(
        room_url,
        data=payload,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    try:
        raw = urllib.request.urlopen(req, timeout=10).read().decode()
        # Response is XML — extract <token> element
        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw)
        token = root.findtext(".//token")
        if token:
            _dm_room_cache[username] = token
        return token
    except Exception as e:
        print(f"[dm] failed to get room for {username}: {e}", flush=True)
        return None


def send_dm_alert(itype: str, description: str, location: str | None,
                  agencies: str, category: str):
    """Send a 🔴 DM alert to all subscribed users."""
    subscribers = get_subscribers(itype, category)
    if not subscribers:
        return
    loc_str = f" @ {location}" if location else ""
    message = (
        f"🔴 BREAKING — {itype}{loc_str}\n"
        f"Agencies: {agencies}\n"
        f"{description}"
    )
    for username in subscribers:
        token = _get_or_create_dm_room(username)
        if token:
            threading.Thread(target=_bot_reply, args=(token, message),
                             daemon=True).start()
            print(f"[dm] alerted {username}: {itype}", flush=True)


# ---------------------------------------------------------------------------
# Deck integration — auto-create incident cards
# ---------------------------------------------------------------------------

def create_deck_card(incident: dict):
    """Create a Deck card in the 🆕 New column when a new incident is detected."""
    itype    = incident.get("itype", "INCIDENT")
    desc     = incident.get("description", "")
    location = incident.get("location")
    agencies = ", ".join(json.loads(incident.get("agencies") or "[]"))
    ts       = datetime.fromtimestamp(incident.get("ts_start", time.time())).strftime("%H:%M")

    title = f"{itype}"
    if location:
        title += f" @ {location}"

    body = (
        f"**Time:** {ts}\n"
        f"**Agencies:** {agencies or 'unknown'}\n"
        f"**Details:** {desc}\n"
    )

    label_id = DECK_LABELS.get(itype, DECK_LABELS.get("SHOOTING"))  # fallback
    creds    = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers  = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    # Create the card
    card_url = f"{DECK_BASE}/boards/{DECK_BOARD_ID}/stacks/{DECK_STACK_NEW}/cards"
    card_data = json.dumps({"title": title, "type": "plain", "order": 0,
                            "description": body}).encode()
    try:
        req  = urllib.request.Request(card_url, data=card_data, headers=headers, method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        card_id = resp.get("id")
        print(f"[deck] card created: {title} (id={card_id})", flush=True)

        # Assign label if matched
        if label_id and card_id:
            label_url  = f"{DECK_BASE}/boards/{DECK_BOARD_ID}/stacks/{DECK_STACK_NEW}/cards/{card_id}/assignLabel"
            label_data = json.dumps({"labelId": label_id}).encode()
            req = urllib.request.Request(label_url, data=label_data, headers=headers, method="PUT")
            urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[deck] card creation failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder="/opt/battlebuddy/static", static_url_path="/static")


@app.route("/receive", methods=["POST"])
def receive():
    data = request.get_json(force=True)
    if not data or "audio_b64" not in data:
        return jsonify({"error": "missing audio_b64"}), 400

    tgid = int(data.get("tgid", 0))

    # Drop non-public-safety talkgroups — don't waste Whisper on them
    if tgid in IGNORE_TGIDS:
        return jsonify({"status": "ignored"}), 202

    wav_bytes = base64.b64decode(data["audio_b64"])

    # Prefer tag from Pi 1 (already resolved by OP25), fall back to TSV
    tag      = data.get("tag") or TGID_META.get(tgid, {}).get("tag") or f"TGID {tgid}"
    node     = data.get("node", "unknown")
    ts       = time.time()
    meta     = TGID_META.get(tgid, {})
    category = meta.get("cat", "Unknown")
    def_lat  = meta.get("lat")
    def_lon  = meta.get("lon")

    try:
        with __import__('wave').open(__import__('io').BytesIO(wav_bytes)) as wf:
            duration = wf.getnframes() / wf.getframerate()
    except Exception:
        duration = 0.0

    print(f"[recv] {tag} ({duration:.1f}s) from {node}", flush=True)

    # Skip clips too short to contain real speech — saves Whisper CPU
    if duration < 0.5:
        return jsonify({"status": "too_short"}), 202

    # ------------------------------------------------------------------
    # Deduplication: reject identical audio payloads received within
    # _DEDUP_TTL_SECONDS of each other (e.g. Pi retransmit on network
    # hiccup, double-post from the broadcastify bridge, etc.).
    # We hash the raw decoded WAV bytes; same call == same bytes.
    # ------------------------------------------------------------------
    audio_hash = hashlib.sha256(wav_bytes).hexdigest()
    now = time.time()
    with _recv_dedup_lock:
        # Evict expired entries first to keep the dict bounded
        if len(_recv_seen_hashes) >= _DEDUP_MAX_ENTRIES:
            expired_keys = [k for k, exp in _recv_seen_hashes.items() if exp <= now]
            for k in expired_keys:
                del _recv_seen_hashes[k]

        if audio_hash in _recv_seen_hashes and _recv_seen_hashes[audio_hash] > now:
            print(f"[recv] DEDUP {tag} ({duration:.1f}s) — identical payload seen within {_DEDUP_TTL_SECONDS}s window", flush=True)
            return jsonify({"status": "duplicate"}), 202

        # Register hash; will expire after TTL
        _recv_seen_hashes[audio_hash] = now + _DEDUP_TTL_SECONDS

    # Bounded backlog with pi5 priority: broadcastify capped at _BROADCASTIFY_MAX
    # so at least (_MAX_PROCESS_THREADS - _BROADCASTIFY_MAX) slots are always
    # available for OP25 audio from the Pi.
    is_broadcastify = node != "pi5"
    if is_broadcastify and not _broadcastify_sem.acquire(blocking=False):
        print(f"[recv] DROP {tag} ({duration:.1f}s) [broadcastify] — broadcastify cap ({_BROADCASTIFY_MAX}) reached", flush=True)
        return jsonify({"status": "backlog_full"}), 202
    if not _process_sem.acquire(blocking=False):
        if is_broadcastify:
            _broadcastify_sem.release()
        src_label = "pi5" if node == "pi5" else "broadcastify"
        print(f"[recv] DROP {tag} ({duration:.1f}s) [{src_label}] — backlog full ({_MAX_PROCESS_THREADS} active)", flush=True)
        return jsonify({"status": "backlog_full"}), 202

    def process():
        try:
            global _last_call_ts
            _last_call_ts = time.time()
            transcript = transcribe(wav_bytes)
            lat, lon, location = extract_location(transcript)
            if lat is None:
                lat, lon = def_lat, def_lon
                location = None
                coords_approx = 1
            else:
                coords_approx = 0
            print(f"[recv] {tag}: {transcript[:80]}", flush=True)
            call_id = insert_call(ts, tgid, tag, category, node, duration, transcript, lat, lon, location, coords_approx)
            call = dict(id=call_id, ts=ts, tgid=tgid, tag=tag, category=category,
                        transcript=transcript, lat=lat, lon=lon, location=location)
            recent = calls_since(ts - 15 * 60)
            call["groq"] = groq_analyze(call, recent)
            # If this is an unknown talkgroup, ask Groq to identify it
            if tag.startswith("TGID ") and transcript and len(transcript) >= _TGID_ID_MIN_LEN:
                threading.Thread(target=groq_identify_tgid, args=(tgid, transcript),
                                 daemon=True).start()
            analyze_for_incident(call)
            post_to_talk(call)
        finally:
            _process_sem.release()
            if is_broadcastify:
                _broadcastify_sem.release()

    threading.Thread(target=process, daemon=True).start()
    return jsonify({"status": "queued"}), 202


@app.route("/watchdog_event", methods=["POST"])
def watchdog_event():
    """Receive Pi watchdog events and forward as Talk DM alerts."""
    data  = request.get_json(force=True)
    event = data.get("event", "unknown")
    msg   = data.get("msg", "")
    icons = {"op25_down": "⚠️", "op25_recovered": "✅", "op25_failed": "🚨"}
    icon  = icons.get(event, "⚠️")
    full  = f"{icon} PI WATCHDOG: {msg}"
    print(f"[pi-watchdog] {full}", flush=True)
    threading.Thread(target=_pi_watchdog_alert, args=(full,), daemon=True).start()
    return jsonify({"status": "ok"}), 200


@app.route("/pi/commands", methods=["GET"])
def pi_commands():
    """Pi polls this endpoint for pending commands (restart_op25, etc.)."""
    cmds = list(_pi_command_queue)
    _pi_command_queue.clear()
    return jsonify({"commands": cmds}), 200


@app.route("/test_call", methods=["POST"])
def test_call():
    """Inject a synthetic call for pipeline testing — bypasses Whisper."""
    data = request.get_json(force=True)
    tgid       = int(data.get("tgid", 1315))
    transcript = data.get("transcript", "")
    tag        = data.get("tag") or TGID_META.get(tgid, {}).get("tag") or f"TGID {tgid}"
    meta       = TGID_META.get(tgid, {})
    category   = data.get("category") or meta.get("cat", "Unknown")
    ts         = time.time()
    lat        = data.get("lat") or meta.get("lat")
    lon        = data.get("lon") or meta.get("lon")
    location   = data.get("location")
    call_id = insert_call(ts, tgid, tag, category, "test", 5.0, transcript, lat, lon, location)
    call = dict(id=call_id, ts=ts, tgid=tgid, tag=tag, category=category,
                transcript=transcript, lat=lat, lon=lon, location=location)
    recent = calls_since(ts - 15 * 60)
    call["groq"] = groq_analyze(call, recent)
    analyze_for_incident(call)
    post_to_talk(call)
    return jsonify({"status": "ok", "tag": tag, "category": category, "transcript": transcript}), 200


@app.route("/api/calls")
def api_calls():
    return jsonify(recent_calls(200))


# --- Prometheus metrics endpoint (added 2026-04-18 / bak39) ---
try:
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry
    from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

    _BB_METRICS_REGISTRY = CollectorRegistry()

    class _BBMetricsCollector:
        def collect(self):
            try:
                c = sqlite3.connect(DB_PATH, timeout=5.0)
                cur = c.cursor()

                cur.execute(
                    "SELECT COALESCE(itype,'unknown'), COUNT(*) "
                    "FROM incidents WHERE (is_test IS NULL OR is_test=0) "
                    "GROUP BY itype"
                )
                m = CounterMetricFamily(
                    "battlebuddy_incidents",
                    "Total non-test incidents detected by Battle Buddy, by itype",
                    labels=["itype"],
                )
                for itype, count in cur.fetchall():
                    m.add_metric([str(itype)], float(count))
                yield m

                cur.execute("SELECT COUNT(*) FROM calls")
                (call_count,) = cur.fetchone()
                m2 = CounterMetricFamily(
                    "battlebuddy_calls",
                    "Total transcribed radio calls across all talkgroups",
                )
                m2.add_metric([], float(call_count))
                yield m2

                cur.execute(
                    "SELECT "
                    "  CASE "
                    "    WHEN tag LIKE 'APD%' THEN 'APD' "
                    "    WHEN tag LIKE 'AFD%' THEN 'AFD' "
                    "    WHEN tag LIKE 'TCEMS%' THEN 'TCEMS' "
                    "    WHEN tag LIKE 'TCSO%' THEN 'TCSO' "
                    "    WHEN tag LIKE 'LE %' OR tag LIKE 'LE/%' OR tag LIKE 'Lago%' THEN 'LE_other' "
                    "    WHEN tag LIKE '%Scanner%' THEN 'scanner_gateway' "
                    "    ELSE 'other' "
                    "  END AS agency, "
                    "  COUNT(*) "
                    "FROM calls GROUP BY agency"
                )
                m3 = CounterMetricFamily(
                    "battlebuddy_calls_by_agency",
                    "Total transcribed radio calls grouped by agency prefix",
                    labels=["agency"],
                )
                for agency, count in cur.fetchall():
                    m3.add_metric([str(agency)], float(count))
                yield m3

                # --- homicide YTD gauge — sourced from curated homicides_2026.json ---
                try:
                    import json as _json, os as _os
                    _hf = _os.path.join(_os.path.dirname(__file__), "homicides_2026.json")
                    _hdata = _json.load(open(_hf))
                    _homicide_victims = sum(h.get("count", 1) for h in _hdata)
                    _homicide_incidents = len(_hdata)
                except Exception:
                    _homicide_victims = 0
                    _homicide_incidents = 0
                g_hom_v = GaugeMetricFamily(
                    "battlebuddy_homicides_ytd_victims",
                    "Austin homicide victims tracked by Battle Buddy, year-to-date 2026",
                )
                g_hom_v.add_metric([], float(_homicide_victims))
                yield g_hom_v
                g_hom_i = GaugeMetricFamily(
                    "battlebuddy_homicides_ytd",
                    "Austin homicide incidents tracked by Battle Buddy, year-to-date 2026",
                )
                g_hom_i.add_metric([], float(_homicide_incidents))
                yield g_hom_i

                # --- shooting intelligence tiers (30-day window) ---
                import time as _time
                _now = _time.time()
                _30d = _now - (30 * 86400)
                CORROBORATING_AGENCIES = {"AFD", "TCEMS", "TCSO", "TCFD"}

                cur.execute(
                    "SELECT agencies FROM incidents "
                    "WHERE itype='SHOOTING' AND ts_start >= ? "
                    "AND (is_test IS NULL OR is_test=0) "
                    "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')",
                    (_30d,),
                )
                import json as _json2
                _s_confirmed = 0
                _s_signal = 0
                for (_ag,) in cur.fetchall():
                    try:
                        _ags = set(_json2.loads(_ag or "[]"))
                    except Exception:
                        _ags = set()
                    if _ags & CORROBORATING_AGENCIES:
                        _s_confirmed += 1
                    elif _ags - {"Unknown", "scanner_gateway", None, ""}:
                        _s_signal += 1

                cur.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE itype='SHOOTING' AND ts_start >= ? "
                    "AND (is_test IS NULL OR is_test=0) "
                    "AND description LIKE '%[APD Press Release]%'",
                    (_30d,),
                )
                (_s_press,) = cur.fetchone()

                for _name, _help, _val in [
                    ("battlebuddy_shootings_confirmed_30d",
                     "Shooting incidents corroborated by AFD/TCEMS/TCSO radio in last 30 days",
                     _s_confirmed),
                    ("battlebuddy_shootings_signal_30d",
                     "Shooting incidents on known agency talkgroup, unverified, last 30 days",
                     _s_signal),
                    ("battlebuddy_shootings_press_release_30d",
                     "Shooting incidents from APD press releases in last 30 days",
                     _s_press),
                ]:
                    _g = GaugeMetricFamily(_name, _help)
                    _g.add_metric([], float(_val))
                    yield _g

                # --- live gauges ---
                _window = _now - 86400

                cur.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE status='active' AND (is_test IS NULL OR is_test=0) "
                    "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')"
                )
                (active_count,) = cur.fetchone()
                g_active = GaugeMetricFamily(
                    "battlebuddy_active_incidents",
                    "Currently active (non-cleared) Battle Buddy incidents",
                )
                g_active.add_metric([], float(active_count))
                yield g_active

                cur.execute(
                    "SELECT COALESCE(itype,'unknown'), COUNT(*) FROM incidents "
                    "WHERE ts_start >= ? AND (is_test IS NULL OR is_test=0) "
                    "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%') "
                    "GROUP BY itype",
                    (_window,),
                )
                g24 = GaugeMetricFamily(
                    "battlebuddy_incidents_24h",
                    "Incidents detected in the last 24 hours by itype",
                    labels=["itype"],
                )
                for itype, count in cur.fetchall():
                    g24.add_metric([str(itype)], float(count))
                yield g24

                cur.execute(
                    "SELECT COUNT(*) FROM calls WHERE ts >= ?",
                    (_window,),
                )
                (calls_24h,) = cur.fetchone()
                g_calls = GaugeMetricFamily(
                    "battlebuddy_calls_24h",
                    "Radio calls received in the last 24 hours",
                )
                g_calls.add_metric([], float(calls_24h))
                yield g_calls

                c.close()
            except Exception as _e:
                print(f"[metrics] collector error: {_e}", flush=True)

    _BB_METRICS_REGISTRY.register(_BBMetricsCollector())

    @app.route("/metrics")
    def prometheus_metrics():
        return (
            generate_latest(_BB_METRICS_REGISTRY),
            200,
            {"Content-Type": CONTENT_TYPE_LATEST},
        )
except Exception as _metrics_init_err:
    print(f"[metrics] disabled \u2014 init failed: {_metrics_init_err}", flush=True)
# --- end Prometheus metrics ---



@app.route("/api/sitrep")
def api_sitrep():
    minutes = int(request.args.get("minutes", 60))
    return jsonify({"sitrep": build_sitrep(minutes)})


@app.route("/api/voice_sitrep")
def api_voice_sitrep():
    """Returns a clean, natural-language spoken sitrep for TTS."""
    minutes = int(request.args.get("minutes", 60))
    calls     = calls_for_sitrep(minutes)
    incidents = [i for i in active_incidents() if not i.get("is_test")]

    now = datetime.now(_CDT).strftime("%-I:%M %p %Z")
    parts = [f"Battle Buddy. Austin Metro situation report as of {now}."]

    if incidents:
        count = len(incidents)
        parts.append(f"{count} active {'incident' if count == 1 else 'incidents'}.")
        for inc in incidents:
            age = int((time.time() - inc["ts_start"]) / 60)
            loc = f" at {inc['location']}" if inc.get("location") else ""
            agencies = json.loads(inc.get("agencies") or "[]")
            agency_str = ", ".join(agencies[:3]) if agencies else "unknown agencies"
            age_str = f"{age} minutes ago" if age < 60 else f"{age // 60} hours ago"
            parts.append(
                f"{inc['itype'].replace('/', ' or ')}{loc}, "
                f"detected {age_str}, {agency_str} responding."
            )
    else:
        parts.append("No active incidents at this time.")

    if calls:
        by_cat: dict[str, int] = {}
        for c in calls:
            cat = c.get("category") or "Unknown"
            by_cat[cat] = by_cat.get(cat, 0) + 1
        top = sorted(by_cat.items(), key=lambda x: -x[1])[:4]
        summary = ", ".join(f"{cat} {n}" for cat, n in top)
        parts.append(
            f"{len(calls)} calls monitored in the past {minutes} minutes "
            f"across {summary}."
        )
    else:
        parts.append(f"No calls received in the past {minutes} minutes.")

    return jsonify({"text": " ".join(parts)})


@app.route("/api/incidents")
def api_incidents():
    return jsonify(get_all_incidents(50))


@app.route("/api/incidents/active")
def api_incidents_active():
    return jsonify(active_incidents())


@app.route("/api/stats")
def api_stats():
    """24-hour summary stats for the splash page."""
    since = time.time() - 86400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    calls_24h = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE ts > ?", (since,)
    ).fetchone()[0]
    inc_24h = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE ts_start > ? AND is_test=0"
        " AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')", (since,)
    ).fetchone()[0]
    no_loc = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE ts_start > ? AND is_test=0"
        " AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')"
        " AND (location IS NULL OR location='')",
        (since,)
    ).fetchone()[0]
    agencies_24h = conn.execute(
        "SELECT COUNT(DISTINCT category) FROM calls WHERE ts > ? AND category IS NOT NULL AND category != '' AND category != 'Unknown'",
        (since,)
    ).fetchone()[0]
    conn.close()
    return jsonify({"calls_24h": calls_24h, "incidents_24h": inc_24h, "no_location": no_loc, "agencies_24h": agencies_24h})


@app.route("/api/shooting_intel")
def api_shooting_intel():
    """
    Public endpoint: shooting incidents with transcript evidence, last 30 days.
    Returns only incidents corroborated by known (non-encrypted) agencies.
    Excludes APD press releases (those are in the verified homicide/press tracker).
    """
    import json as _json
    since = time.time() - (30 * 86400)
    CORROBORATING = {"AFD", "TCEMS", "TCSO", "TCFD"}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT i.id, i.itype, i.agencies, i.description, i.location, i.lat, i.lon, "
        "       i.ts_start, i.status "
        "FROM incidents i "
        "WHERE i.itype IN ('SHOOTING','STABBING') "
        "AND i.ts_start >= ? "
        "AND (i.is_test IS NULL OR i.is_test=0) "
        "AND (i.description IS NULL OR i.description NOT LIKE '%[APD Press Release]%') "
        "ORDER BY i.ts_start DESC",
        (since,)
    ).fetchall()

    results = []
    for row in rows:
        try:
            agencies = set(_json.loads(row["agencies"] or "[]"))
        except Exception:
            agencies = set()

        # Only include incidents from known, non-encrypted agencies
        known = agencies - {"Unknown", "scanner_gateway", None, ""}
        if not known:
            continue

        if agencies & CORROBORATING:
            tier = "confirmed"
            tier_label = "Corroborated — EMS/Fire radio"
        else:
            tier = "radio_signal"
            tier_label = "Radio signal — known agency, under investigation"

        # Pull corroborating transcripts
        transcripts = conn.execute(
            "SELECT c.tag, c.category, c.transcript, datetime(c.ts,'unixepoch','localtime') as ts "
            "FROM calls c JOIN incident_calls ic ON ic.call_id = c.id "
            "WHERE ic.incident_id = ? "
            "AND c.category NOT IN ('Unknown') "
            "AND length(c.transcript) > 10 "
            "ORDER BY c.ts ASC LIMIT 5",
            (row["id"],)
        ).fetchall()

        results.append({
            "incident_id": row["id"],
            "itype": row["itype"],
            "ts": row["ts_start"],
            "ts_local": __import__("datetime").datetime.fromtimestamp(
                row["ts_start"],
                tz=__import__("zoneinfo").ZoneInfo("America/Chicago")
            ).strftime("%Y-%m-%d %H:%M CDT"),
            "location": row["location"],
            "lat": row["lat"],
            "lon": row["lon"],
            "agencies": list(agencies),
            "confidence_tier": tier,
            "confidence_label": tier_label,
            "evidence": [
                {
                    "agency": t["category"],
                    "talkgroup": t["tag"],
                    "time": t["ts"],
                    "transcript": t["transcript"][:300],
                }
                for t in transcripts
            ],
        })

    conn.close()
    return jsonify({
        "window": "last_30_days",
        "methodology": (
            "Only incidents detected on known, non-encrypted agency talkgroups are included. "
            "'Confirmed' = corroborated by AFD, TCEMS, TCSO, or TCFD radio traffic. "
            "'Radio signal' = detected on a known agency talkgroup but not yet corroborated by a second agency. "
            "APD radio is P25 encrypted — APD transcripts are not available. "
            "All evidence is verbatim radio transcript text."
        ),
        "count": len(results),
        "confirmed": sum(1 for r in results if r["confidence_tier"] == "confirmed"),
        "radio_signal": sum(1 for r in results if r["confidence_tier"] == "radio_signal"),
        "incidents": results,
    })


@app.route("/api/daily_summary")
def api_daily_summary():
    """
    Public daily summary of incidents bucketed by confidence tier.

    Confidence tiers:
      confirmed     — corroborated by AFD, TCEMS, TCSO, or TCFD radio traffic
      radio_signal  — detected on known (non-Unknown) agency talkgroup, single source
      press_release — APD press release (verified, but lagging and APD-selected)
      unconfirmed   — Unknown agency / scanner gateway only (low confidence)
    """
    import json as _json
    since = time.time() - 86400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT itype, description, agencies FROM incidents "
        "WHERE ts_start > ? AND (is_test IS NULL OR is_test=0) "
        "ORDER BY ts_start DESC",
        (since,)
    ).fetchall()
    conn.close()

    CORROBORATING = {"AFD", "TCEMS", "TCSO", "TCFD"}

    confirmed = []
    radio_signal = []
    press_release = []
    unconfirmed = []

    for row in rows:
        desc = row["description"] or ""
        itype = row["itype"] or "UNKNOWN"
        try:
            agencies = set(_json.loads(row["agencies"] or "[]"))
        except Exception:
            agencies = set()

        if desc.startswith("[APD Press Release]"):
            press_release.append(itype)
        elif agencies & CORROBORATING:
            confirmed.append(itype)
        elif agencies - {"Unknown", "scanner_gateway", None, ""}:
            radio_signal.append(itype)
        else:
            unconfirmed.append(itype)

    def summarize(lst):
        from collections import Counter
        return dict(Counter(lst))

    return jsonify({
        "window": "last_24h",
        "note": (
            "Confirmed = corroborated by AFD/TCEMS/TCSO/TCFD radio. "
            "Radio signal = single known agency, unverified. "
            "Press release = APD-published, verified but lagging. "
            "Unconfirmed = encrypted scanner noise, treat as rumor only."
        ),
        "confirmed":     {"count": len(confirmed),     "by_type": summarize(confirmed)},
        "radio_signal":  {"count": len(radio_signal),  "by_type": summarize(radio_signal)},
        "press_release": {"count": len(press_release), "by_type": summarize(press_release)},
        "unconfirmed":   {"count": len(unconfirmed),   "by_type": summarize(unconfirmed)},
    })


@app.route("/api/tgid_guesses")
def api_tgid_guesses():
    """Return all TGID identification guesses, grouped by tgid."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM tgid_guesses ORDER BY ts DESC LIMIT 500"
    ).fetchall()
    conn.close()

    # Group by tgid — include count of HIGH/MED guesses and top guess
    from collections import Counter, defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r["tgid"]].append(dict(r))

    result = []
    for tgid, guesses in sorted(groups.items()):
        hm = [g["guess"] for g in guesses if g["confidence"] in ("HIGH", "MED") and g["guess"]]
        top = Counter(hm).most_common(1)[0] if hm else (None, 0)
        result.append({
            "tgid":          tgid,
            "guess_count":   len(guesses),
            "top_guess":     top[0],
            "top_count":     top[1],
            "confirmed":     any(g["confirmed"] for g in guesses),
            "guesses":       guesses[:10],   # most recent 10
        })

    return jsonify(result)


@app.route("/api/tgid_guesses/confirm", methods=["POST"])
def api_tgid_confirm():
    """Manually confirm a TGID name and write it to the tags TSV."""
    data  = request.get_json(force=True)
    tgid  = int(data.get("tgid", 0))
    name  = (data.get("name") or "").strip()
    if not tgid or not name:
        return jsonify({"error": "tgid and name required"}), 400

    # Write to tags file
    try:
        with open(TGID_TSV, "a") as f:
            f.write(f"{tgid}\t{name}\n")
        # Mark confirmed in DB
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid,))
        conn.commit()
        conn.close()
        # Reload talkgroup table
        load_talkgroups()
        return jsonify({"status": "ok", "tgid": tgid, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/drone_sighting", methods=["POST"])
def api_drone_sighting():
    """Receive a drone sighting from the DroneRID Android app."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no JSON body"}), 400
    serial = data.get("serial", "").strip()
    lat    = data.get("lat")
    lon    = data.get("lon")
    if not serial or lat is None or lon is None:
        return jsonify({"error": "serial, lat, lon required"}), 400
    ts = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO drone_sightings (ts, serial, ua_type, lat, lon, alt_geo, alt_agl, speed_ms, heading) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            serial,
            int(data.get("ua_type", 0)),
            float(lat),
            float(lon),
            float(data.get("alt_geo", 0)),
            float(data.get("alt_agl", 0)),
            float(data.get("speed_ms", 0)),
            int(data.get("heading", 0)),
        )
    )
    conn.commit()
    conn.close()
    app.logger.info(f"Drone sighting: serial={serial} lat={lat} lon={lon} agl={data.get('alt_agl')}m")
    return jsonify({"status": "ok", "serial": serial, "ts": ts})


@app.route("/api/drone_sightings")
def api_drone_sightings():
    """Return recent drone sightings (last 24h by default, ?hours=N to override)."""
    hours = min(int(request.args.get("hours", 24)), 168)
    since = time.time() - (hours * 3600)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM drone_sightings WHERE ts > ? ORDER BY ts DESC LIMIT 500",
        (since,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/adsb")
def api_adsb():
    """Return current aircraft positions + 30-min trails grouped by icao24."""
    cutoff = time.time() - ADSB_TRAIL_SECS
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM aircraft_positions WHERE ts > ? ORDER BY icao24, ts",
        (cutoff,)
    ).fetchall()
    conn.close()

    aircraft: dict = {}
    for r in rows:
        icao = r["icao24"]
        if icao not in aircraft:
            aircraft[icao] = {
                "icao24":   icao,
                "label":    r["label"],
                "callsign": r["callsign"],
                "is_leo":   bool(r["is_leo"]),
                "lat":      r["lat"],
                "lon":      r["lon"],
                "alt_ft":   r["alt_ft"],
                "heading":  r["heading"],
                "ts":       r["ts"],
                "trail":    [],
            }
        else:
            # Keep latest position as current
            aircraft[icao].update({
                "lat":     r["lat"],
                "lon":     r["lon"],
                "alt_ft":  r["alt_ft"],
                "heading": r["heading"],
                "ts":      r["ts"],
            })
        aircraft[icao]["trail"].append([r["lat"], r["lon"], r["ts"]])

    return jsonify(list(aircraft.values()))


@app.route("/")
def index():
    return render_template_string(HTML)


# ---------------------------------------------------------------------------
# Talk bot webhook — handles slash commands from Nextcloud Talk users
# Register with: occ talk:bot:install "Battle Buddy" <secret> http://127.0.0.1:9001/bot/talk
# ---------------------------------------------------------------------------

def _verify_bot_signature(raw_body: bytes, random_header: str, sig_header: str) -> bool:
    """Verify Nextcloud Talk bot HMAC-SHA256 signature."""
    expected = hmac.new(
        TALK_BOT_SECRET.encode(),
        (random_header + raw_body.decode()).encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header.lower())


def _bot_reply(room_token: str, message: str):
    """Post a reply back to the Talk room that triggered the command."""
    url     = f"{TALK_BASE}/chat/{room_token}"
    payload = json.dumps({"message": message}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    req     = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization":  f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Content-Type":   "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[bot] reply sent to {room_token} ({len(message)} chars)", flush=True)
    except Exception as e:
        print(f"[bot] reply FAILED to {room_token}: {e}", flush=True)


@app.route("/bot/talk", methods=["POST"])
def bot_talk():
    raw_body      = request.get_data()
    random_header = request.headers.get("X-Nextcloud-Talk-Random", "")
    sig_header    = request.headers.get("X-Nextcloud-Talk-Signature", "")

    if not _verify_bot_signature(raw_body, random_header, sig_header):
        return jsonify({"error": "invalid signature"}), 401

    data    = json.loads(raw_body)
    raw_content = (data.get("object", {}).get("content") or "")
    # Content may be a JSON-encoded message object or plain text
    try:
        parsed = json.loads(raw_content)
        content = (parsed.get("message") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        content = raw_content.strip()
    token   = data.get("target", {}).get("id", TALK_ROOM)
    actor   = data.get("actor", {}).get("name", "someone")

    # Ignore messages posted by the bot itself to avoid loops
    if actor in ("Battle Buddy", TALK_USER):
        return jsonify({"status": "ignored"}), 200

    print(f"[bot] received from {actor}: '{content[:80]}'", flush=True)

    # Only respond to !commands
    if not content.startswith("!"):
        return jsonify({"status": "ignored"}), 200

    parts   = content.split()
    command = parts[0].lower()
    print(f"[bot] executing command: {command}", flush=True)

    def respond(msg):
        threading.Thread(target=_bot_reply, args=(token, msg), daemon=True).start()

    if command == "!sitrep":
        try:
            minutes = int(parts[1]) if len(parts) > 1 else 60
            minutes = max(5, min(minutes, 360))
        except ValueError:
            minutes = 60
        sitrep = build_sitrep(minutes)
        respond(f"📋 Sitrep requested by {actor} (last {minutes} min)\n\n{sitrep}")

    elif command == "!incidents":
        incs = active_incidents()
        if not incs:
            respond("✅ No active incidents at this time.")
        else:
            lines = [f"⚡ {len(incs)} active incident(s):"]
            for inc in incs:
                age     = int((time.time() - inc["ts_start"]) / 60)
                updated = int((time.time() - inc["ts_updated"]) / 60)
                loc     = f" @ {inc['location']}" if inc.get("location") else ""
                agencies = ", ".join(json.loads(inc.get("agencies") or "[]"))
                lines.append(
                    f"• {inc['itype']}{loc} — started {age}m ago, "
                    f"last update {updated}m ago — {agencies}"
                )
                if inc.get("description"):
                    lines.append(f"  {inc['description']}")
            respond("\n".join(lines))

    elif command == "!status":
        calls_1h  = len(calls_since(time.time() - 3600))
        calls_15m = len(calls_since(time.time() - 900))
        incs      = active_incidents()
        held      = _current_hold_tgid
        hold_str  = f"Holding TGID {held}" if held else "No hold active"
        respond(
            f"🛰 Battle Buddy Status\n"
            f"Calls (last 15m): {calls_15m}  |  Calls (last 1h): {calls_1h}\n"
            f"Active incidents: {len(incs)}\n"
            f"OP25 hold: {hold_str}\n"
            f"Transcription: faster-whisper large-v3-turbo INT8 (local)"
        )

    elif command == "!subscribe":
        beat = parts[1].lower() if len(parts) > 1 else "all"
        valid = {"all", "apd", "fire-ems", "general"}
        if beat not in valid:
            respond(f"Unknown beat '{beat}'. Valid options: all, apd, fire-ems, general")
        else:
            # Resolve Nextcloud username from display name
            nc_user = data.get("actor", {}).get("id", "").replace("users/", "")
            if not nc_user:
                nc_user = actor.lower().replace(" ", "")
            add_subscription(nc_user, beat)
            respond(
                f"✅ {actor} subscribed to 🔴 alerts"
                + (f" for beat: {beat}" if beat != "all" else " for all incidents")
                + "\nYou'll receive a direct message when a priority incident is detected."
            )

    elif command == "!unsubscribe":
        beat = parts[1].lower() if len(parts) > 1 else "all"
        nc_user = data.get("actor", {}).get("id", "").replace("users/", "")
        if not nc_user:
            nc_user = actor.lower().replace(" ", "")
        remove_subscription(nc_user, beat)
        respond(f"🔕 {actor} unsubscribed from alerts (beat: {beat})")

    elif command == "!unknowns":
        # Show recent unknown TGID guesses
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT tgid, guess, confidence, COUNT(*) as cnt FROM tgid_guesses "
            "WHERE confirmed=0 GROUP BY tgid ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        conn.close()
        if not rows:
            respond("No unknown talkgroups identified yet.")
        else:
            lines = [f"🔍 Unknown TGIDs ({len(rows)} groups):"]
            for r in rows:
                lines.append(f"• TGID {r[0]}: {r[1] or '?'} ({r[2]}, {r[3]} guesses)")
            lines.append("\nTo confirm: !addtag <tgid> <name>")
            respond("\n".join(lines))

    elif command == "!addtag":
        # !addtag <tgid> <name with spaces>
        if len(parts) < 3:
            respond("Usage: !addtag <tgid> <name>  (e.g. !addtag 1373 APD South Patrol)")
        else:
            try:
                tgid_arg = int(parts[1])
                name_arg = " ".join(parts[2:])
                with open(TGID_TSV, "a") as f:
                    f.write(f"{tgid_arg}\t{name_arg}\n")
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid_arg,))
                conn.commit()
                conn.close()
                load_talkgroups()
                respond(f"✅ TGID {tgid_arg} → **{name_arg}** saved and loaded.")
            except ValueError:
                respond("Error: tgid must be a number.")
            except Exception as e:
                respond(f"Error saving tag: {e}")

    elif command == "!help":
        respond(
            "🤖 Battle Buddy Commands\n\n"
            "!sitrep [minutes] — Situation report (default 60m, max 360m)\n"
            "!incidents — List active incidents\n"
            "!status — System status and call volume\n"
            "!unknowns — Show unidentified talkgroup guesses\n"
            "!addtag <tgid> <name> — Confirm a talkgroup name\n"
            "!subscribe [beat] — Get 🔴 DM alerts (beats: all, apd, fire-ems, general)\n"
            "!unsubscribe [beat] — Stop DM alerts\n"
            "!help — This message"
        )

    else:
        respond(f"Unknown command: {command}. Try !help")

    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; background: #0f172a; color: #e2e8f0; display: flex; flex-direction: column; height: 100vh; }
#header { padding: 8px 16px; background: #1e293b; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#header h1 { font-size: 1.1rem; color: #f8fafc; letter-spacing: 2px; }
#status { font-size: 0.75rem; color: #64748b; margin-left: auto; }
#incident-bar { display: none; padding: 6px 16px; background: #7f1d1d; border-bottom: 2px solid #ef4444; font-size: 0.78rem; color: #fca5a5; }
#incident-bar.active { display: block; }
#main { display: flex; flex: 1; overflow: hidden; }
#map { flex: 1; }
#sidebar { width: 380px; display: flex; flex-direction: column; border-left: 1px solid #334155; overflow: hidden; }
#tabs { display: flex; border-bottom: 1px solid #334155; }
.tab { flex: 1; padding: 8px; text-align: center; cursor: pointer; font-size: 0.75rem; color: #64748b; border-bottom: 2px solid transparent; }
.tab.active { color: #f8fafc; border-bottom-color: #3b82f6; }
.tab.alert { color: #ef4444 !important; }
#feed, #sitrep-panel, #incidents-panel { flex: 1; overflow-y: auto; padding: 8px; display: none; }
#feed.active, #sitrep-panel.active, #incidents-panel.active { display: block; }
.call { padding: 6px 8px; border-bottom: 1px solid #1e293b; font-size: 0.72rem; }
.call .meta { display: flex; gap: 8px; margin-bottom: 2px; flex-wrap: wrap; }
.call .time { color: #64748b; }
.call .tag { font-weight: bold; }
.call .cat { padding: 1px 5px; border-radius: 3px; font-size: 0.65rem; }
.call .transcript { color: #94a3b8; }
.call .loc { color: #34d399; font-size: 0.65rem; }
.incident-card { padding: 8px; margin-bottom: 8px; border-radius: 4px; border-left: 3px solid #ef4444; background: #1e293b; font-size: 0.75rem; }
.incident-card.active { border-left-color: #ef4444; }
.incident-card.cleared { border-left-color: #334155; opacity: 0.6; }
.incident-card .itype { font-weight: bold; color: #f87171; font-size: 0.8rem; margin-bottom: 4px; }
.incident-card.cleared .itype { color: #64748b; }
.incident-card .imeta { color: #64748b; font-size: 0.68rem; margin-bottom: 3px; }
.incident-card .idesc { color: #94a3b8; }
#sitrep-panel { white-space: pre-wrap; font-size: 0.72rem; color: #94a3b8; line-height: 1.6; }
#sitrep-controls { padding: 8px; border-top: 1px solid #334155; display: none; }
#sitrep-controls.active { display: flex; gap: 8px; }
#sitrep-controls select, #sitrep-controls button { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; padding: 4px 8px; font-family: monospace; font-size: 0.75rem; cursor: pointer; }
</style>
</head>
<body>
<div id="header">
  <h1>&#9652; BATTLE BUDDY</h1>
  <span id="status">connecting...</span>
</div>
<div id="incident-bar" id="incident-bar"></div>
<div id="main">
  <div id="map"></div>
  <div id="sidebar">
    <div id="tabs">
      <div class="tab active" onclick="showTab('feed')">LIVE FEED</div>
      <div class="tab" id="tab-incidents" onclick="showTab('incidents')">INCIDENTS</div>
      <div class="tab" onclick="showTab('sitrep')">SITREP</div>
    </div>
    <div id="feed" class="active"></div>
    <div id="incidents-panel"></div>
    <div id="sitrep-panel"></div>
    <div id="sitrep-controls">
      <select id="sitrep-mins">
        <option value="30">30 min</option>
        <option value="60" selected>1 hr</option>
        <option value="180">3 hr</option>
        <option value="360">6 hr</option>
      </select>
      <button onclick="loadSitrep()">Refresh</button>
    </div>
  </div>
</div>

<script>
const CAT_COLORS = """ + json.dumps(CAT_COLORS) + r""";

const map = L.map('map').setView([30.2672, -97.7431], 10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 18
}).addTo(map);

const markers = {};

function catColor(cat) { return CAT_COLORS[cat] || CAT_COLORS['Unknown']; }

function makeIcon(cat, big) {
  const color = catColor(cat);
  const sz = big ? 18 : 12;
  return L.divIcon({
    html: `<div style="width:${sz}px;height:${sz}px;background:${color};border:2px solid white;border-radius:50%;box-shadow:0 0 6px ${color}"></div>`,
    iconSize: [sz,sz], iconAnchor: [sz/2,sz/2], className: ''
  });
}

function timeStr(ts) {
  return new Date(ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
}

function addCallToFeed(c) {
  const feed = document.getElementById('feed');
  const div = document.createElement('div');
  div.className = 'call';
  const color = catColor(c.category);
  div.innerHTML = `
    <div class="meta">
      <span class="time">${timeStr(c.ts)}</span>
      <span class="tag">${c.tag || 'TGID '+c.tgid}</span>
      <span class="cat" style="background:${color}22;color:${color}">${c.category||'?'}</span>
      ${c.location ? `<span class="loc">&#9654; ${c.location}</span>` : ''}
    </div>
    <div class="transcript">${c.transcript || '<em>transcribing...</em>'}</div>
  `;
  feed.insertBefore(div, feed.firstChild);
  while (feed.children.length > 100) feed.removeChild(feed.lastChild);
}

function addMarker(c) {
  if (!c.lat || !c.lon) return;
  const key = `${c.tgid}-${Math.round(c.ts)}`;
  if (markers[key]) return;
  const m = L.marker([c.lat, c.lon], {icon: makeIcon(c.category, false)}).addTo(map);
  const txt = c.transcript ? c.transcript.substring(0,120) : '(no transcript)';
  m.bindPopup(`<b>${c.tag || 'TGID '+c.tgid}</b><br><small>${timeStr(c.ts)}</small><br>${txt}`);
  markers[key] = m;
}

let lastCount = 0;

async function poll() {
  try {
    const resp = await fetch('/api/calls');
    const calls = await resp.json();
    document.getElementById('status').textContent =
      `${calls.length} calls | ${new Date().toLocaleTimeString()}`;
    if (calls.length !== lastCount) {
      if (lastCount === 0) {
        document.getElementById('feed').innerHTML = '';
        calls.forEach(c => { addCallToFeed(c); addMarker(c); });
      } else {
        const newCalls = calls.slice(0, calls.length - lastCount);
        newCalls.reverse().forEach(c => { addCallToFeed(c); addMarker(c); });
      }
      lastCount = calls.length;
    }
  } catch(e) {
    document.getElementById('status').textContent = 'connection error';
  }
}

async function pollIncidents() {
  try {
    const resp = await fetch('/api/incidents/active');
    const incidents = await resp.json();
    const bar = document.getElementById('incident-bar');
    const tab = document.getElementById('tab-incidents');

    if (incidents.length > 0) {
      bar.className = 'active';
      bar.textContent = '⚠ ACTIVE: ' + incidents.map(i => i.itype).join(' | ');
      tab.classList.add('alert');
    } else {
      bar.className = '';
      tab.classList.remove('alert');
    }

    if (document.getElementById('incidents-panel').classList.contains('active')) {
      renderIncidents();
    }
  } catch(e) {}
}

async function renderIncidents() {
  const resp = await fetch('/api/incidents');
  const incidents = await resp.json();
  const panel = document.getElementById('incidents-panel');
  if (!incidents.length) {
    panel.innerHTML = '<div style="padding:16px;color:#64748b">No incidents recorded yet.</div>';
    return;
  }
  panel.innerHTML = incidents.map(inc => {
    const age = Math.round((Date.now()/1000 - inc.ts_start) / 60);
    const agencies = (() => { try { return JSON.parse(inc.agencies||'[]').join(', '); } catch(e){ return ''; }})();
    const tgids    = (() => { try { return JSON.parse(inc.tgids||'[]').join(', '); } catch(e){ return ''; }})();
    const loc = inc.location ? ` @ ${inc.location}` : '';
    return `<div class="incident-card ${inc.status}">
      <div class="itype">${inc.itype}${loc}</div>
      <div class="imeta">${new Date(inc.ts_start*1000).toLocaleString()} · ${age}m ago · ${inc.status.toUpperCase()}</div>
      <div class="imeta">Agencies: ${agencies || 'unknown'} · TGIDs: ${tgids || 'unknown'}</div>
      <div class="idesc">${inc.description || ''}</div>
    </div>`;
  }).join('');
}

async function loadSitrep() {
  const mins = document.getElementById('sitrep-mins').value;
  const resp = await fetch(`/api/sitrep?minutes=${mins}`);
  const data = await resp.json();
  document.getElementById('sitrep-panel').textContent = data.sitrep;
}

function showTab(name) {
  ['feed','incidents','sitrep'].forEach(t => {
    document.getElementById(t === 'sitrep' ? 'sitrep-panel' : (t === 'incidents' ? 'incidents-panel' : 'feed'))
      .classList.toggle('active', t === name);
  });
  document.querySelectorAll('.tab').forEach((el, i) => {
    el.classList.toggle('active', ['feed','incidents','sitrep'][i] === name);
  });
  document.getElementById('sitrep-controls').classList.toggle('active', name === 'sitrep');
  if (name === 'sitrep') loadSitrep();
  if (name === 'incidents') renderIncidents();
}

const incMarkers = {};

function makeIncidentIcon(itype, status) {
  const active = status === 'active';
  const colors = {
    'SHOOTING': '#ef4444', 'OFFICER DOWN': '#ef4444', 'MASS CASUALTY': '#ef4444',
    'STRUCTURE FIRE': '#f97316', 'FIRE DISPATCH': '#f97316', 'FIRE/EMS DISPATCH': '#f97316',
    'FIRE ALARM': '#fbbf24', 'CRASH/COLLISION': '#fb923c', 'PURSUIT': '#fb923c',
    'HAZMAT': '#22c55e', 'WEAPONS': '#f97316',
  };
  const bg = colors[itype] || '#9ca3af';
  const abbr = itype.split(/[\s\/]/)[0].substring(0,3).toUpperCase();
  const opacity = active ? 1.0 : 0.55;
  const border = active ? '3px solid white' : '2px solid #94a3b8';
  const glow = active ? `box-shadow:0 0 10px ${bg},0 0 20px ${bg};` : '';
  return L.divIcon({
    html: `<div style="width:32px;height:32px;background:${bg};border:${border};border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:bold;color:white;opacity:${opacity};${glow}">${abbr}</div>`,
    iconSize: [32,32], iconAnchor: [16,16], className: ''
  });
}

async function pollIncidentMarkers() {
  try {
    const resp = await fetch('/api/incidents');
    const incidents = await resp.json();
    const seen = new Set();
    for (const inc of incidents) {
      // Only plot incidents with a real geocoded address (location field present)
      if (!inc.location || !inc.lat || !inc.lon) continue;
      seen.add(inc.id);
      const existing = incMarkers[inc.id];
      if (existing) {
        // Update icon if status changed
        existing.setIcon(makeIncidentIcon(inc.itype, inc.status));
      } else {
        const m = L.marker([inc.lat, inc.lon], {icon: makeIncidentIcon(inc.itype, inc.status), zIndexOffset: 1000}).addTo(map);
        const age = Math.round((Date.now()/1000 - inc.ts_start) / 60);
        m.bindPopup(`<b>${inc.itype}</b><br><small>${inc.status.toUpperCase()} · ${age}m ago</small><br>📍 ${inc.location}<br><small>${inc.description || ''}</small>`);
        incMarkers[inc.id] = m;
      }
    }
    // Remove markers for incidents no longer returned
    for (const id of Object.keys(incMarkers)) {
      if (!seen.has(parseInt(id))) {
        map.removeLayer(incMarkers[id]);
        delete incMarkers[id];
      }
    }
  } catch(e) {}
}

poll();
pollIncidents();
pollIncidentMarkers();
setInterval(poll, 5000);
setInterval(pollIncidents, 15000);
setInterval(pollIncidentMarkers, 30000);

// ── ADS-B helicopter layer ──────────────────────────────────────────────────
const adsbMarkers = {};
const adsbTrails  = {};

function makeHeloIcon(isLeo) {
  const color = isLeo ? '#f59e0b' : '#a855f7';  // amber=LEO, purple=unknown
  return L.divIcon({
    html: `<div style="font-size:20px;line-height:1;filter:drop-shadow(0 0 4px ${color});color:${color}">🚁</div>`,
    iconSize: [24,24], iconAnchor: [12,12], className: ''
  });
}

function adsbPopup(ac) {
  const ago = Math.round((Date.now()/1000 - ac.ts) / 60);
  const leo = ac.is_leo ? '<br><b style="color:#f59e0b">🔴 LEO</b>' : '';
  return `<b>${ac.label || ac.icao24}</b>${leo}
    <br>${ac.callsign ? 'Flight: ' + ac.callsign + '<br>' : ''}
    Alt: ${ac.alt_ft ? ac.alt_ft + ' ft' : '?'}
    | ICAO: ${ac.icao24}
    <br><small>${ago}m ago</small>`;
}

async function pollAdsb() {
  try {
    const resp = await fetch('/api/adsb');
    const aircraft = await resp.json();
    const seen = new Set();

    for (const ac of aircraft) {
      const key = ac.icao24;
      seen.add(key);

      // Draw / update trail
      const trailPts = ac.trail.map(p => [p[0], p[1]]);
      const trailColor = ac.is_leo ? '#f59e0b' : '#a855f7';
      if (adsbTrails[key]) {
        adsbTrails[key].setLatLngs(trailPts);
      } else {
        adsbTrails[key] = L.polyline(trailPts, {
          color: trailColor, weight: 1.5, opacity: 0.6, dashArray: '4 4'
        }).addTo(map);
      }

      // Draw / update marker
      if (adsbMarkers[key]) {
        adsbMarkers[key].setLatLng([ac.lat, ac.lon]);
        adsbMarkers[key].setPopupContent(adsbPopup(ac));
      } else {
        adsbMarkers[key] = L.marker([ac.lat, ac.lon], {icon: makeHeloIcon(ac.is_leo)})
          .bindPopup(adsbPopup(ac))
          .addTo(map);
      }
    }

    // Remove stale aircraft that dropped off
    for (const key of Object.keys(adsbMarkers)) {
      if (!seen.has(key)) {
        adsbMarkers[key].remove();
        delete adsbMarkers[key];
        if (adsbTrails[key]) { adsbTrails[key].remove(); delete adsbTrails[key]; }
      }
    }
  } catch(e) {}
}

pollAdsb();
setInterval(pollAdsb, 30000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Public-facing pages
# ---------------------------------------------------------------------------

PUBLIC_SPLASH_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Public Safety Intelligence</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Real-time Austin public safety intelligence. AI-powered P25 radio monitoring, live incident map, and breaking alerts.">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #0a0f1e;
  color: #e2e8f0;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
#hero {
  position: relative;
  flex: 1;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  overflow: hidden;
}
#hero-bg {
  position: absolute;
  inset: 0;
  background-image: url('/static/bgbattlebuddy.png');
  background-size: cover;
  background-position: center;
  filter: brightness(0.35) saturate(0.8);
  z-index: 0;
}
#hero-overlay {
  position: absolute;
  inset: 0;
  background: linear-gradient(to bottom, rgba(10,15,30,0.3) 0%, rgba(10,15,30,0.7) 70%, rgba(10,15,30,1) 100%);
  z-index: 1;
}
#hero-content {
  position: relative;
  z-index: 2;
  max-width: 800px;
  padding: 40px 24px;
}
.logo {
  font-size: 0.8rem;
  letter-spacing: 6px;
  color: #3b82f6;
  text-transform: uppercase;
  margin-bottom: 20px;
}
h1 {
  font-size: clamp(2.2rem, 5vw, 3.8rem);
  font-weight: 800;
  color: #f8fafc;
  line-height: 1.15;
  margin-bottom: 20px;
}
h1 span { color: #3b82f6; }
.sub {
  font-size: clamp(0.95rem, 2vw, 1.2rem);
  color: #94a3b8;
  line-height: 1.6;
  max-width: 580px;
  margin: 0 auto 36px;
}
.cta-row {
  display: flex;
  gap: 14px;
  justify-content: center;
  flex-wrap: wrap;
  margin-bottom: 56px;
}
.btn-primary {
  background: #3b82f6;
  color: white;
  padding: 14px 32px;
  border-radius: 8px;
  text-decoration: none;
  font-weight: 700;
  font-size: 1rem;
  transition: background 0.2s;
}
.btn-primary:hover { background: #2563eb; }
.btn-secondary {
  background: transparent;
  color: #e2e8f0;
  padding: 14px 32px;
  border-radius: 8px;
  border: 1px solid #334155;
  text-decoration: none;
  font-weight: 600;
  font-size: 1rem;
  transition: border-color 0.2s;
}
.btn-secondary:hover { border-color: #3b82f6; color: #3b82f6; }
.live-badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: rgba(239,68,68,0.15);
  border: 1px solid rgba(239,68,68,0.4);
  border-radius: 20px;
  padding: 6px 16px;
  font-size: 0.78rem;
  color: #fca5a5;
  margin-bottom: 28px;
}
.live-dot { width: 8px; height: 8px; background: #ef4444; border-radius: 50%; animation: blink 1s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.stats-row {
  display: flex;
  gap: 40px;
  justify-content: center;
  flex-wrap: wrap;
}
.stat { text-align: center; }
.stat-num { font-size: 2rem; font-weight: 800; color: #3b82f6; }
.stat-label { font-size: 0.72rem; color: #64748b; text-transform: uppercase; letter-spacing: 1px; margin-top: 2px; }
#features {
  background: #0a0f1e;
  padding: 80px 24px;
  text-align: center;
}
#features h2 { font-size: 1.8rem; color: #f8fafc; margin-bottom: 12px; }
#features .sub { font-size: 0.95rem; color: #64748b; margin-bottom: 48px; }
.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 20px;
  max-width: 960px;
  margin: 0 auto 60px;
}
.feature {
  background: #0f1729;
  border: 1px solid #1e3a5f;
  border-radius: 10px;
  padding: 24px;
  text-align: left;
}
.feature .icon { font-size: 1.6rem; margin-bottom: 12px; }
.feature h3 { font-size: 0.9rem; color: #f8fafc; margin-bottom: 6px; }
.feature p { font-size: 0.78rem; color: #64748b; line-height: 1.5; }
.final-cta {
  background: linear-gradient(135deg, #0f1729, #1e3a5f);
  border-top: 1px solid #1e3a5f;
  padding: 60px 24px;
  text-align: center;
}
.final-cta h2 { font-size: 1.6rem; color: #f8fafc; margin-bottom: 10px; }
.final-cta p { color: #64748b; margin-bottom: 28px; font-size: 0.9rem; }
footer {
  background: #0a0f1e;
  border-top: 1px solid #0f1729;
  padding: 20px 24px;
  text-align: center;
  font-size: 0.72rem;
  color: #334155;
}
</style>
</head>
<body>
<section id="hero">
  <div id="hero-bg"></div>
  <div id="hero-overlay"></div>
  <div id="hero-content">
    <div class="logo">&#9652; Battle Buddy</div>
    <div class="live-badge"><span class="live-dot"></span> Live — Austin Metro</div>
    <h1>Austin's Public Safety<br><span>Intelligence Platform</span></h1>
    <p class="sub">AI-powered P25 radio monitoring that listens to every agency simultaneously — and surfaces what matters before any news article exists.</p>
    <div class="cta-row">
      <a href="/premium/" class="btn-primary" style="background:#ef4444;font-size:1.05rem;padding:16px 36px">Subscribe &mdash; from $4/mo &rarr;</a>
      <a href="/public" class="btn-secondary">View Live Map</a>
      <a href="/public/feed" class="btn-secondary">Live Feed</a>
    </div>
    <div class="stats-row" id="stats">
      <div class="stat"><div class="stat-num" id="s-calls">—</div><div class="stat-label">Calls Monitored</div></div>
      <div class="stat"><div class="stat-num" id="s-incidents">—</div><div class="stat-label">Incidents Detected</div></div>
      <div class="stat"><div class="stat-num" id="s-homicides">—</div><div class="stat-label">Homicides Tracked — 2026</div></div>
      <div class="stat"><div class="stat-num" id="s-agencies">—</div><div class="stat-label">Agencies Monitored</div></div>
    </div>
    <div style="font-size:0.72rem;color:#64748b;margin-top:8px;">Last 24 hours &nbsp;·&nbsp; <span id="s-updated">updating...</span></div>
  </div>
</section>

<section id="features">
  <h2>Built for Breaking News</h2>
  <p class="sub">No scanner. No waiting. No missed calls.</p>
  <div class="feature-grid">
    <div class="feature"><div class="icon">📡</div><h3>P25 Radio Monitoring</h3><p>Every APD, AFD, DPS, Travis County EMS, and UT Police transmission captured simultaneously, around the clock.</p></div>
    <div class="feature"><div class="icon">🤖</div><h3>AI Transcription</h3><p>OpenAI Whisper converts every radio transmission to searchable text in near real time.</p></div>
    <div class="feature"><div class="icon">🔍</div><h3>Incident Detection</h3><p>Shootings, structure fires, SWAT activations, air assets, and DPS Capitol responses detected automatically.</p></div>
    <div class="feature"><div class="icon">📈</div><h3>Escalation Tracking</h3><p>From welfare check to K-9 standoff — Battle Buddy tracks the full chain as an incident evolves.</p></div>
    <div class="feature"><div class="icon">🗺️</div><h3>Live Incident Map</h3><p>Every incident plotted in real time across the Austin metro with agency, type, and transcript detail.</p></div>
    <div class="feature"><div class="icon">⚡</div><h3>Instant Alerts</h3><p>Subscribers receive direct alerts the moment a critical incident is detected — before any public notification.</p></div>
    <div class="feature"><div class="icon">🚁</div><h3>Air Asset Tracking &amp; Agency ID</h3><p>ADS-B telemetry monitored continuously. Aircraft tail numbers are cross-referenced against a database of known LEO, EMS, and fire assets — APD Air1, STAR Flight, and other agency helicopters identified by registration and announced on air when active. Intelligence persists even when radio goes encrypted.</p></div>
    <div class="feature"><div class="icon">📰</div><h3>APD Press Release Monitor</h3><p>New APD press releases detected within 5 minutes. Homicides geocoded, mapped, and cross-referenced with scanner data automatically.</p></div>
    <div class="feature"><div class="icon">🔴</div><h3>Austin Homicide Map</h3><p>Every confirmed 2026 homicide from official APD press releases — geocoded, mapped, and linked to the source. Self-updating.</p></div>
    <div class="feature"><div class="icon">🗺️</div><h3>TAK Integration</h3><p>Every detected incident automatically pushed to FreeTAKServer as a CoT marker — appearing in real time on WinTAK, ATAK, and iTAK across your team. Markers auto-clear when the incident closes.</p></div>
    <div class="feature"><div class="icon">🛸</div><h3>FAA Remote ID Drone Detection <span style="font-size:0.58rem;background:#1e3a5f;color:#60a5fa;padding:2px 6px;border-radius:3px;vertical-align:middle;margin-left:4px">COMING SOON</span></h3><p>FAA Remote ID broadcasts from licensed drones captured via SDR and plotted on the live map in real time — adding aerial dimension to ground-level situational awareness.</p></div>
    <div class="feature"><div class="icon">🗞️</div><h3>Intel News Feed</h3><p>Every confirmed incident and APD press release delivered as a live RSS feed in Nextcloud News — auto-subscribed at signup. One scrollable feed covering radio detections, press releases, and homicide updates.</p></div>
  </div>
</section>

<section id="ecosystem" style="padding:64px 24px;background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f">
  <div style="max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center">
    <div style="border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)">
      <img src="/static/nextcloud_ecosystem.png" alt="Battle Buddy connected ecosystem across laptop, phone, and tablet" loading="lazy" style="width:100%;display:block"/>
    </div>
    <div>
      <div style="font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:12px">Connected Platform</div>
      <h2 style="font-size:1.6rem;color:#f8fafc;font-weight:800;line-height:1.25;margin-bottom:16px">Every Device.<br>One Intelligence Feed.</h2>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px">Battle Buddy runs on a private Nextcloud platform — giving subscribers a full connected app ecosystem alongside real-time incident intelligence. Access from any device, anywhere.</p>
      <ul style="list-style:none;margin:20px 0 0;padding:0">
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Talk — encrypted team chat with direct incident alert delivery</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Files — shared briefings, field docs, and ATAK data packages</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Calendar — event coordination and assignment scheduling</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Maps — offline maps for field deployment</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Notes — field intel synced across all your devices</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>News — live incident and press release feed, auto-subscribed at signup</li>
      </ul>
    </div>
  </div>
</section>

<section id="atak-showcase" style="padding:64px 24px;background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f">
  <div style="max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center">
    <div style="border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)">
      <img src="/static/atak_screenshot.png" alt="Battle Buddy incident markers on ATAK — Austin aerial view" loading="lazy" style="width:100%;display:block"/>
    </div>
    <div>
      <div style="font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:12px">TAK Integration</div>
      <h2 style="font-size:1.6rem;color:#f8fafc;font-weight:800;line-height:1.25;margin-bottom:16px">Live Incidents.<br>On Your Tactical Map.</h2>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px">Every incident Battle Buddy detects is automatically pushed to FreeTAKServer as a CoT marker — appearing in real time on WinTAK, ATAK, and iTAK displays across your team.</p>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px">No manual entry. The moment a shooting or structure fire is confirmed, a red marker hits the map at the geocoded address with incident type, timestamp, and description. Markers auto-clear when the incident closes.</p>
      <ul style="list-style:none;margin:20px 0 0;padding:0">
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>CoT markers auto-post on incident detection</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Markers auto-clear when incident closes</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Works with WinTAK, ATAK Phone, iTAK</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Connects via FreeTAKServer SSL — radiodesk.ddns.net</li>
      </ul>
    </div>
  </div>
</section>
<section class="final-cta">
  <h2>Know Before Anyone Else</h2>
  <p style="max-width:520px;margin:0 auto 10px">Battle Buddy listens to every Austin agency simultaneously and alerts you the moment something happens &mdash; before any news article exists. Basic access starts at $4/mo.</p>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:28px">
    <a href="/premium/" class="btn-primary" style="background:#ef4444;font-size:1.05rem;padding:16px 36px">Subscribe Now &rarr;</a>
    <a href="/public" class="btn-secondary">Explore Free Map</a>
  </div>
  <p style="margin-top:18px;font-size:0.75rem;color:#475569">Basic $4/mo &nbsp;&middot;&nbsp; Premium $11/mo &nbsp;&middot;&nbsp; 7-day free trial &nbsp;&middot;&nbsp; Cancel anytime</p>
</section>

<footer>
  &copy; 2026 Battle Buddy &nbsp;·&nbsp; Austin Metro Public Safety Intelligence &nbsp;·&nbsp;
  <a href="/public" style="color:#3b82f6;text-decoration:none">Live Map</a> &nbsp;·&nbsp;
  <a href="/public/aircraft" style="color:#f59e0b;text-decoration:none">Aircraft</a> &nbsp;&middot;&nbsp;
  <a href="/public/homicides" style="color:#ef4444;text-decoration:none">Homicide Map</a> &nbsp;&middot;&nbsp;
  <a href="/public/feed" style="color:#3b82f6;text-decoration:none">Feed</a> &nbsp;·&nbsp;
  <a href="/public/about" style="color:#3b82f6;text-decoration:none">About</a> &nbsp;·&nbsp;
  <a href="https://kevinwatkins.grafana.net/public-dashboards/40592df4da7946c7861619906c8de92c" target="_blank" style="color:#10b981;text-decoration:none">📊 Stats</a>
</footer>

<script>
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('s-calls').textContent = d.calls_24h.toLocaleString();
    document.getElementById('s-incidents').textContent = d.incidents_24h.toLocaleString();
    // fetch homicide count separately
    try {
      const rh = await fetch('/api/homicides');
      const dh = await rh.json();
      const all = (dh.homicides || []).concat(dh.live || []);
      let total = 0; all.forEach(function(h){ total += (h.count || 1); });
      document.getElementById('s-homicides').textContent = total;
    } catch(eh) {}
    document.getElementById('s-agencies').textContent = d.agencies_24h.toLocaleString();
    document.getElementById('s-updated').textContent = 'Updated ' + new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  } catch(e) {}
}
loadStats();
setInterval(loadStats, 60000);
</script>
</body>
</html>
"""

PUBLIC_MAP_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Live Incident Map</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Real-time Austin public safety incident map powered by Battle Buddy. Live P25 radio intelligence for the Austin metro area.">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.heat/dist/leaflet-heat.js"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat/dist/leaflet-heat.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; display: flex; flex-direction: column; height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover { color: #3b82f6; }
#topbar .nav a.active { color: #3b82f6; }
#breaking { display: none; padding: 8px 20px; background: linear-gradient(90deg,#7f1d1d,#991b1b); border-bottom: 2px solid #ef4444; font-size: 0.8rem; color: #fca5a5; font-weight: 600; animation: pulse 2s infinite; }
#breaking.show { display: block; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.85} }
#map { flex: 1; }
#legend { position: absolute; bottom: 30px; left: 10px; z-index: 1000; background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f; border-radius: 8px; padding: 12px 16px; font-size: 0.72rem; }
#legend h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.leg-item { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.leg-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
#stats-bar { position: absolute; bottom: 30px; right: 10px; z-index: 1000; background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f; border-radius: 8px; padding: 12px 16px; font-size: 0.72rem; min-width: 160px; }
#stats-bar h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.stat-row { display: flex; justify-content: space-between; gap: 16px; margin-bottom: 3px; color: #cbd5e1; }
.stat-val { color: #3b82f6; font-weight: 600; }
#footer-ticker { background: #0f1729; border-top: 1px solid #1e3a5f; padding: 6px 20px; font-size: 0.7rem; color: #64748b; white-space: nowrap; overflow: hidden; }
#ticker-inner { display: inline-block; animation: scroll 40s linear infinite; }
@keyframes scroll { 0%{transform:translateX(100vw)} 100%{transform:translateX(-100%)} }
.popup-custom { font-family: -apple-system, sans-serif; font-size: 13px; }
.popup-custom .itype { font-weight: 700; color: #ef4444; font-size: 14px; margin-bottom: 4px; }
.popup-custom .meta { color: #64748b; font-size: 11px; margin-bottom: 4px; }
.popup-custom .transcript { color: #374151; font-size: 12px; line-height: 1.4; }
#voice-btn { background: none; border: 1px solid #1e3a5f; color: #64748b; border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; cursor: pointer; display: flex; align-items: center; gap: 5px; transition: all 0.2s; }
#voice-btn:hover { border-color: #3b82f6; color: #3b82f6; }
#voice-btn.on { border-color: #3b82f6; color: #3b82f6; background: rgba(59,130,246,0.1); }
#voice-btn.speaking { border-color: #ef4444; color: #ef4444; background: rgba(239,68,68,0.1); animation: pulse 1s infinite; }
#sitrep-btn { background: none; border: 1px solid #1e3a5f; color: #94a3b8; border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; cursor: pointer; transition: all 0.2s; }
#sitrep-btn:hover { border-color: #3b82f6; color: #3b82f6; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public" class="active">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="https://kevinwatkins.grafana.net/public-dashboards/40592df4da7946c7861619906c8de92c" target="_blank">📊 Stats</a>
    <a href="/tip">Submit Tip</a>
  </nav>
  <button id="sitrep-btn" onclick="speakSitrep()" title="Read situation report aloud">&#128266; SITREP</button>
  <button id="voice-btn" onclick="toggleAutoVoice()" title="Auto-announce new incidents">&#128276; AUTO</button>
</div>
<div id="breaking"></div>
<div id="map"></div>
<div id="legend">
  <h4>Agencies</h4>
  <div class="leg-item"><div class="leg-dot" style="background:#3b82f6"></div><span>APD / Law Enforcement</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#f97316"></div><span>AFD / Fire</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#22c55e"></div><span>EMS</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#a855f7"></div><span>DPS / State</span></div>
  <div class="leg-item"><svg width="12" height="12" viewBox="0 0 12 12" style="filter:drop-shadow(0 0 4px #ef4444);flex-shrink:0"><polygon points="6,0 12,12 0,12" fill="#ef4444" stroke="#fca5a5" stroke-width="1.5"/></svg><span>Active Incident</span></div>
  <div class="leg-item"><svg width="12" height="12" viewBox="0 0 12 12" style="flex-shrink:0"><polygon points="0,0 12,0 6,12" fill="#334155" stroke="#475569" stroke-width="1.5"/></svg><span>Cleared Incident</span></div>
</div>
<div id="stats-bar">
  <h4>Last 48 Hours</h4>
  <div class="stat-row"><span>Calls monitored</span><span class="stat-val" id="s-calls">—</span></div>
  <div class="stat-row"><span>Incidents detected</span><span class="stat-val" id="s-incidents">—</span></div>
  <div class="stat-row"><span>Active now</span><span class="stat-val" id="s-active">—</span></div>
  <div class="stat-row"><span>Last update</span><span class="stat-val" id="s-time">—</span></div>
</div>
<div id="footer-ticker"><div id="ticker-inner">Loading live feed...</div></div>
<script>
const CAT_COLORS = {"APD":"#3b82f6","TCSO":"#3b82f6","UTPD":"#3b82f6","DPS":"#a855f7","AFD":"#f97316","TCFD":"#f97316","TCEMS":"#22c55e","ABIA":"#eab308","Unknown":"#64748b"};
const INCIDENT_COLOR = "#ef4444";

const AUSTIN_BOUNDS = L.latLngBounds(
  L.latLng(29.85, -98.25),   // SW — south of Kyle/Buda, west of Bee Cave
  L.latLng(30.70, -97.25)    // NE — north of Round Rock, east of Bastrop
);
const map = L.map('map', {
  minZoom: 10,
  maxBounds: AUSTIN_BOUNDS,
  maxBoundsViscosity: 1.0
}).setView([30.32, -97.77], 11);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OpenStreetMap contributors',
  maxZoom: 18
}).addTo(map);

let heatLayer = null;
const incidentMarkers = {};

function catColor(cat) { return CAT_COLORS[cat] || CAT_COLORS['Unknown']; }

function makeIncidentIcon(itype) {
  return L.divIcon({
    html: `<div style="width:20px;height:20px;background:#ef4444;border:2px solid #fca5a5;border-radius:50%;box-shadow:0 0 12px #ef4444;animation:ping 1.5s infinite"></div>`,
    iconSize:[20,20], iconAnchor:[10,10], className:''
  });
}

function timeAgo(ts) {
  const m = Math.round((Date.now()/1000 - ts) / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.round(m/60)}h ago`;
}

async function flagIncident(id, btn) {
  btn.disabled = true;
  btn.textContent = 'Flagging...';
  try {
    await fetch(`/api/incidents/${id}/flag`, {method:'POST'});
    btn.textContent = '✔ FLAGGED';
    btn.style.background = '#16a34a';
  } catch(e) {
    btn.textContent = '⚑ FLAG FOR DEMO';
    btn.disabled = false;
  }
}

async function loadHeatmap() {
  const resp = await fetch('/api/calls');
  const calls = await resp.json();
  const pts = calls.filter(c => c.lat && c.lon && !c.coords_approx && AUSTIN_BOUNDS.contains([c.lat, c.lon])).map(c => [c.lat, c.lon, 0.6]);
  if (heatLayer) map.removeLayer(heatLayer);
  heatLayer = L.heatLayer(pts, {radius:22, blur:18, maxZoom:13,
    gradient:{0.2:'#1e3a5f', 0.5:'#3b82f6', 0.8:'#f97316', 1.0:'#ef4444'}
  }).addTo(map);
  const t = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  document.getElementById('s-time').textContent = t;
  // ticker
  const recent = calls.slice(0,20);
  document.getElementById('ticker-inner').textContent =
    recent.map(c => `${c.tag||'?'} · ${c.transcript ? c.transcript.substring(0,60) : '...'}`).join('   ◆   ');
}

let _incidentsSeeded = false;
async function loadIncidents() {
  const [activeResp, allResp] = await Promise.all([
    fetch('/api/incidents/active'), fetch('/api/incidents')]);
  const active = await activeResp.json();
  const all    = await allResp.json();
  const realAll    = all.filter(i => !i.is_test);
  const realActive = active.filter(i => !i.is_test);
  document.getElementById('s-active').textContent    = realActive.length;

  // Voice: seed on first load, check for new ones on subsequent polls
  if (!_incidentsSeeded) { _seedKnownIncidents(all); _incidentsSeeded = true; }
  else { _checkNewIncidents(all); }

  // Breaking bar — never show test incidents
  const bar = document.getElementById('breaking');
  if (realActive.length > 0) {
    bar.textContent = '⚠ BREAKING: ' + realActive.map(i =>
      i.itype + (i.location ? ' @ ' + i.location : '')).join('  ·  ');
    bar.classList.add('show');
  } else {
    bar.classList.remove('show');
  }

  // Clear old markers
  Object.values(incidentMarkers).forEach(m => map.removeLayer(m));

  // Only plot incidents we have a real address for, and only crime/fire types
  const MAP_ITYPES = new Set([
    "SHOOTING","STABBING","OFFICER DOWN","PURSUIT","WEAPONS",
    "STRUCTURE FIRE","FIRE DISPATCH","FIRE ALARM","FIRE/EMS DISPATCH","GRASS FIRE",
    "CRASH/COLLISION","FATAL CRASH","MULTI-AGENCY RESPONSE","MASS CASUALTY",
    "EMS DISPATCH","HAZMAT","AIR ASSET ACTIVE","DPS CAPITOL ACTIVATION",
    "FLOODING","ROAD HAZARD","PEDESTRIAN INCIDENT","VEHICLE FIRE"
  ]);
  // Add incident markers
  all.filter(i => i.location && i.lat && i.lon && MAP_ITYPES.has(i.itype) && AUSTIN_BOUNDS.contains([i.lat, i.lon])).forEach(inc => {
    const isTest   = inc.is_test === 1;
    const isActive = inc.status === 'active' && !isTest;
    const fill   = isTest ? '#78716c' : (isActive ? '#ef4444' : '#334155');
    const stroke = isTest ? '#a8a29e' : (isActive ? '#fca5a5' : '#475569');
    const opacity = isTest ? 0.45 : 1;
    const size   = isTest ? 12 : (isActive ? 24 : 16);
    const half   = size / 2;
    // Active = point-up triangle, Cleared = point-down triangle
    const pts = isActive
      ? `${half},0 ${size},${size} 0,${size}`
      : `0,0 ${size},0 ${half},${size}`;
    const glowFilter = isActive
      ? `filter:drop-shadow(0 0 6px #ef4444) drop-shadow(0 0 12px #ef4444)`
      : '';
    const icon = L.divIcon({
      html: `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="${glowFilter};opacity:${opacity}"><polygon points="${pts}" fill="${fill}" stroke="${stroke}" stroke-width="1.5"/></svg>`,
      iconSize:[size,size], iconAnchor:[half,half], className:''
    });
    const m = L.marker([inc.lat, inc.lon], {icon}).addTo(map);
    let agencies = '';
    try { agencies = JSON.parse(inc.agencies||'[]').join(', '); } catch(e){}
    m.bindPopup(`
      <div class="popup-custom">
        ${isTest ? `<div style="background:#292524;color:#a8a29e;font-size:10px;font-weight:700;letter-spacing:1px;padding:3px 6px;border-radius:3px;margin-bottom:6px;display:inline-block">SYSTEM TEST — NOT A REAL INCIDENT</div><br>` : ''}
        <div class="itype" style="${isTest?'color:#a8a29e':''}">${inc.itype}</div>
        <div class="meta">${new Date(inc.ts_start*1000).toLocaleString()} · ${timeAgo(inc.ts_start)} · ${inc.status.toUpperCase()}</div>
        ${inc.location ? `<div class="meta">📍 ${inc.location}</div>` : (inc._coords_approx ? `<div class="meta" style="color:#94a3b8">📍 Approximate location (no address extracted)</div>` : '')}
        <div class="meta">Agencies: ${agencies||'unknown'}</div>
        <div class="transcript">${inc.description||''}</div>
        ${!isTest ? `<button onclick="flagIncident(${inc.id},this)" style="margin-top:8px;padding:4px 10px;background:${inc.flagged?'#16a34a':'#1e40af'};color:white;border:none;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">${inc.flagged?'✔ FLAGGED':'⚑ FLAG FOR DEMO'}</button>` : ''}
      </div>
    `);
    incidentMarkers[inc.id] = m;
  });
}

// ---------------------------------------------------------------------------
// Text-to-speech
// ---------------------------------------------------------------------------
let _voiceAutoOn = localStorage.getItem('bb_voice_auto') === '1';
let _knownIncidentIds = new Set();
let _speaking = false;

function _bestVoice() {
  const voices = speechSynthesis.getVoices();
  // Prefer a natural-sounding US English voice
  const prefs = ['Samantha', 'Google US English', 'Microsoft Aria', 'Alex', 'Karen'];
  for (const name of prefs) {
    const v = voices.find(v => v.name.includes(name));
    if (v) return v;
  }
  return voices.find(v => v.lang === 'en-US') || voices[0] || null;
}

function _speak(text) {
  if (!('speechSynthesis' in window)) return;
  speechSynthesis.cancel();
  const utt = new SpeechSynthesisUtterance(text);
  utt.voice = _bestVoice();
  utt.rate  = 0.92;
  utt.pitch = 1.0;
  utt.volume = 1.0;
  const btn = document.getElementById('sitrep-btn');
  const vbtn = document.getElementById('voice-btn');
  _speaking = true;
  if (btn) btn.textContent = '⏹ STOP';
  utt.onend = utt.onerror = () => {
    _speaking = false;
    if (btn) btn.textContent = '🔊 SITREP';
    if (vbtn) vbtn.classList.remove('speaking');
  };
  speechSynthesis.speak(utt);
}

async function speakSitrep() {
  if (_speaking) { speechSynthesis.cancel(); return; }
  const resp = await fetch('/api/voice_sitrep');
  const data = await resp.json();
  _speak(data.text);
}

function toggleAutoVoice() {
  _voiceAutoOn = !_voiceAutoOn;
  localStorage.setItem('bb_voice_auto', _voiceAutoOn ? '1' : '0');
  const btn = document.getElementById('voice-btn');
  btn.classList.toggle('on', _voiceAutoOn);
  btn.title = _voiceAutoOn ? 'Auto-announce ON — click to disable' : 'Auto-announce new incidents';
}

function _checkNewIncidents(incidents) {
  if (!_voiceAutoOn) return;
  const real = incidents.filter(i => !i.is_test);
  for (const inc of real) {
    if (!_knownIncidentIds.has(inc.id)) {
      _knownIncidentIds.add(inc.id);
      // Don't announce on first page load — only genuinely new ones
      if (_knownIncidentIds.size > real.length) continue;
      const loc = inc.location ? ` at ${inc.location}` : '';
      const itype = inc.itype.replace('/', ' or ');
      const vbtn = document.getElementById('voice-btn');
      if (vbtn) vbtn.classList.add('speaking');
      _speak(`Battle Buddy alert. ${itype}${loc}. ${inc.description || ''}`);
      return; // speak one at a time
    }
  }
}

// Seed known IDs on first load so we don't announce old incidents
function _seedKnownIncidents(incidents) {
  incidents.filter(i => !i.is_test).forEach(i => _knownIncidentIds.add(i.id));
}

// Init voice button state
window.addEventListener('load', () => {
  const btn = document.getElementById('voice-btn');
  if (btn && _voiceAutoOn) btn.classList.add('on');
  // Seed voices list (Chrome requires a user gesture first, but this primes it)
  speechSynthesis.getVoices();
});

// ---------------------------------------------------------------------------

async function loadMapStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('s-calls').textContent = d.calls_24h.toLocaleString();
    document.getElementById('s-incidents').textContent = d.incidents_24h.toLocaleString();
  } catch(e) {}
}

loadHeatmap();
loadIncidents();
loadMapStats();
setInterval(loadHeatmap, 15000);
setInterval(loadIncidents, 10000);
setInterval(loadMapStats, 60000);
</script>
</body>
</html>
"""

PUBLIC_FEED_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Live Feed</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; min-height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover, #topbar .nav a.active { color: #3b82f6; }
#breaking { display: none; padding: 8px 20px; background: linear-gradient(90deg,#7f1d1d,#991b1b); border-bottom: 2px solid #ef4444; font-size: 0.8rem; color: #fca5a5; font-weight: 600; }
#breaking.show { display: block; }
#content { max-width: 860px; margin: 0 auto; padding: 24px 16px; }
.section-title { font-size: 0.7rem; letter-spacing: 2px; color: #3b82f6; text-transform: uppercase; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #1e3a5f; }
.incident-card { background: #0f1729; border: 1px solid #1e3a5f; border-left: 4px solid #ef4444; border-radius: 6px; padding: 14px 16px; margin-bottom: 12px; }
.incident-card.cleared { border-left-color: #334155; opacity: 0.7; }
.incident-card .itype { font-weight: 700; color: #ef4444; font-size: 1rem; margin-bottom: 4px; }
.incident-card.cleared .itype { color: #64748b; }
.incident-card .meta { font-size: 0.72rem; color: #64748b; margin-bottom: 6px; }
.incident-card .desc { font-size: 0.82rem; color: #94a3b8; }
.call-row { padding: 10px 0; border-bottom: 1px solid #0f1729; display: flex; gap: 12px; align-items: flex-start; }
.call-row .time { font-size: 0.7rem; color: #475569; min-width: 50px; padding-top: 2px; }
.call-row .tag { font-size: 0.72rem; font-weight: 600; min-width: 140px; }
.call-row .body { flex: 1; }
.call-row .transcript { font-size: 0.78rem; color: #94a3b8; line-height: 1.4; }
.call-row .loc { font-size: 0.68rem; color: #22c55e; margin-top: 2px; }
.cat-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.62rem; margin-left: 4px; vertical-align: middle; }
#live-dot { width: 8px; height: 8px; background: #22c55e; border-radius: 50%; display: inline-block; margin-right: 6px; animation: blink 1s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed" class="active">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="https://kevinwatkins.grafana.net/public-dashboards/40592df4da7946c7861619906c8de92c" target="_blank">📊 Stats</a>
    <a href="/tip">Submit Tip</a>
  </nav>
</div>
<div id="breaking"></div>
<div id="content">
  <div class="section-title"><span id="live-dot"></span>Active Incidents</div>
  <div id="incidents-section"></div>
  <div class="section-title" style="margin-top:28px">Recent Radio Activity</div>
  <div id="feed-section"></div>
</div>
<script>
const CAT_COLORS = {"APD":"#3b82f6","TCSO":"#3b82f6","UTPD":"#3b82f6","DPS":"#a855f7","AFD":"#f97316","TCFD":"#f97316","TCEMS":"#22c55e","ABIA":"#eab308","Unknown":"#475569"};

function timeStr(ts) { return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); }
function timeAgo(ts) { const m=Math.round((Date.now()/1000-ts)/60); return m<60?`${m}m ago`:`${Math.round(m/60)}h ago`; }

async function refresh() {
  const [callsR, activeR, allR] = await Promise.all([
    fetch('/api/calls'), fetch('/api/incidents/active'), fetch('/api/incidents')]);
  const calls = await callsR.json();
  const active = await activeR.json();
  const all = await allR.json();

  // Breaking bar — never show test incidents
  const realActive = active.filter(i => !i.is_test);
  const bar = document.getElementById('breaking');
  if (realActive.length) { bar.textContent='⚠ BREAKING: '+realActive.map(i=>i.itype+(i.location?' @ '+i.location:'')).join(' · '); bar.classList.add('show'); }
  else bar.classList.remove('show');

  // Incidents — never show test incidents
  const realAll = all.filter(i => !i.is_test);
  const inc = document.getElementById('incidents-section');
  if (!realAll.length) { inc.innerHTML='<p style="color:#475569;font-size:0.8rem">No incidents in the last 48 hours.</p>'; }
  else inc.innerHTML = realAll.map(i => {
    let ag=''; try{ag=JSON.parse(i.agencies||'[]').join(', ');}catch(e){}
    return `<div class="incident-card ${i.status}">
      <div class="itype">${i.itype}${i.location?' <span style="font-weight:400;color:#94a3b8;font-size:0.85rem">@ ${i.location}</span>':''}</div>
      <div class="meta">${new Date(i.ts_start*1000).toLocaleString()} · ${timeAgo(i.ts_start)} · ${i.status.toUpperCase()} · ${ag}</div>
      <div class="desc">${i.description||''}</div>
    </div>`;
  }).join('');

  // Feed
  const feed = document.getElementById('feed-section');
  feed.innerHTML = calls.slice(0,60).map(c => {
    const color = CAT_COLORS[c.category]||'#475569';
    return `<div class="call-row">
      <div class="time">${timeStr(c.ts)}</div>
      <div class="tag" style="color:${color}">${c.tag||'TGID '+c.tgid}<span class="cat-badge" style="background:${color}22;color:${color}">${c.category||'?'}</span></div>
      <div class="body">
        <div class="transcript">${c.transcript||'<em style="color:#334155">transcribing...</em>'}</div>
        ${c.location?`<div class="loc">&#9654; ${c.location}</div>`:''}
      </div>
    </div>`;
  }).join('');
}

refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>
"""

PUBLIC_ABOUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>About — Battle Buddy Austin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Battle Buddy monitors Austin public safety radio 24/7 — AI transcription, incident detection, homicide mapping, and air asset tracking. Built for journalists and community members.">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0a0f1e;color:#e2e8f0;line-height:1.6}
#topbar{background:#0f1729;border-bottom:1px solid #1e3a5f;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
#topbar .logo{font-size:1.1rem;font-weight:700;color:#3b82f6;letter-spacing:3px}
#topbar .tagline{font-size:0.72rem;color:#64748b}
#topbar .nav{margin-left:auto;display:flex;gap:16px}
#topbar .nav a{color:#94a3b8;text-decoration:none;font-size:0.8rem}
#topbar .nav a:hover,#topbar .nav a.active{color:#3b82f6}
#stats-strip{background:#060c18;border-bottom:1px solid #1e3a5f;padding:12px 24px;display:flex;justify-content:center;gap:48px;flex-wrap:wrap}
.sstat{text-align:center}
.sstat-num{font-size:1.5rem;font-weight:800;color:#3b82f6}
.sstat-label{font-size:0.65rem;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px}
.live-pip{display:inline-flex;align-items:center;gap:4px;font-size:0.6rem;color:#fca5a5;margin-left:6px}
.live-dot{width:5px;height:5px;background:#ef4444;border-radius:50%;animation:blink 1s infinite;display:inline-block}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.2}}
#hero-wrap{position:relative;overflow:hidden}
#hero-bg{position:absolute;inset:0;background-image:url('/static/bgbattlebuddy.png');background-size:cover;background-position:center top;filter:brightness(0.28) saturate(0.7);z-index:0}
#hero-overlay{position:absolute;inset:0;background:linear-gradient(to bottom,rgba(10,15,30,0.2) 0%,rgba(10,15,30,0.75) 70%,rgba(10,15,30,1) 100%);z-index:1}
#hero{position:relative;z-index:2;padding:90px 24px 72px;text-align:center;max-width:760px;margin:0 auto}
#atak-showcase{padding:72px 24px;background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f}
.atak-inner{max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center}
@media(max-width:700px){.atak-inner{grid-template-columns:1fr}}
.atak-screen{border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)}
.atak-screen img{width:100%;display:block}
.atak-copy .eyebrow{font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:12px}
.atak-copy h2{font-size:1.6rem;color:#f8fafc;font-weight:800;line-height:1.25;margin-bottom:16px}
.atak-copy p{font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px}
.atak-bullets{list-style:none;margin:20px 0 0}
.atak-bullets li{font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px}
.atak-bullets li:last-child{border-bottom:none}
.atak-bullets li::before{content:"";width:6px;height:6px;background:#3b82f6;border-radius:50%;flex-shrink:0}
.hero-label{font-size:0.7rem;letter-spacing:4px;color:#3b82f6;text-transform:uppercase;margin-bottom:20px}
#hero h1{font-size:clamp(1.9rem,4vw,2.9rem);font-weight:800;color:#f8fafc;line-height:1.2;margin-bottom:22px}
#hero h1 em{color:#3b82f6;font-style:normal}
#hero .lead{font-size:1rem;color:#94a3b8;line-height:1.75;max-width:600px;margin:0 auto 32px}
.btn-primary{display:inline-block;background:#3b82f6;color:white;padding:13px 34px;border-radius:8px;text-decoration:none;font-weight:700;font-size:0.95rem}
.btn-primary:hover{background:#2563eb}
#pillars{background:#0f1729;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f;padding:52px 24px}
.pillar-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0;max-width:900px;margin:0 auto}
.pillar{text-align:center;padding:28px 24px;border-right:1px solid #1e3a5f}
.pillar:last-child{border-right:none}
@media(max-width:640px){.pillar{border-right:none;border-bottom:1px solid #1e3a5f}}
.pillar .pnum{font-size:2.4rem;font-weight:900;color:#1e3a5f;line-height:1;margin-bottom:10px}
.pillar h3{font-size:1rem;color:#f8fafc;margin-bottom:8px;font-weight:700}
.pillar p{font-size:0.82rem;color:#64748b;line-height:1.6}
.section-wrap{padding:64px 24px}
.section-header{text-align:center;margin-bottom:44px}
.section-header .eyebrow{font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:8px}
.section-header h2{font-size:1.6rem;color:#f8fafc;margin-bottom:10px}
.section-header p{font-size:0.88rem;color:#64748b;max-width:520px;margin:0 auto}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;max-width:1000px;margin:0 auto}
.feature{background:#0f1729;border:1px solid #1e3a5f;border-radius:10px;padding:20px}
.feature .icon{font-size:1.5rem;margin-bottom:10px}
.feature h3{font-size:0.88rem;color:#f8fafc;margin-bottom:5px;font-weight:600}
.feature p{font-size:0.78rem;color:#64748b;line-height:1.55}
.badge{font-size:0.58rem;background:#1e3a5f;color:#60a5fa;padding:2px 6px;border-radius:3px;vertical-align:middle;margin-left:4px;white-space:nowrap}
#methodology{background:#0f1729;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f;padding:64px 24px}
.method-content{max-width:720px;margin:0 auto}
.method-step{display:flex;gap:20px;margin-bottom:30px;align-items:flex-start}
.step-num{flex-shrink:0;width:34px;height:34px;border:1px solid #1e3a5f;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:0.72rem;color:#3b82f6;font-weight:700}
.method-step h4{font-size:0.88rem;color:#f8fafc;margin-bottom:4px;font-weight:600}
.method-step p{font-size:0.8rem;color:#64748b;line-height:1.6}
.audience-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;max-width:900px;margin:0 auto}
.audience-card{background:#0f1729;border:1px solid #1e3a5f;border-radius:10px;padding:20px}
.audience-card .icon{font-size:1.4rem;margin-bottom:10px}
.audience-card h4{font-size:0.88rem;color:#f8fafc;margin-bottom:6px;font-weight:600}
.audience-card p{font-size:0.78rem;color:#64748b;line-height:1.5}
#cta{background:linear-gradient(135deg,#0f1729,#1a2d4a);border-top:1px solid #1e3a5f;padding:72px 24px;text-align:center}
#cta h2{font-size:1.7rem;color:#f8fafc;margin-bottom:12px}
#cta p{color:#64748b;font-size:0.9rem;max-width:460px;margin:0 auto 28px}
footer{background:#0a0f1e;border-top:1px solid #0f1729;padding:20px 24px;text-align:center;font-size:0.7rem;color:#334155}
footer a{color:#3b82f6;text-decoration:none}
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about" class="active">About</a>
    <a href="https://kevinwatkins.grafana.net/public-dashboards/40592df4da7946c7861619906c8de92c" target="_blank">📊 Stats</a>
  </nav>
</div>

<div id="stats-strip">
  <div class="sstat">
    <div class="sstat-num" id="ss-calls">—</div>
    <div class="sstat-label">Calls / 24h <span class="live-pip"><span class="live-dot"></span>live</span></div>
  </div>
  <div class="sstat">
    <div class="sstat-num" id="ss-incidents">—</div>
    <div class="sstat-label">Incidents Detected / 24h</div>
  </div>
  <div class="sstat">
    <div class="sstat-num" id="ss-homicides">—</div>
    <div class="sstat-label">Homicides Mapped — 2026</div>
  </div>
  <div class="sstat">
    <div class="sstat-num" id="ss-agencies">—</div>
    <div class="sstat-label">Agencies Monitored</div>
  </div>
</div>

<div id="hero-wrap">
<div id="hero-bg"></div>
<div id="hero-overlay"></div>
<div id="hero">
  <div class="hero-label">&#9652; Battle Buddy</div>
  <h1>Before the tweet.<br>Before the article.<br><em>Before anyone else knows.</em></h1>
  <p class="lead">Battle Buddy monitors every Austin public safety radio channel around the clock — transcribing, classifying, geocoding, and mapping incidents the moment they happen. No scanner. No waiting. No missed calls.</p>
  <a href="/public" class="btn-primary">Open Live Map</a>
</div>
</div><!-- /hero-wrap -->

<div id="pillars">
  <div class="pillar-grid">
    <div class="pillar">
      <div class="pnum">01</div>
      <h3>Speed</h3>
      <p>Incidents are detected and mapped within seconds of the first radio transmission — before any news desk, tweet, or dispatch alert reaches the public.</p>
    </div>
    <div class="pillar">
      <div class="pnum">02</div>
      <h3>Completeness</h3>
      <p>Every APD, AFD, DPS, Travis County EMS, UT Police, and ABIA transmission captured simultaneously. Battle Buddy never tunes out, never takes a break.</p>
    </div>
    <div class="pillar">
      <div class="pnum">03</div>
      <h3>Verification</h3>
      <p>Every homicide marker links directly to the official APD press release. Scanner intelligence cross-referenced with confirmed public records — not speculation.</p>
    </div>
  </div>
</div>

<div class="section-wrap">
  <div class="section-header">
    <div class="eyebrow">Platform Features</div>
    <h2>Eleven Intelligence Layers. One Feed.</h2>
    <p>Running simultaneously, 24 hours a day, across Austin and Travis County.</p>
  </div>
  <div class="feature-grid">
    <div class="feature">
      <div class="icon">📡</div>
      <h3>P25 Radio Monitoring</h3>
      <p>Software-defined radio captures the Austin GATRRS P25 trunked radio system (WPQY813) across all public safety talkgroups simultaneously, 24/7. Every transmission logged.</p>
    </div>
    <div class="feature">
      <div class="icon">🤖</div>
      <h3>AI Transcription</h3>
      <p>OpenAI Whisper converts every transmission to searchable text in near-real-time. Every call timestamped, tagged by talkgroup, and stored for the complete incident window.</p>
    </div>
    <div class="feature">
      <div class="icon">🔍</div>
      <h3>Incident Detection &amp; Classification</h3>
      <p>AI classifies incidents automatically — shootings, structure fires, SWAT activations, officer-down calls, pursuits, hazmat, mass casualties, and more.</p>
    </div>
    <div class="feature">
      <div class="icon">📈</div>
      <h3>Escalation Tracking</h3>
      <p>From welfare check to K-9 standoff, Battle Buddy tracks the full chain as incidents escalate across dispatch, field, and tactical channels — following the radio traffic as it moves.</p>
    </div>
    <div class="feature">
      <div class="icon">🗺️</div>
      <h3>Live Incident Map</h3>
      <p>Every incident plotted in real time with address, agency, incident type, and transcript excerpt. Geographic patterns visible at a glance across the entire metro.</p>
    </div>
    <div class="feature">
      <div class="icon">⚡</div>
      <h3>Instant Subscriber Alerts</h3>
      <p>Critical incidents trigger direct alerts the moment they are detected — before any public notification, press release, or news broadcast exists.</p>
    </div>
    <div class="feature">
      <div class="icon">🚁</div>
      <h3>Air Asset Tracking</h3>
      <p>ADS-B transponder data monitored continuously. When APD Air1, STAR Flight, or any low-altitude helicopter enters Austin airspace, Battle Buddy maps it and alerts subscribers — intelligence that persists even when radio goes encrypted.</p>
    </div>
    <div class="feature">
      <div class="icon">📰</div>
      <h3>APD Press Release Monitor</h3>
      <p>Austin Police Department press releases are automatically retrieved within 5 minutes of publication. Homicides and major incidents are geocoded, mapped, and cross-referenced with scanner intelligence.</p>
    </div>
    <div class="feature">
      <div class="icon">🔴</div>
      <h3>Austin Homicide Map</h3>
      <p>Every confirmed 2026 Austin homicide sourced directly from official APD press releases — geocoded, mapped, and linked to the source document. Heat map and incident markers. Self-updating. <a href="/public/homicides" style="color:#3b82f6">View it live.</a></p>
    </div>
    <div class="feature">
      <div class="icon">🛸</div>
      <h3>FAA Remote ID Drone Detection <span class="badge">COMING SOON</span></h3>
      <p>FAA Remote ID broadcasts from licensed drones captured via software-defined radio and plotted on the live map in real time — adding aerial dimension to ground-level situational awareness.</p>
    </div>
    <div class="feature">
      <div class="icon">🗞️</div>
      <h3>Intel News Feed</h3>
      <p>Every confirmed incident and APD press release delivered as a live RSS feed directly in Nextcloud News — auto-subscribed at signup. One scrollable feed covering radio detections, press releases, and homicide updates across Austin.</p>
    </div>
  </div>
</div>

<div class="section-wrap" style="background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f;padding:72px 24px">
  <div style="max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:56px;align-items:center">
    <div style="border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)">
      <img src="/static/nextcloud_ecosystem.png" alt="Battle Buddy connected ecosystem across laptop, phone, and tablet" loading="lazy" style="width:100%;display:block"/>
    </div>
    <div>
      <div class="eyebrow">Connected Platform</div>
      <h2 style="margin-bottom:16px">Every Device.<br>One Intelligence Feed.</h2>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:12px">Battle Buddy is built on a private Nextcloud platform — not a generic SaaS stack. Subscribers get access to a full ecosystem of connected apps alongside the real-time intelligence feed, all hosted on the same hardened infrastructure.</p>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:20px">Every app runs on the same server as the scanner pipeline. No third-party data exposure. Accessible from any device — phone, tablet, laptop — in the field or at a desk.</p>
      <ul style="list-style:none;margin:0;padding:0">
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Talk</strong>&nbsp;— encrypted team messaging; where incident alerts are delivered</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Files</strong>&nbsp;— shared briefings, incident archives, and ATAK data packages</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Calendar</strong>&nbsp;— event coordination and assignment scheduling</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Maps</strong>&nbsp;— offline maps for field deployment without cell connectivity</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Notes</strong>&nbsp;— field intel and incident notes synced across all devices</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">News</strong>&nbsp;— live incident and press release feed, auto-subscribed at signup</li>
      </ul>
    </div>
  </div>
</div>

<section id="methodology">
  <div class="section-header">
    <div class="eyebrow">How It Works</div>
    <h2>Radio Wave to Mapped Incident in Seconds</h2>
    <p>A fully automated pipeline with no human in the loop.</p>
  </div>
  <div class="method-content">
    <div class="method-step">
      <div class="step-num">1</div>
      <div>
        <h4>P25 Radio Capture</h4>
        <p>A software-defined radio receiver continuously monitors the GATRRS P25 trunked radio system — the shared radio backbone for APD, AFD, DPS, Travis County EMS, UT Police, ABIA, and Austin Energy. Every active talkgroup is captured simultaneously, 24 hours a day.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">2</div>
      <div>
        <h4>Whisper Transcription</h4>
        <p>Each recorded transmission is passed to OpenAI Whisper running locally. Audio is transcribed to text within seconds, tagged with talkgroup ID, timestamp, and call duration, then logged to the incident database.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">3</div>
      <div>
        <h4>AI Incident Detection</h4>
        <p>Transcripts are analyzed by a large language model that classifies the call type, extracts location information, and determines whether an active incident should be opened, updated, or escalated. Address strings are geocoded in real time against Austin and Travis County data.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">4</div>
      <div>
        <h4>Press Release Cross-Reference</h4>
        <p>APD public press releases are polled every 5 minutes. Confirmed homicides, fatal shootings, and major incidents are automatically pulled, geocoded, and added to the homicide map — linked directly to the official source document so every data point is verifiable.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">5</div>
      <div>
        <h4>Live Map, Archive &amp; Alerts</h4>
        <p>Active incidents are plotted on the live map and pushed to subscribers. Incidents are tracked until cleared, then archived with full radio traffic transcripts, geocoded address, agency attribution, and escalation chain for the complete incident window.</p>
      </div>
    </div>
  </div>
</section>

<section id="atak-showcase">
  <div class="atak-inner">
    <div class="atak-screen">
      <img src="/static/atak_screenshot.png" alt="Battle Buddy incidents displayed as CoT markers on ATAK — Austin aerial view" loading="lazy"/>
    </div>
    <div class="atak-copy">
      <div class="eyebrow">TAK Integration</div>
      <h2>Live Incidents.<br>On Your Tactical Map.</h2>
      <p>Every incident Battle Buddy detects is automatically pushed to FreeTAKServer as a CoT marker — appearing in real time on WinTAK, ATAK, and iTAK displays across your team.</p>
      <p>No manual entry. No copy-paste. The moment a shooting or structure fire is confirmed, a red marker hits the map at the geocoded address with incident type, timestamp, and description.</p>
      <ul class="atak-bullets">
        <li>CoT markers auto-post on incident detection</li>
        <li>Markers auto-clear when incident closes</li>
        <li>Works with WinTAK, ATAK Phone, iTAK</li>
        <li>Connects via FreeTAKServer over SSL — radiodesk.ddns.net</li>
        <li>Incident type drives marker color and stale time</li>
      </ul>
    </div>
  </div>
</section>

<div class="section-wrap">
  <div class="section-header">
    <div class="eyebrow">Who Uses Battle Buddy</div>
    <h2>Built for Anyone Who Needs to Know</h2>
  </div>
  <div class="audience-grid">
    <div class="audience-card">
      <div class="icon">📰</div>
      <h4>Journalists &amp; News Desks</h4>
      <p>Beat reporters and assignment desks covering Austin crime, fire, and public safety. Know about breaking incidents before any official statement exists.</p>
    </div>
    <div class="audience-card">
      <div class="icon">🏘️</div>
      <h4>Community Members</h4>
      <p>Residents who want to understand what is actually happening in their neighborhoods — verified and mapped rather than rumor-driven social media posts.</p>
    </div>
    <div class="audience-card">
      <div class="icon">🔬</div>
      <h4>Researchers &amp; Analysts</h4>
      <p>Academics, policy analysts, and public safety researchers who need incident-level data with timestamps, locations, and agency attribution.</p>
    </div>
    <div class="audience-card">
      <div class="icon">⚖️</div>
      <h4>Legal &amp; Insurance Professionals</h4>
      <p>Attorneys, investigators, and adjusters who need timestamped incident records cross-referenced with official press releases and radio traffic logs.</p>
    </div>
  </div>
</div>

<section id="cta">
  <h2>Austin Does Not Slow Down.<br>Neither Do We.</h2>
  <p>Subscriber access includes real-time alerts, full incident history, and the complete intelligence feed — built for people who need to know right now.</p>
  <a href="mailto:admin@libertas.mobi" class="btn-primary">Request Subscriber Access</a>
</section>

<footer>
  &copy; 2026 Battle Buddy &nbsp;&middot;&nbsp; Austin Metro Public Safety Intelligence &nbsp;&middot;&nbsp;
  <a href="/public">Live Map</a> &nbsp;&middot;&nbsp;
  <a href="/public/homicides">Homicide Map</a> &nbsp;&middot;&nbsp;
  <a href="/public/feed">Live Feed</a> &nbsp;&middot;&nbsp;
  <a href="/public/about">About</a>
</footer>

<script>
async function loadStats() {
  try {
    const r = await fetch("/api/stats");
    const d = await r.json();
    document.getElementById("ss-calls").textContent = d.calls_24h.toLocaleString();
    document.getElementById("ss-incidents").textContent = d.incidents_24h.toLocaleString();
    document.getElementById("ss-agencies").textContent = d.agencies_24h.toLocaleString();
  } catch(e) {}
  try {
    const r2 = await fetch("/api/homicides");
    const d2 = await r2.json();
    const all = (d2.homicides || []).concat(d2.live || []);
    let total = 0;
    all.forEach(function(h){ total += (h.count || 1); });
    document.getElementById("ss-homicides").textContent = total;
  } catch(e) {}
}
loadStats();
setInterval(loadStats, 60000);
</script>
</body>
</html>
"""


@app.route("/splash")
def public_splash():
    return PUBLIC_SPLASH_HTML

@app.route("/public")
def public_map():
    return PUBLIC_MAP_HTML


@app.route("/api/homicides")
def api_homicides():
    """Return 2026 homicide data for the heat map — static seed + live DB incidents."""
    import os
    seed_path = "/opt/battlebuddy/homicides_2026.json"
    seed = []
    if os.path.exists(seed_path):
        try:
            with open(seed_path) as f:
                seed = json.load(f)
        except Exception:
            pass

    # Only pull confirmed homicides from DB (APD press release sourced)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, ts_start, itype, description, location, lat, lon
           FROM incidents
           WHERE itype = 'HOMICIDE'
             AND lat IS NOT NULL AND lon IS NOT NULL
             AND ts_start > strftime('%s','2026-01-01')
             AND is_test = 0"""
    ).fetchall()
    conn.close()

    live = []
    for r in rows:
        live.append({
            "source": "scanner",
            "date": __import__('datetime').datetime.fromtimestamp(r[1]).strftime('%Y-%m-%d'),
            "itype": r[2],
            "summary": r[3][:120] if r[3] else "",
            "address": r[4] or "",
            "lat": r[5],
            "lon": r[6],
            "url": ""
        })

    return jsonify({"homicides": seed, "live": live})

@app.route("/public/feed")
def public_feed():
    return PUBLIC_FEED_HTML



@app.route("/public/feed.rss")
def public_feed_rss():
    """RSS 2.0 feed of confirmed Battle Buddy incidents (last 200, 30 days)."""
    cutoff = time.time() - 86400 * 30
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, ts_start, itype, location, description, article_url FROM incidents "
        "WHERE ts_start > ? AND is_test = 0 "
        "ORDER BY ts_start DESC LIMIT 200",
        (cutoff,)
    ).fetchall()
    conn.close()

    def _esc(s):
        if not s:
            return ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _clean_desc(s):
        if not s:
            return ""
        # JS/JSON noise from Google News article scraping starts at patterns like
        # ","key":  or  ",true,  or  ",[  — cut everything from that point.
        noise = re.search(r'\\["\']', s)
        if noise:
            s = s[:noise.start()].rstrip('., \t')
        s = re.sub(r'\s+', ' ', s).strip()
        if len(s) > 350:
            s = s[:350].rsplit(' ', 1)[0] + "..."
        return s

    items = []
    for inc_id, ts, itype, location, description, article_url in rows:
        dt = datetime.utcfromtimestamp(ts)
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        title_loc = f" — {location}" if location else ""
        # For press releases, pull title from description for a cleaner feed title
        if description and description.startswith("[APD Press Release]"):
            pr_title = description[len("[APD Press Release] "):].split(".")[0].strip()
            title = _esc(f"[{itype}] {pr_title[:80]}" if pr_title else f"[{itype}]{title_loc}")
        else:
            title = _esc(f"[{itype}]{title_loc}")
        desc = _esc(_clean_desc(description or itype))
        guid = f"https://battlebuddy.news/public/incident/{inc_id}"
        items.append(
            f"    <item>\n"
            f"      <title>{title}</title>\n"
            f"      <description>{desc}</description>\n"
            f"      <pubDate>{pub_date}</pubDate>\n"
            f"      <guid isPermaLink=\"false\">{guid}</guid>\n"
            f"      <link>{_esc(article_url) if article_url else 'https://battlebuddy.news/public'}</link>\n"
            f"    </item>"
        )

    body = "\n".join(items)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        '  <channel>\n'
        '    <title>Battle Buddy — Austin Public Safety Intelligence</title>\n'
        '    <link>https://battlebuddy.news/public</link>\n'
        '    <description>Real-time confirmed incidents from Austin, TX public safety radio and press releases.</description>\n'
        '    <language>en-us</language>\n'
        f'{body}\n'
        '  </channel>\n'
        '</rss>'
    )
    from flask import Response
    return Response(xml, mimetype="application/rss+xml")


HOMICIDE_MAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Austin Homicide Map 2026 — Battle Buddy</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat/dist/leaflet-heat.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0a0f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif}
    #topbar{display:flex;align-items:center;gap:16px;padding:10px 20px;background:#0f0f1a;border-bottom:1px solid #1e293b;position:sticky;top:0;z-index:1000}
    .logo{font-weight:800;font-size:1.1rem;color:#f1f5f9;letter-spacing:1px}
    .tagline{font-size:.75rem;color:#64748b;flex:1}
    nav a{color:#94a3b8;text-decoration:none;font-size:.85rem;padding:4px 10px;border-radius:4px;transition:all .2s}
    nav a:hover,nav a.active{color:#f1f5f9;background:#1e293b}
    #header{padding:20px 24px 12px;border-bottom:1px solid #1e293b}
    #header h1{font-size:1.4rem;color:#f8fafc;margin-bottom:4px}
    #header p{font-size:.85rem;color:#64748b}
    #stats-bar{display:flex;gap:24px;padding:12px 24px;background:#0f0f1a;border-bottom:1px solid #1e293b;font-size:.8rem}
    .stat{color:#94a3b8}.stat span{color:#ef4444;font-weight:700;font-size:1rem}
    #controls{display:flex;gap:12px;padding:10px 24px;background:#0f0f1a;border-bottom:1px solid #1e293b;align-items:center;flex-wrap:wrap}
    .ctrl-btn{background:#1e293b;border:1px solid #334155;color:#94a3b8;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:.8rem;transition:all .2s}
    .ctrl-btn.active,.ctrl-btn:hover{background:#1d4ed8;border-color:#3b82f6;color:#fff}
    #map{height:calc(100vh - 220px);width:100%;position:relative}
    #hmap-legend{position:absolute;bottom:40px;right:12px;z-index:1000;background:rgba(10,10,15,.92);border:1px solid #1e293b;border-radius:8px;padding:14px 18px;font-size:.75rem;min-width:190px;backdrop-filter:blur(4px)}
    #hmap-legend h4{color:#94a3b8;font-size:.68rem;letter-spacing:1px;text-transform:uppercase;margin-bottom:10px}
    .hleg-row{display:flex;align-items:center;gap:8px;margin:5px 0;color:#cbd5e1}
    .hleg-dot{width:11px;height:11px;border-radius:50%;flex-shrink:0;border:1.5px solid rgba(255,255,255,.25)}
    .hleg-heat{width:52px;height:8px;border-radius:4px;flex-shrink:0;background:linear-gradient(to right,#1d4ed8,#7c3aed,#dc2626,#ea580c,#fbbf24)}
    .hleg-sq{width:11px;height:11px;border-radius:2px;flex-shrink:0}
    .hleg-divider{border:none;border-top:1px solid #1e293b;margin:8px 0}
    .incident-popup h3{font-size:.9rem;color:#ef4444;margin-bottom:4px}
    .incident-popup p{font-size:.78rem;color:#94a3b8;margin:2px 0}
    .incident-popup a{color:#3b82f6;font-size:.78rem}
    .legend{background:#0f0f1a;border:1px solid #1e293b;padding:10px 14px;border-radius:8px;font-size:.75rem;color:#94a3b8}
    .legend-row{display:flex;align-items:center;gap:8px;margin:3px 0}
    .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
    footer{text-align:center;padding:12px;font-size:.75rem;color:#475569;border-top:1px solid #1e293b}
    #methodology{background:#0a0a0f;border-top:1px solid #1e293b}
    #sources-bar{display:flex;gap:0;flex-wrap:wrap;border-bottom:1px solid #1e293b}
    .source-block{flex:1;min-width:240px;padding:14px 20px;border-right:1px solid #1e293b}
    .source-block:last-child{border-right:none}
    .source-badge{font-size:.7rem;font-weight:700;padding:2px 7px;border-radius:3px;margin-right:6px}
    .source-badge.verified{background:#14532d;color:#4ade80}
    .source-badge.scanner{background:#1e3a5f;color:#60a5fa}
    .source-badge.live{background:#3b1f00;color:#fb923c}
    .source-label{font-size:.82rem;font-weight:600;color:#e2e8f0}
    .source-desc{display:block;font-size:.75rem;color:#64748b;margin-top:4px}
    #nerd-box{border-top:1px solid #1e293b}
    #nerd-box summary{padding:12px 24px;cursor:pointer;font-size:.85rem;color:#64748b;user-select:none;list-style:none}
    #nerd-box summary:hover{color:#94a3b8;background:#0f0f1a}
    #nerd-box summary::marker{display:none}
    .nerd-content{padding:20px 28px 24px;max-width:860px;font-size:.82rem;color:#94a3b8;line-height:1.7}
    .nerd-content h3{color:#e2e8f0;font-size:.9rem;margin:16px 0 6px;letter-spacing:.05em;text-transform:uppercase}
    .nerd-content p{margin-bottom:10px}
    .nerd-content a{color:#3b82f6}
    .nerd-content ul{margin:6px 0 10px 18px}
    .nerd-content li{margin-bottom:4px}
  </style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/homicides" class="active">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="/tip">Submit Tip</a>
  </nav>
</div>
<div id="header">
  <h1>&#128308; Austin Homicide Map — 2026</h1>
  <p>All confirmed homicides in Austin from January 1, 2026 to present. Click any marker for details and press release links.</p>
</div>
<div id="stats-bar">
  <div class="stat">Total homicides: <span id="total">—</span> <span style="font-size:0.75rem;color:#94a3b8">(<a href="#methodology" style="color:#94a3b8">includes 3 victims from Mar 1 mass shooting — 1 marker</a>)</span></div>
  <div class="stat">Most recent: <span id="latest" style="color:#f59e0b">—</span></div>
  <div class="stat">Hot zone: <span id="hotzone" style="color:#f97316">—</span></div>
  <div class="stat" style="margin-left:auto;color:#475569">Source: APD press releases + Battle Buddy scanner</div>
</div>
<div id="controls">
  <span style="font-size:.8rem;color:#64748b">View:</span>
  <button class="ctrl-btn active" onclick="setMode('heat')" id="btn-heat">Heat Map</button>
  <button class="ctrl-btn" onclick="setMode('markers')" id="btn-markers">Markers</button>
  <button class="ctrl-btn" onclick="setMode('both')" id="btn-both">Both</button>
</div>
<div id="map">
  <div id="hmap-legend">
    <h4>&#9650; Legend</h4>
    <div class="hleg-row"><div class="hleg-heat"></div><span>Incident density</span></div>
    <hr class="hleg-divider"/>
    <div class="hleg-row"><div class="hleg-dot" style="background:#ef4444"></div><span>APD Press Release (verified)</span></div>
    <div class="hleg-row"><div class="hleg-dot" style="background:#f59e0b"></div><span>Scanner detection</span></div>
    <hr class="hleg-divider"/>
    <div class="hleg-row"><div class="hleg-sq" style="background:#7f1d1d;border:1px solid #ef4444"></div><span>Shooting / Homicide</span></div>
    <div class="hleg-row"><div class="hleg-sq" style="background:#1e1b4b;border:1px solid #818cf8"></div><span>Stabbing</span></div>
    <div class="hleg-row"><div class="hleg-sq" style="background:#1c1917;border:1px solid #a8a29e"></div><span>Other violent crime</span></div>
    <hr class="hleg-divider"/>
    <div style="font-size:.68rem;color:#475569;margin-top:2px">Click any marker for details<br/>and press release links.</div>
  </div>
</div>

<div id="methodology">
  <div id="sources-bar">
    <div class="source-block">
      <span class="source-badge verified">&#10003; VERIFIED</span>
      <span class="source-label">APD Press Releases</span>
      <span class="source-desc">Official homicide announcements published at austintexas.gov/news. Each incident links directly to the source document.</span>
    </div>
    <div class="source-block">
      <span class="source-badge scanner">&#9632; SCANNER</span>
      <span class="source-label">Battle Buddy Scanner Detection</span>
      <span class="source-desc">Incidents detected via P25 radio monitoring and AI transcription. Not yet confirmed by press release.</span>
    </div>
    <div class="source-block">
      <span class="source-badge live">&#9654; LIVE</span>
      <span class="source-label">Self-Updating</span>
      <span class="source-desc">New APD press releases are detected automatically within 5 minutes of publication and added to this map.</span>
    </div>
  </div>

  <details id="nerd-box">
    <summary>&#128300; Methodology (for nerds)</summary>
    <div class="nerd-content">
      <h3>Data Sources</h3>
      <p><strong>Primary source:</strong> APD homicide press releases published at
      <a href="https://www.austintexas.gov/news?field_news_type_tid=75" target="_blank">austintexas.gov/news</a>.
      Battle Buddy polls this page every 5 minutes. New articles matching homicide/shooting/death keywords trigger
      automatic article retrieval, address extraction, and geocoding.</p>

      <p><strong>Secondary source:</strong> Battle Buddy&rsquo;s P25 radio scanner pipeline.
      The system monitors Austin&rsquo;s GATRRS trunked radio system (WPQY813, 851 MHz, P25 Phase II),
      transcribes audio using faster-whisper large-v3-turbo (INT8 quantized), and classifies incidents using
      Groq&rsquo;s llama-3.3-70b-versatile LLM. Scanner detections are flagged separately from press-release-verified incidents.</p>

      <h3>Geocoding</h3>
      <p>Street addresses are extracted from press release body text using regex pattern matching and geocoded
      via Nominatim (OpenStreetMap) with Austin, TX and Travis County, TX fallbacks for rural addresses.
      Incidents without a resolvable address are excluded from the map but still appear in Talk alerts.</p>

      <h3>Seed Dataset</h3>
      <p>The 2026 dataset was bootstrapped on April 6, 2026 by manually compiling all APD homicide press releases
      from January 1&ndash;April 5, 2026 (16 confirmed incidents, covering Austin&rsquo;s 1st through 18th homicide of the year).
      All seed records were individually verified against official press releases and geocoded.</p>

      <h3>Limitations</h3>
      <ul>
        <li>Homicides where APD has not yet published a press release will not appear in the verified dataset.</li>
        <li>Scanner detections depend on radio traffic being unencrypted. APD patrol channels (TGIDs 960&ndash;987)
        went to AES-256 encryption in March 2026, significantly reducing real-time APD intelligence.</li>
        <li>Geocoding accuracy is address-level (not GPS-precise). Block-range addresses are plotted at the midpoint.</li>
        <li>The March 1, 2026 mass shooting at 700 W 6th Street is counted as a single map point but represents 3 homicide victims (Austin&rsquo;s 12th&ndash;14th of the year).</li>
      </ul>

      <h3>Technology Stack</h3>
      <p>Battle Buddy runs on a Contabo VPS (Ubuntu 24.04, 24 GB RAM). Radio capture via RTL-SDR on a Raspberry Pi 5.
      P25 trunked decoding via OP25 (GNU Radio). Web stack: Python/Flask, SQLite, Nginx.
      Map: Leaflet.js + leaflet.heat. Geocoding: geopy/Nominatim.</p>
    </div>
  </details>
</div>

<footer>&copy; 2026 Battle Buddy &nbsp;&middot;&nbsp; Austin Metro Public Safety Intelligence</footer>

<script>
const map = L.map('map', {center: [30.307, -97.735], zoom: 11});
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19
}).addTo(map);

let heatLayer = null, markerGroup = L.layerGroup(), mode = 'heat';
let allPoints = [];

function setMode(m) {
  mode = m;
  ['heat','markers','both'].forEach(id => {
    document.getElementById('btn-'+id).classList.toggle('active', id === m);
  });
  render();
}

function render() {
  if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
  markerGroup.clearLayers();

  if (mode === 'heat' || mode === 'both') {
    heatLayer = L.heatLayer(allPoints.map(p => [p.lat, p.lon, 1.0]), {
      radius: 35, blur: 25, maxZoom: 14,
      gradient: {0.2:'#1d4ed8', 0.4:'#7c3aed', 0.6:'#dc2626', 0.8:'#ea580c', 1.0:'#fbbf24'}
    }).addTo(map);
  }

  if (mode === 'markers' || mode === 'both') {
    allPoints.forEach(p => {
      const icon = L.divIcon({
        className: '',
        html: '<div style="background:' + ((p.source==='scanner'?'#f59e0b':(p.itype==='STABBING'?'#818cf8':(p.itype==='WEAPONS'?'#a8a29e':'#ef4444')))) +
              ';width:12px;height:12px;border-radius:50%;border:2px solid rgba(255,255,255,.4)"></div>',
        iconSize: [12, 12], iconAnchor: [6, 6]
      });
      const popup = '<div class="incident-popup">' +
        '<h3>#' + (p.n||'') + ' ' + (p.itype||'HOMICIDE') + '</h3>' +
        '<p><b>Date:</b> ' + p.date + '</p>' +
        (p.victim ? '<p><b>Victim:</b> ' + p.victim + '</p>' : '') +
        '<p><b>Location:</b> ' + (p.address||'Unknown') + '</p>' +
        '<p>' + (p.summary||'') + '</p>' +
        (p.url ? '<a href="' + p.url + '" target="_blank">APD Press Release &#8599;</a>' : '') +
        '</div>';
      L.marker([p.lat, p.lon], {icon}).addTo(markerGroup).bindPopup(popup);
    });
    markerGroup.addTo(map);
  }
}

async function load() {
  const r = await fetch('/api/homicides');
  const d = await r.json();
  const seed = (d.homicides||[]).filter(h => h.lat && h.lon);
  const live = (d.live||[]).filter(h => h.lat && h.lon);
  allPoints = [
    ...seed,
    ...live.map(l => ({...l, n: null, victim: null}))
  ];

  document.getElementById('total').textContent = seed.reduce((s,h)=>s+(h.count||1),0) + live.reduce((s,h)=>s+(h.count||1),0);
  if (seed.length) {
    const latest = seed.slice().sort((a,b) => b.date.localeCompare(a.date))[0];
    document.getElementById('latest').textContent = latest.date + ' — ' + (latest.address||'');
  }

  // Find hottest neighborhood (rough grid cell with most hits)
  const grid = {};
  allPoints.forEach(p => {
    const key = (Math.round(p.lat*20)/20).toFixed(2) + ',' + (Math.round(p.lon*20)/20).toFixed(2);
    grid[key] = (grid[key]||0) + 1;
  });
  const hot = Object.entries(grid).sort((a,b) => b[1]-a[1])[0];
  if (hot && hot[1] > 1) document.getElementById('hotzone').textContent = hot[1] + ' incidents near ' + hot[0];

  render();
}

load();
</script>
</body>
</html>"""


@app.route("/public/homicides")
def public_homicides():
    return HOMICIDE_MAP_HTML

@app.route("/public/about")
def public_about():
    return PUBLIC_ABOUT_HTML


PUBLIC_AIRCRAFT_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Aircraft Tracker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Live low-altitude aircraft tracking over Austin, TX. Helicopters, police air assets, and EMS flight tracking.">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; display: flex; flex-direction: column; height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; flex-wrap: wrap; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover { color: #3b82f6; }
#topbar .nav a.active { color: #3b82f6; }
#map { flex: 1; }
#legend {
  position: absolute; bottom: 30px; left: 10px; z-index: 1000;
  background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f;
  border-radius: 8px; padding: 12px 16px; font-size: 0.72rem;
}
#legend h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.leg-item { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
#status-bar {
  position: absolute; bottom: 30px; right: 10px; z-index: 1000;
  background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f;
  border-radius: 8px; padding: 12px 16px; font-size: 0.72rem; min-width: 180px;
}
#status-bar h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.stat-row { display: flex; justify-content: space-between; gap: 16px; margin-bottom: 3px; color: #cbd5e1; }
.stat-val { color: #3b82f6; font-weight: 600; }
#no-aircraft {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
  z-index: 1000; background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f;
  border-radius: 8px; padding: 24px 32px; text-align: center; display: none;
}
#no-aircraft h3 { color: #64748b; margin-bottom: 8px; }
#no-aircraft p { color: #475569; font-size: 0.8rem; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Live Aircraft Tracker</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft" class="active">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
  </nav>
</div>
<div id="map"></div>
<div id="legend">
  <h4>Aircraft</h4>
  <div class="leg-item"><span style="font-size:18px;line-height:1;color:#f59e0b">🚁</span><span style="color:#f59e0b">LEO / EMS (APD, STAR Flight)</span></div>
  <div class="leg-item"><span style="font-size:18px;line-height:1;color:#a855f7">🚁</span><span style="color:#a855f7">Unknown helicopter &lt;5,000ft</span></div>
  <div class="leg-item">
    <svg width="32" height="6" style="flex-shrink:0">
      <line x1="0" y1="3" x2="32" y2="3" stroke="#f59e0b" stroke-width="2" stroke-dasharray="4 4"/>
    </svg>
    <span>30-min flight trail</span>
  </div>
</div>
<div id="status-bar">
  <h4>Status</h4>
  <div class="stat-row"><span>Aircraft tracked</span><span class="stat-val" id="s-count">—</span></div>
  <div class="stat-row"><span>LEO airborne</span><span class="stat-val" id="s-leo">—</span></div>
  <div class="stat-row"><span>Last update</span><span class="stat-val" id="s-time">—</span></div>
</div>
<div id="no-aircraft">
  <h3>No aircraft in range</h3>
  <p>No helicopters below 5,000ft detected within 60 miles of Austin.<br>Checking every 30 seconds.</p>
</div>
<script>
const map = L.map('map').setView([30.2672, -97.7431], 10);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &amp; CartoDB', maxZoom: 18
}).addTo(map);

const acMarkers = {};
const acTrails  = {};

function makeHeloIcon(isLeo) {
  const color = isLeo ? '#f59e0b' : '#a855f7';
  return L.divIcon({
    html: `<div style="font-size:22px;line-height:1;filter:drop-shadow(0 0 5px ${color});color:${color}">🚁</div>`,
    iconSize: [26,26], iconAnchor: [13,13], className: ''
  });
}

function popupHtml(ac) {
  const ago  = Math.round((Date.now()/1000 - ac.ts) / 60);
  const leo  = ac.is_leo ? '<div style="color:#f59e0b;font-weight:700;margin:4px 0">🔴 LAW ENFORCEMENT / EMS</div>' : '';
  const cs   = ac.callsign ? `<div>Flight: <b>${ac.callsign}</b></div>` : '';
  const hdg  = ac.heading  ? `${Math.round(ac.heading)}&deg;` : '?';
  const spd  = ac.speed_kts ? `${Math.round(ac.speed_kts)} kts` : '?';
  return `
    <div style="font-family:-apple-system,sans-serif;min-width:180px">
      <div style="font-size:15px;font-weight:700;margin-bottom:4px">${ac.label || ac.icao24}</div>
      ${leo}${cs}
      <div style="color:#64748b;font-size:12px">
        ICAO: ${ac.icao24}<br>
        Alt: <b>${ac.alt_ft ? ac.alt_ft.toLocaleString() + ' ft' : '?'}</b> &nbsp;
        Hdg: ${hdg} &nbsp; Spd: ${spd}<br>
        Updated ${ago}m ago
      </div>
    </div>`;
}

async function poll() {
  try {
    const resp = await fetch('/api/adsb');
    const aircraft = await resp.json();
    const seen = new Set();

    for (const ac of aircraft) {
      const key = ac.icao24;
      seen.add(key);

      const trailPts   = ac.trail.map(p => [p[0], p[1]]);
      const trailColor = ac.is_leo ? '#f59e0b' : '#a855f7';

      if (acTrails[key]) {
        acTrails[key].setLatLngs(trailPts);
      } else {
        acTrails[key] = L.polyline(trailPts, {
          color: trailColor, weight: 2, opacity: 0.55, dashArray: '5 5'
        }).addTo(map);
      }

      if (acMarkers[key]) {
        acMarkers[key].setLatLng([ac.lat, ac.lon]);
        acMarkers[key].setPopupContent(popupHtml(ac));
      } else {
        acMarkers[key] = L.marker([ac.lat, ac.lon], {icon: makeHeloIcon(ac.is_leo)})
          .bindPopup(popupHtml(ac))
          .addTo(map);
      }
    }

    // Remove aircraft that are gone
    for (const key of Object.keys(acMarkers)) {
      if (!seen.has(key)) {
        acMarkers[key].remove(); delete acMarkers[key];
        if (acTrails[key]) { acTrails[key].remove(); delete acTrails[key]; }
      }
    }

    const total = aircraft.length;
    const leo   = aircraft.filter(a => a.is_leo).length;
    document.getElementById('s-count').textContent = total;
    document.getElementById('s-leo').textContent   = leo;
    document.getElementById('s-time').textContent  = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    document.getElementById('no-aircraft').style.display = total === 0 ? 'block' : 'none';
  } catch(e) {
    document.getElementById('s-time').textContent = 'error';
  }
}

poll();
setInterval(poll, 30000);
</script>
</body>
</html>
"""


@app.route("/public/aircraft")
def public_aircraft():
    return PUBLIC_AIRCRAFT_HTML


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------




def _load_active_incidents_from_db():
    """On startup, reload active incidents into _active_incidents so _release_stale can close them.
    Any incident older than 4 hours is closed immediately as stale."""
    MAX_AGE = 4 * 3600  # close anything untouched for >4 hours
    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status='active' AND is_test=0"
    ).fetchall()
    conn.close()

    to_close_now = []
    loaded = 0
    for row in rows:
        inc = dict(row)
        iid = inc['id']
        age = now - inc['ts_updated']
        if age > MAX_AGE:
            to_close_now.append(iid)
        else:
            with _incident_lock:
                _active_incidents[iid] = {
                    'itype':            inc.get('itype', 'UNKNOWN'),
                    'ts_updated':       inc['ts_updated'],
                    'agencies':         set(json.loads(inc['agencies']) if inc.get('agencies') else []),
                    'tgids':            set(json.loads(inc['tgids'])    if inc.get('tgids')    else []),
                    'lat':              inc.get('lat'),
                    'lon':              inc.get('lon'),
                    'escalation_stage': None,
                }
            loaded += 1

    if to_close_now:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany(
            "UPDATE incidents SET status='cleared', ts_cleared=? WHERE id=?",
            [(now, iid) for iid in to_close_now]
        )
        conn.commit()
        conn.close()
        print(f"[incident] startup cleanup: closed {len(to_close_now)} stale incident(s) (>4h old)", flush=True)

    if loaded:
        print(f"[incident] startup: loaded {loaded} active incident(s) into memory for timeout tracking", flush=True)

def _atak_resync_on_startup():
    """Re-post ATAK markers for any incidents still active in the DB at startup."""
    if not FTS_ENABLED:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - 30 * 60
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status='active' AND ts_updated > ?"
        " AND location IS NOT NULL AND location != ''"
        "AND lat IS NOT NULL AND lon IS NOT NULL AND is_test=0",
        (cutoff,)
    ).fetchall()
    conn.close()
    count = 0
    for row in rows:
        inc = dict(row)
        threading.Thread(
            target=_atak_post_marker,
            args=(inc['id'], inc['lat'], inc['lon'], inc['itype'], inc['location'], inc.get('description')),
            daemon=True
        ).start()
        count += 1
    if count:
        print(f"[atak] startup resync: re-posted {count} active incident marker(s)", flush=True)


# ---------------------------------------------------------------------------
# Incident flagging — capture a full snapshot for demo/presentation use
# ---------------------------------------------------------------------------

def _nc_upload(path: str, data: bytes, content_type: str = "text/markdown"):
    """Upload a file to Nextcloud via WebDAV."""
    import urllib.request as _ur
    import base64 as _b64
    creds = _b64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    url   = f"{NC_WEBDAV}/{path}"
    req   = _ur.Request(url, data=data,
                        headers={"Authorization": f"Basic {creds}",
                                 "Content-Type": content_type},
                        method="PUT")
    try:
        _ur.urlopen(req, timeout=15)
        return True
    except Exception as exc:
        print(f"[flag] Nextcloud upload failed: {exc}", flush=True)
        return False


def _export_incident_snapshot(inc_id: int):
    """Build a markdown snapshot of a flagged incident and push to Nextcloud."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    inc = conn.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
    if not inc:
        conn.close()
        return
    inc = dict(inc)
    # Fetch all calls linked to this incident within its time window (+/- 5 min)
    t0 = inc["ts_start"] - 300
    t1 = (inc["ts_cleared"] or time.time()) + 300
    try:
        tgids = json.loads(inc.get("tgids") or "[]")
    except Exception:
        tgids = []
    calls = conn.execute(
        "SELECT ts, tgid, category, transcript, duration FROM calls "
        "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
        (t0, t1)
    ).fetchall()
    conn.close()

    from datetime import datetime as _dt
    start_str = _dt.fromtimestamp(inc["ts_start"]).strftime("%Y-%m-%d %H:%M:%S CDT")
    clear_str  = (_dt.fromtimestamp(inc["ts_cleared"]).strftime("%H:%M:%S CDT")
                  if inc.get("ts_cleared") else "ongoing")
    try:
        agencies = ", ".join(json.loads(inc.get("agencies") or "[]"))
    except Exception:
        agencies = "unknown"

    lines = [
        f"# Flagged Incident — {inc['itype']}",
        f"**Incident ID**: {inc_id}  ",
        f"**Time**: {start_str} → {clear_str}  ",
        f"**Location**: {inc.get('location') or 'No address extracted'}  ",
        f"**Coordinates**: {inc.get('lat')}, {inc.get('lon')}  ",
        f"**Agencies**: {agencies}  ",
        f"**TGIDs**: {', '.join(str(t) for t in tgids)}  ",
        f"**Status**: {inc['status'].upper()}  ",
        "",
        "## Description",
        inc.get("description") or "*(no description)*",
        "",
        "## Radio Traffic Timeline",
        f"*Calls within 5 minutes of incident window ({len(calls)} total)*",
        "",
    ]
    for c in calls:
        ts_str = _dt.fromtimestamp(c["ts"]).strftime("%H:%M:%S")
        tag    = c["category"] or f"TGID {c['tgid']}"
        xscr   = (c["transcript"] or "*(no transcript)*").strip()
        dur    = f"{c['duration']:.1f}s" if c["duration"] else ""
        lines.append(f"**{ts_str}** `{tag}` {dur}  ")
        lines.append(f"> {xscr}")
        lines.append("")

    lines += [
        "---",
        f"*Exported by Battle Buddy — {_dt.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*",
    ]

    slug     = start_str[:10].replace("-", "") + f"_{inc['itype'].replace(' ','_').replace('/','_')}_{inc_id}"
    filename = f"{NC_REPORT_DIR}/{slug}.md"
    data     = "\n".join(lines).encode("utf-8")

    # Ensure directory exists
    import urllib.request as _ur, base64 as _b64
    creds = _b64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    mkcol_req = _ur.Request(
        f"{NC_WEBDAV}/{NC_REPORT_DIR}",
        headers={"Authorization": f"Basic {creds}"},
        method="MKCOL"
    )
    try: _ur.urlopen(mkcol_req, timeout=10)
    except Exception: pass

    if _nc_upload(filename, data):
        print(f"[flag] snapshot uploaded: {filename}", flush=True)
    return filename


@app.route("/api/incidents/<int:inc_id>/flag", methods=["POST"])
def api_flag_incident(inc_id):
    """Flag an incident and export a snapshot to Nextcloud."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE incidents SET flagged=1 WHERE id=?", (inc_id,))
    conn.commit()
    conn.close()
    threading.Thread(target=_export_incident_snapshot, args=(inc_id,), daemon=True).start()
    return jsonify({"status": "flagged", "id": inc_id})


@app.route("/api/incidents/flagged")
def api_flagged_incidents():
    """Return all flagged incidents."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents WHERE flagged=1 ORDER BY ts_start DESC"
    ).fetchall()
    conn.close()
    return jsonify([_fill_incident_coords(dict(r)) for r in rows])

# ── TIP SUBMISSION SYSTEM ────────────────────────────────────────────────────
import os as _os
_os.makedirs(TIPS_UPLOAD_DIR, exist_ok=True)

_ALLOWED_TIP_EXT = {"jpg", "jpeg", "png", "gif", "webp"}

def _save_tip_photo(file) -> str | None:
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _ALLOWED_TIP_EXT:
        return None
    filename = uuid.uuid4().hex + "." + ext
    file.save(os.path.join(TIPS_UPLOAD_DIR, filename))
    return filename


def _notify_new_tip(tip_id: int, location_text: str, description: str,
                    photo_path: str | None, lat, lon, ts: float):
    """DM kevin when a new tip arrives. Runs in a thread."""
    token = _get_or_create_dm_room("kevin")
    if not token:
        print(f"[tip] could not get DM room for kevin", flush=True)
        return

    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %I:%M %p")
    coords_str = f"{lat:.5f}, {lon:.5f}" if (lat and lon) else "not geocoded"

    lines = [
        f"📍 NEW TIP #{tip_id} — review needed",
        f"",
        f"Location: {location_text}",
        f"Coords: {coords_str}",
        f"Time: {time_str}",
    ]
    if description:
        lines += ["", "What they saw:", description]
    if photo_path:
        lines += ["", f"📷 https://battlebuddy.news/static/tips/{photo_path}"]
    lines += [
        "",
        f"Review: https://battlebuddy.news/admin/tips",
        f"To investigate: ask me to look into tip #{tip_id}",
    ]

    _bot_reply(token, chr(10).join(lines))
    print(f"[tip] DM sent to kevin for tip #{tip_id}", flush=True)


TIP_FORM_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Submit a Tip</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; min-height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover { color: #3b82f6; }
#topbar .nav a.active { color: #3b82f6; }
.container { max-width: 600px; margin: 40px auto; padding: 0 20px 60px; }
h1 { font-size: 1.4rem; font-weight: 700; color: #e2e8f0; margin-bottom: 6px; }
.subtitle { color: #64748b; font-size: 0.85rem; margin-bottom: 32px; line-height: 1.5; }
.field { margin-bottom: 22px; }
label { display: block; font-size: 0.8rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
label .req { color: #ef4444; margin-left: 3px; }
input[type=text], input[type=datetime-local], textarea {
  width: 100%; background: #1e293b; border: 1px solid #1e3a5f; border-radius: 6px;
  color: #e2e8f0; padding: 10px 14px; font-size: 0.9rem; outline: none;
  font-family: inherit; transition: border-color 0.2s;
}
input[type=text]:focus, textarea:focus, input[type=datetime-local]:focus { border-color: #3b82f6; }
textarea { min-height: 120px; resize: vertical; line-height: 1.5; }
.file-label {
  display: flex; align-items: center; gap: 10px; background: #1e293b;
  border: 1px dashed #1e3a5f; border-radius: 6px; padding: 14px;
  cursor: pointer; transition: border-color 0.2s; color: #64748b; font-size: 0.85rem;
}
.file-label:hover { border-color: #3b82f6; color: #94a3b8; }
input[type=file] { display: none; }
#file-name { font-size: 0.8rem; color: #3b82f6; }
.hint { font-size: 0.75rem; color: #475569; margin-top: 6px; }
.anon-note { background: #0f1729; border: 1px solid #1e3a5f; border-radius: 8px; padding: 14px 16px; margin-bottom: 28px; font-size: 0.8rem; color: #64748b; line-height: 1.6; }
.anon-note strong { color: #94a3b8; }
button[type=submit] {
  width: 100%; background: #1d4ed8; color: #fff; border: none; border-radius: 8px;
  padding: 13px; font-size: 0.95rem; font-weight: 600; cursor: pointer;
  letter-spacing: 0.5px; transition: background 0.2s;
}
button[type=submit]:hover { background: #2563eb; }
.hp { display: none; }
#confirm { display: none; text-align: center; padding: 40px 20px; }
#confirm .check { font-size: 3rem; margin-bottom: 16px; }
#confirm h2 { color: #22c55e; margin-bottom: 10px; }
#confirm p { color: #64748b; font-size: 0.9rem; line-height: 1.6; }
#confirm a { color: #3b82f6; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="/tip" class="active">Submit Tip</a>
  </nav>
</div>
<div class="container">
  <div id="form-wrap">
    <h1>Submit a Tip</h1>
    <p class="subtitle">Saw something in Austin worth knowing about? City operations, unusual activity, police presence, encampments, infrastructure — anything that adds to the picture. All tips are reviewed before appearing anywhere.</p>
    <div class="anon-note"><strong>Anonymous by default.</strong> We do not log your IP address or require an account. What you submit is all we receive.</div>
    <form id="tip-form" enctype="multipart/form-data">
      <div class="field">
        <label>Location <span class="req">*</span></label>
        <input type="text" name="location_text" id="location_text" placeholder="e.g. South Congress and Wasson Road" required autocomplete="off">
        <div class="hint">Street intersection, address, or landmark. Be as specific as you can.</div>
      </div>
      <div class="field">
        <label>What did you see?</label>
        <textarea name="description" placeholder="Describe what you observed — who, what, how many, any vehicles or equipment..."></textarea>
      </div>
      <div class="field">
        <label>When</label>
        <input type="datetime-local" name="observed_at" id="observed_at">
        <div class="hint">Leave blank to use current time.</div>
      </div>
      <div class="field">
        <label>Photo <span style="color:#475569;font-weight:400;text-transform:none">(optional)</span></label>
        <label class="file-label" for="photo">
          <span>&#128247; Attach a photo</span>
          <span id="file-name"></span>
        </label>
        <input type="file" name="photo" id="photo" accept="image/*">
        <div class="hint">JPG, PNG, GIF, or WebP. Max 10 MB.</div>
      </div>
      <div class="hp"><input type="text" name="website" tabindex="-1" autocomplete="off"></div>
      <button type="submit">Submit Tip</button>
    </form>
  </div>
  <div id="confirm">
    <div class="check">&#10003;</div>
    <h2>Tip received</h2>
    <p>Thank you. Your tip has been logged and will be reviewed shortly.<br><br><a href="/public">Return to the live map</a> &nbsp;·&nbsp; <a href="/tip">Submit another</a></p>
  </div>
</div>
<script>
document.getElementById('observed_at').value = new Date().toISOString().slice(0,16);
document.getElementById('photo').addEventListener('change', function() {
  document.getElementById('file-name').textContent = this.files[0] ? this.files[0].name : '';
});
document.getElementById('tip-form').addEventListener('submit', async function(e) {
  e.preventDefault();
  const btn = this.querySelector('button[type=submit]');
  btn.disabled = true; btn.textContent = 'Submitting...';
  const fd = new FormData(this);
  try {
    const r = await fetch('/tip', {method:'POST', body: fd});
    if (r.ok) {
      document.getElementById('form-wrap').style.display = 'none';
      document.getElementById('confirm').style.display = 'block';
    } else {
      btn.disabled = false; btn.textContent = 'Submit Tip';
      alert('Submission failed. Please try again.');
    }
  } catch(err) {
    btn.disabled = false; btn.textContent = 'Submit Tip';
    alert('Network error. Please try again.');
  }
});
</script>
</body>
</html>"""

TIPS_ADMIN_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Tip Review</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; padding: 30px; }
h1 { font-size: 1.3rem; font-weight: 700; color: #3b82f6; letter-spacing: 2px; margin-bottom: 6px; }
.counts { color: #64748b; font-size: 0.8rem; margin-bottom: 28px; }
.tip-card { background: #0f1729; border: 1px solid #1e3a5f; border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }
.tip-card.approved { border-color: #166534; }
.tip-card.rejected { border-color: #374151; opacity: 0.55; }
.tip-meta { font-size: 0.72rem; color: #64748b; margin-bottom: 8px; }
.tip-location { font-size: 1rem; font-weight: 600; color: #e2e8f0; margin-bottom: 6px; }
.tip-desc { font-size: 0.85rem; color: #94a3b8; line-height: 1.5; margin-bottom: 10px; white-space: pre-wrap; }
.tip-coords { font-size: 0.72rem; color: #475569; margin-bottom: 10px; }
.tip-photo img { max-width: 280px; max-height: 200px; border-radius: 6px; border: 1px solid #1e3a5f; margin-bottom: 10px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.btn-approve { background: #166534; color: #86efac; border: 1px solid #166534; border-radius: 6px; padding: 6px 16px; font-size: 0.8rem; cursor: pointer; font-weight: 600; }
.btn-approve:hover { background: #15803d; }
.btn-reject { background: transparent; color: #94a3b8; border: 1px solid #374151; border-radius: 6px; padding: 6px 16px; font-size: 0.8rem; cursor: pointer; }
.btn-reject:hover { border-color: #ef4444; color: #ef4444; }
.badge { font-size: 0.7rem; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.badge-pending { background: #1e3a5f; color: #60a5fa; }
.badge-approved { background: #14532d; color: #86efac; }
.badge-rejected { background: #1f2937; color: #6b7280; }
.section-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: #475569; margin: 28px 0 12px; }
.note-input { background: #1e293b; border: 1px solid #1e3a5f; border-radius: 6px; color: #e2e8f0; padding: 6px 10px; font-size: 0.8rem; width: 220px; }
</style>
</head>
<body>
<h1>&#9652; TIP REVIEW</h1>
<div class="counts" id="counts">Loading...</div>
<div class="section-label">Pending Review</div>
<div id="pending-list"></div>
<div class="section-label">Reviewed</div>
<div id="reviewed-list"></div>
<script>
async function loadTips() {
  const r = await fetch('/api/tips');
  const tips = await r.json();
  const pending = tips.filter(t => t.status === 'pending');
  const reviewed = tips.filter(t => t.status !== 'pending');
  document.getElementById('counts').textContent =
    pending.length + ' pending · ' + reviewed.length + ' reviewed · ' + tips.length + ' total';
  document.getElementById('pending-list').innerHTML = pending.map(tipCard).join('') || '<p style="color:#475569;font-size:0.85rem">No pending tips.</p>';
  document.getElementById('reviewed-list').innerHTML = reviewed.map(tipCard).join('') || '<p style="color:#475569;font-size:0.85rem">None yet.</p>';
}
function tipCard(t) {
  const dt = new Date(t.ts * 1000).toLocaleString();
  const badge = `<span class="badge badge-${t.status}">${t.status.toUpperCase()}</span>`;
  const photo = t.photo_path ? `<div class="tip-photo"><img src="/static/tips/${t.photo_path}"></div>` : '';
  const coords = (t.lat && t.lon) ? `<div class="tip-coords">&#128205; ${t.lat.toFixed(5)}, ${t.lon.toFixed(5)}</div>` : '<div class="tip-coords">Location not geocoded</div>';
  const actions = t.status === 'pending' ? `
    <div class="actions">
      <input class="note-input" id="note-${t.id}" placeholder="Reviewer note (optional)">
      <button class="btn-approve" onclick="act(${t.id},'approve')">&#10003; Approve</button>
      <button class="btn-reject" onclick="act(${t.id},'reject')">&#215; Reject</button>
    </div>` : (t.reviewer_note ? `<div style="font-size:0.75rem;color:#475569">Note: ${t.reviewer_note}</div>` : '');
  return `<div class="tip-card ${t.status}" id="card-${t.id}">
    <div class="tip-meta">#${t.id} &nbsp;·&nbsp; ${dt} &nbsp;·&nbsp; ${badge}</div>
    <div class="tip-location">${t.location_text || '(no location)'}</div>
    ${coords}
    <div class="tip-desc">${t.description || '<em style="color:#475569">No description provided.</em>'}</div>
    ${photo}
    ${actions}
  </div>`;
}
async function act(id, action) {
  const note = document.getElementById('note-' + id)?.value || '';
  const r = await fetch('/api/tips/' + id + '/' + action, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({reviewer_note: note})
  });
  if (r.ok) loadTips();
  else alert('Action failed');
}
loadTips();
</script>
</body>
</html>"""


@app.route("/tip", methods=["GET"])
def tip_form():
    return TIP_FORM_HTML


@app.route("/tip", methods=["POST"])
def tip_submit():
    # Honeypot — bots fill the hidden website field
    if request.form.get("website"):
        return jsonify({"status": "ok"}), 200  # silent drop

    location_text = (request.form.get("location_text") or "").strip()
    if not location_text:
        return jsonify({"error": "location required"}), 400

    description = (request.form.get("description") or "").strip()
    observed_at  = request.form.get("observed_at") or ""

    # Parse observed time or use now
    try:
        ts = datetime.fromisoformat(observed_at).timestamp() if observed_at else time.time()
    except ValueError:
        ts = time.time()

    # Geocode
    lat, lon = None, None
    coords = _geocode_address(location_text)
    if coords:
        lat, lon = coords

    # Photo
    photo_path = None
    if "photo" in request.files:
        photo_path = _save_tip_photo(request.files["photo"])

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO tips (ts, location_text, lat, lon, description, photo_path, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', 'web')",
        (ts, location_text, lat, lon, description, photo_path)
    )
    tip_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"[tip] new submission: {location_text} | geocoded={lat},{lon} | photo={'yes' if photo_path else 'no'}", flush=True)
    threading.Thread(
        target=_notify_new_tip,
        args=(tip_id, location_text, description, photo_path, lat, lon, ts),
        daemon=True
    ).start()
    return jsonify({"status": "ok"}), 200


@app.route("/admin/tips")
def tips_admin():
    return TIPS_ADMIN_HTML


@app.route("/api/tips")
def api_tips():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tips ORDER BY ts DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tips/<int:tip_id>/approve", methods=["POST"])
def api_tip_approve(tip_id):
    data = request.get_json(silent=True) or {}
    note = (data.get("reviewer_note") or "").strip()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tips SET status='approved', reviewer_note=? WHERE id=?", (note, tip_id))
    conn.commit()
    conn.close()
    print(f"[tip] approved #{tip_id}", flush=True)
    return jsonify({"status": "approved", "id": tip_id})


@app.route("/api/tips/<int:tip_id>/reject", methods=["POST"])
def api_tip_reject(tip_id):
    data = request.get_json(silent=True) or {}
    note = (data.get("reviewer_note") or "").strip()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tips SET status='rejected', reviewer_note=? WHERE id=?", (note, tip_id))
    conn.commit()
    conn.close()
    print(f"[tip] rejected #{tip_id}", flush=True)
    return jsonify({"status": "rejected", "id": tip_id})



# ===========================================================================
# STRIPE + AUTH — Premium membership integration
# ===========================================================================
import stripe as _stripe
import secrets as _secrets

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")  # legacy
STRIPE_PLANS = {
    "premium_monthly": {"price_id": "price_1TGmOYIkODTTsH8IeoQPtVXf", "tier": "premium"},
    "premium_annual":  {"price_id": "price_1TGmPjIkODTTsH8IKHU4a5xK", "tier": "premium"},
    "basic_monthly":   {"price_id": "price_1TGmMzIkODTTsH8IipK3zPVr", "tier": "basic"},
    "basic_annual":    {"price_id": "price_1TGmNrIkODTTsH8IpSy0yHNi", "tier": "basic"},
}
STRIPE_PRICE_TO_TIER = {v["price_id"]: v["tier"] for v in STRIPE_PLANS.values()}

if STRIPE_SECRET_KEY:
    _stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _nc_validate_user(username, password):
    """Validate Nextcloud credentials via OCS API. Returns True/False."""
    import urllib.parse
    url = "https://kevcloud.ddns.net/ocs/v2.php/cloud/user"
    auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=6) as r:
            data = json.loads(r.read())
            return data.get("ocs", {}).get("meta", {}).get("status") == "ok"
    except Exception:
        return False


def _is_premium(username):
    """Check if username has an active premium record."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT status FROM premium_users WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    return row is not None and row[0] == "active"


def _is_admin(username):
    """Simple admin check — kevin is always admin."""
    return username.lower() in ("kevin", "mrrob")


def _issue_session(username):
    """Create a session token, store in DB, return token string."""
    token = _secrets.token_hex(32)
    now = time.time()
    expires = now + 86400 * 30  # 30 days
    premium = _is_premium(username)
    admin = _is_admin(username)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO sessions (token, username, created_ts, expires_ts, is_admin, is_premium) "
        "VALUES (?,?,?,?,?,?)",
        (token, username, now, expires, int(admin), int(premium))
    )
    conn.commit()
    conn.close()
    return token


def _get_session(request_obj):
    """Extract and validate session token from cookie or Authorization header.
    Returns dict {username, is_admin, is_premium} or None."""
    token = request_obj.cookies.get("bb_session") or \
            request_obj.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, expires_ts, is_admin, is_premium FROM sessions WHERE token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    username, expires_ts, is_admin, is_premium = row
    if time.time() > expires_ts:
        return None
    return {"username": username, "is_admin": bool(is_admin), "is_premium": bool(is_premium)}


def _require_premium(f):
    """Decorator: require valid session with is_premium=True."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        sess = _get_session(request)
        if not sess:
            return jsonify({"error": "login required"}), 401
        if not sess["is_premium"] and not sess["is_admin"]:
            return jsonify({"error": "premium required"}), 403
        return f(*args, **kwargs)
    return decorated


def _require_admin(f):
    """Decorator: require valid session with is_admin=True."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        sess = _get_session(request)
        if not sess or not sess["is_admin"]:
            return jsonify({"error": "admin required"}), 403
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Provisioning helpers
# ---------------------------------------------------------------------------

TALK_ROOMS = {
    "incidents": "89q5fnh5",
    "apd":       "m38srso2",
    "fire-ems":  "ee6si4vj",
    "general":   "iyidr3xy",
}


def _nc_create_user(username, password, email, display_name, tier="premium"):
    """Create Nextcloud user and add to the appropriate membership group."""
    nc_group = "Premium Members" if tier == "premium" else "Basic Members"
    nc_base = "https://kevcloud.ddns.net/ocs/v2.php/cloud"
    auth_b64 = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    def _post(url, body):
        data = urllib.parse.urlencode(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[stripe] NC POST {url} error: {e}", flush=True)
            return {}

    # Create user
    _post(f"{nc_base}/users", {
        "userid": username, "password": password,
        "email": email, "displayName": display_name,
    })
    # Add to group
    _post(f"{nc_base}/users/{username}/groups", {"groupid": nc_group})
    print(f"[stripe] Nextcloud user '{username}' created and added to {nc_group}", flush=True)
    _subscribe_news_feed(username)


def _subscribe_news_feed(username):
    """Subscribe a Nextcloud user to the Battle Buddy RSS feed in Nextcloud News."""
    try:
        result = subprocess.run(
            ["sudo", "-u", "www-data", "php", "/var/www/nextcloud/occ",
             "news:feed:add", username, "https://battlebuddy.news/public/feed.rss",
             "--title", "Battle Buddy Intel Feed"],
            capture_output=True, text=True, timeout=30
        )
        print(f"[provision] News feed subscribed for '{username}': "
              f"{result.stdout.strip() or result.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"[provision] News feed subscribe error for '{username}': {e}", flush=True)



def _plant_user_guide(username):
    """Copy Battle Buddy User Guide.md into a new user's NC files and scan it into the DB."""
    NC_DATA   = "/var/www/nextcloud-data"
    GUIDE_SRC = "/var/www/nextcloud/core/skeleton/Battle Buddy User Guide.md"
    dest_dir  = os.path.join(NC_DATA, username, "files")
    dest_file = os.path.join(dest_dir, "Battle Buddy User Guide.md")
    try:
        if not os.path.isdir(dest_dir):
            print(f"[provision] guide: user dir not ready yet for '{username}', skipping filesystem copy", flush=True)
        else:
            shutil.copy2(GUIDE_SRC, dest_file)
            os.chown(dest_file, pwd.getpwnam("www-data").pw_uid, pwd.getpwnam("www-data").pw_gid)
            result = subprocess.run(
                ["sudo", "-u", "www-data", "php", "/var/www/nextcloud/occ",
                 "files:scan", "--path=/" + username + "/files/Battle Buddy User Guide.md"],
                capture_output=True, text=True, timeout=30
            )
            print(f"[provision] guide planted for '{username}': {result.stdout.strip() or 'ok'}", flush=True)
    except Exception as e:
        print(f"[provision] guide plant error for '{username}': {e}", flush=True)

def _add_to_talk_rooms(username):
    """Add Nextcloud user to all 4 Battle Buddy Talk rooms."""
    auth_b64 = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    for room_name, token in TALK_ROOMS.items():
        url = f"https://kevcloud.ddns.net/ocs/v2.php/apps/spreed/api/v4/room/{token}/participants"
        data = urllib.parse.urlencode({"newParticipant": username}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=8) as r:
                resp = json.loads(r.read())
                print(f"[stripe] Added {username} to Talk room '{room_name}': {resp.get('ocs',{}).get('meta',{}).get('status')}", flush=True)
        except Exception as e:
            print(f"[stripe] Talk room '{room_name}' add error: {e}", flush=True)


def _enroll_subscriptions(username):
    """Add to subscriptions table for DM incident alerts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (username, beat) VALUES (?, 'all')",
        (username,)
    )
    conn.commit()
    conn.close()
    print(f"[stripe] {username} enrolled in subscriptions (beat=all)", flush=True)


def _send_welcome_email(email, username, setup_token, tier="premium"):
    """Send Mailgun welcome email with onboarding instructions, tier-aware."""
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        return

    atak_step = (
        "=== STEP 7 — ATAK FIELD PACKAGE ===\n"
        "Battle Buddy pushes live incident markers directly to WinTAK, ATAK, and iTAK.\n"
        "Reply to this email to request your ATAK data package and connection instructions.\n\n"
    ) if tier == "premium" else ""

    plain = (
        f"Welcome to Battle Buddy Premium, {username}!\n\n"
        f"=== SET YOUR PASSWORD ===\n"
        f"Use the link below to set your password and access your dashboard.\n"
        f"This link expires in 72 hours.\n"
        f"  https://battlebuddy.news/premium/setup?token={setup_token}\n\n"
        f"Your username is: {username}\n\n"
        f"=== STEP 1 — DOWNLOAD THE APPS ===\n"
        f"Install the Nextcloud app on your phone or tablet to access everything on the go.\n"
        f"  Android: https://play.google.com/store/apps/details?id=com.nextcloud.client\n"
        f"  iPhone/iPad: https://apps.apple.com/us/app/nextcloud/id1125420102\n"
        f"  Desktop (Windows/Mac/Linux): https://nextcloud.com/install/#desktop-clients\n\n"
        f"For Talk (incident alert notifications on your phone):\n"
        f"  Android: https://play.google.com/store/apps/details?id=com.nextcloud.talk2\n"
        f"  iPhone/iPad: https://apps.apple.com/us/app/nextcloud-talk/id1296825574\n\n"
        f"=== STEP 2 — INCIDENT ALERTS (TALK) ===\n"
        f"You have been added to all Battle Buddy alert rooms in Nextcloud Talk.\n"
        f"  Web: https://kevcloud.ddns.net/apps/talk\n"
        f"  Rooms you are in:\n"
        f"    - Incidents  (all detected incidents)\n"
        f"    - APD        (Austin Police press releases and scanner intel)\n"
        f"    - Fire & EMS (AFD, Travis County EMS, STAR Flight)\n"
        f"    - General    (Battle Buddy updates and announcements)\n\n"
        f"Enable push notifications in the Talk app so alerts reach you immediately.\n\n"
        f"=== STEP 3 — INTEL NEWS FEED ===\n"
        f"You are auto-subscribed to the Battle Buddy Intel Feed in Nextcloud News.\n"
        f"  Web: https://kevcloud.ddns.net/apps/news\n"
        f"The feed updates continuously with every confirmed incident, APD press release,\n"
        f"and homicide update — one scrollable stream of verified Austin intelligence.\n\n"
        f"You can also subscribe any RSS reader to the feed directly:\n"
        f"  Feed URL: https://battlebuddy.news/public/feed.rss\n\n"
        f"=== STEP 4 — INTEL QUERY ===\n"
        f"Ask questions about Austin radio traffic in plain English. Battle Buddy searches\n"
        f"the full scanner transcript database and returns an AI-synthesized summary.\n"
        f"  URL: https://battlebuddy.news/premium/\n"
        f"  Quota: 5 queries per month (resets the 1st of each month)\n"
        f"  Example: 'How many shootings near North Lamar this week?'\n\n"
        f"=== STEP 5 — COMMUTE MONITOR ===\n"
        f"Save your commute route and get a Talk alert whenever an incident is detected\n"
        f"near your path — with live travel time vs your normal commute.\n"
        f"  Dashboard:   https://battlebuddy.news/premium/\n"
        f"  Commute Map: https://battlebuddy.news/premium/commute\n\n"
        f"=== STEP 6 — LIVE INTELLIGENCE ===\n"
        f"  Live incident map:  https://battlebuddy.news/public\n"
        f"  Live incident feed: https://battlebuddy.news/public/feed\n"
        f"  2026 Homicide map:  https://battlebuddy.news/public/homicides\n\n"
        + atak_step
        + "=== SUPPORT ===\n"
        + "Reply to this email with any questions. We will get back to you promptly.\n\n"
        + "— Battle Buddy Operations\n"
        + "  https://battlebuddy.news"
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#e2e8f0;margin:0;padding:0}}
  .wrap{{max-width:600px;margin:0 auto;padding:32px 20px}}
  .header{{background:#0f1729;border:1px solid #1e3a5f;border-radius:10px;padding:28px 24px;margin-bottom:24px;text-align:center}}
  .header h1{{color:#f8fafc;font-size:1.5rem;margin:0 0 6px}}
  .header p{{color:#64748b;font-size:0.88rem;margin:0}}
  .creds{{background:#0f1729;border:1px solid #f59e0b;border-radius:8px;padding:20px 24px;margin-bottom:24px}}
  .creds h2{{color:#f59e0b;font-size:0.72rem;letter-spacing:2px;text-transform:uppercase;margin:0 0 14px}}
  .cred-row{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #1e293b;font-size:0.88rem}}
  .cred-row:last-child{{border-bottom:none}}
  .cred-label{{color:#64748b}}
  .cred-val{{color:#f8fafc;font-weight:600;font-family:monospace}}
  .section{{background:#0f1729;border:1px solid #1e3a5f;border-radius:8px;padding:20px 24px;margin-bottom:16px}}
  .section-num{{display:inline-block;background:#1e3a5f;color:#60a5fa;font-size:0.7rem;font-weight:700;padding:2px 8px;border-radius:3px;letter-spacing:1px;margin-bottom:10px}}
  .section h2{{color:#f8fafc;font-size:1rem;margin:0 0 10px}}
  .section p{{color:#94a3b8;font-size:0.84rem;line-height:1.6;margin:0 0 10px}}
  .section p:last-child{{margin-bottom:0}}
  .app-links{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}}
  .app-link{{display:inline-block;background:#1e3a5f;color:#60a5fa;text-decoration:none;font-size:0.78rem;padding:6px 14px;border-radius:5px;border:1px solid #2d4a7a}}
  .rooms{{list-style:none;margin:10px 0 0;padding:0}}
  .rooms li{{font-size:0.83rem;color:#94a3b8;padding:5px 0;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:8px}}
  .rooms li:last-child{{border-bottom:none}}
  .dot{{width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0}}
  .feed-url{{background:#060c18;border:1px solid #1e3a5f;border-radius:5px;padding:10px 14px;font-family:monospace;font-size:0.8rem;color:#60a5fa;word-break:break-all;margin-top:10px}}
  .cta{{display:inline-block;background:#f59e0b;color:#0a0a0f;font-weight:700;text-decoration:none;padding:11px 24px;border-radius:6px;font-size:0.9rem;margin-top:12px}}
  .links{{display:flex;flex-direction:column;gap:7px;margin-top:10px}}
  .links a{{color:#60a5fa;font-size:0.84rem;text-decoration:none}}
  .footer{{text-align:center;color:#334155;font-size:0.75rem;margin-top:24px;line-height:1.6}}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <h1>Welcome to Battle Buddy Premium</h1>
    <p>Austin's real-time public safety intelligence platform</p>
  </div>

  <div class="creds">
    <h2>Activate Your Account</h2>
    <p style="color:#94a3b8;font-size:0.9rem;margin:0 0 16px">Click below to set your password and access your dashboard. This link expires in 72 hours.</p>
    <a class="cta" href="https://battlebuddy.news/premium/setup?token={setup_token}">Set Your Password →</a>
    <p style="color:#64748b;font-size:0.78rem;margin:14px 0 0">Your username is: <strong style="color:#cbd5e1">{username}</strong></p>
  </div>

  <div class="section">
    <span class="section-num">STEP 1</span>
    <h2>Download the Apps</h2>
    <p>Install Nextcloud on your phone or tablet for access to Talk alerts, the News feed, files, and calendar — all in one place.</p>
    <p><strong style="color:#cbd5e1">Nextcloud (main app)</strong></p>
    <div class="app-links">
      <a class="app-link" href="https://play.google.com/store/apps/details?id=com.nextcloud.client">Android</a>
      <a class="app-link" href="https://apps.apple.com/us/app/nextcloud/id1125420102">iPhone / iPad</a>
      <a class="app-link" href="https://nextcloud.com/install/#desktop-clients">Desktop (Win/Mac/Linux)</a>
    </div>
    <p style="margin-top:14px"><strong style="color:#cbd5e1">Nextcloud Talk (for push alert notifications)</strong></p>
    <div class="app-links">
      <a class="app-link" href="https://play.google.com/store/apps/details?id=com.nextcloud.talk2">Android</a>
      <a class="app-link" href="https://apps.apple.com/us/app/nextcloud-talk/id1296825574">iPhone / iPad</a>
    </div>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">After installing, tap <em>Add account</em> and enter the server URL above with your username and password.</p>
  </div>

  <div class="section">
    <span class="section-num">STEP 2</span>
    <h2>Incident Alerts — Talk</h2>
    <p>You are enrolled in all four Battle Buddy alert rooms. Incidents are posted the moment they are detected — before any news broadcast exists.</p>
    <ul class="rooms">
      <li><span class="dot"></span><strong style="color:#cbd5e1">Incidents</strong> &nbsp;— all detected incidents across agencies</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">APD</strong> &nbsp;— Austin Police press releases and scanner intel</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">Fire &amp; EMS</strong> &nbsp;— AFD, Travis County EMS, STAR Flight</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">General</strong> &nbsp;— Battle Buddy updates and announcements</li>
    </ul>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">Enable push notifications in the Talk app so critical incident alerts wake your phone immediately.</p>
    <div class="app-links" style="margin-top:10px">
      <a class="app-link" href="https://kevcloud.ddns.net/apps/talk">Open Talk on Web</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 3</span>
    <h2>Intel News Feed</h2>
    <p>You are auto-subscribed to the <strong style="color:#cbd5e1">Battle Buddy Intel Feed</strong> in Nextcloud News. Open it from the News app on the web or inside the Nextcloud mobile app — it updates continuously with every confirmed incident, APD press release, and homicide update.</p>
    <div class="app-links">
      <a class="app-link" href="https://kevcloud.ddns.net/apps/news">Open News on Web</a>
    </div>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">You can also subscribe any RSS reader (Feedly, Reeder, etc.) to the feed directly:</p>
    <div class="feed-url">https://battlebuddy.news/public/feed.rss</div>
  </div>

  <div class="section">
    <span class="section-num">STEP 4</span>
    <h2>Intel Query — AI Search</h2>
    <p>Ask any question about Austin radio traffic in plain English. Battle Buddy searches the full scanner transcript and press release database and returns an AI-written intelligence summary.</p>
    <p style="font-size:0.8rem;color:#64748b"><em>Examples: "How many shootings near North Lamar this week?" &nbsp;·&nbsp; "Any AFD structure fires in East Austin this month?"</em></p>
    <p style="font-size:0.8rem;color:#64748b">Quota: <strong style="color:#cbd5e1">5 queries per month</strong>, resets on the 1st.</p>
    <div class="app-links">
      <a class="app-link" href="https://battlebuddy.news/premium/">Open Intel Query</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 5</span>
    <h2>🚗 Commute Monitor</h2>
    <p>Save your daily commute route and Battle Buddy will alert you via Talk whenever an active incident is detected near your path — with live travel time vs your normal commute.</p>
    <p style="font-size:0.8rem;color:#64748b">Open your premium dashboard, find the Commute Monitor card, and enter your origin and destination (include city and state — e.g. <em>Slaughter Ln &amp; I-35, Austin TX</em>).</p>
    <div class="app-links">
      <a class="app-link" href="https://battlebuddy.news/premium/commute">Open Commute Map</a>
      <a class="app-link" href="https://battlebuddy.news/premium/">Premium Dashboard</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 6</span>
    <h2>Live Intelligence — Web</h2>
    <p>The public intelligence portal is available to you at any time from any browser — no login required.</p>
    <div class="links">
      <a href="https://battlebuddy.news/public">battlebuddy.news/public &nbsp;— Live incident map</a>
      <a href="https://battlebuddy.news/public/feed">battlebuddy.news/public/feed &nbsp;— Live incident feed</a>
      <a href="https://battlebuddy.news/public/homicides">battlebuddy.news/public/homicides &nbsp;— 2026 Austin Homicide Map</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 7</span>
    <h2>ATAK Field Package</h2>
    <p>Battle Buddy pushes live incident markers directly to WinTAK, ATAK (Android), and iTAK as Cursor-on-Target (CoT) markers — appearing on your tactical map the moment an incident is confirmed.</p>
    <p>Reply to this email to request your ATAK data package (.zip with certs and connection profile) and setup instructions for your device.</p>
  </div>

  <div class="footer">
    <p>Questions? Reply to this email — we respond promptly.</p>
    <p style="margin-top:6px"><a href="https://battlebuddy.news" style="color:#3b82f6">battlebuddy.news</a> &nbsp;·&nbsp; Battle Buddy Operations</p>
  </div>

</div>
</body>
</html>"""

    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    auth_b64 = base64.b64encode(f"api:{MAILGUN_API_KEY}".encode()).decode()
    data = urllib.parse.urlencode({
        "from": MAILGUN_FROM,
        "to": email,
        "subject": "Welcome to Battle Buddy Premium — Getting Started",
        "text": plain,
        "html": html,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {auth_b64}",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[stripe] Welcome email sent to {email}: {r.status}", flush=True)
    except Exception as e:
        print(f"[stripe] Welcome email error: {e}", flush=True)


def _provision_premium_user(session_obj):
    """Full provisioning flow after successful Stripe checkout."""
    customer_email = (session_obj.get("customer_details") or {}).get("email") or                      session_obj.get("customer_email") or ""
    customer_id    = session_obj.get("customer", "")
    sub_id         = session_obj.get("subscription", "")
    metadata       = session_obj.get("metadata") or {}

    username     = metadata.get("username", "").strip().lower()
    nc_password  = metadata.get("nc_password", "").strip()
    display_name = metadata.get("display_name", "") or username
    tier         = metadata.get("tier", "premium")

    if not username or not customer_email:
        print(f"[stripe] provision skipped — missing username or email in session", flush=True)
        return

    # 1. Nextcloud user
    _nc_create_user(username, nc_password, customer_email, display_name, tier)

    # 2. Talk rooms
    _add_to_talk_rooms(username)

    # 3. Subscriptions
    _enroll_subscriptions(username)

    # 3b. Plant user guide in NC files
    _plant_user_guide(username)

    # 4. premium_users table
    setup_token   = _secrets.token_urlsafe(32)
    setup_expires = int(time.time()) + 72 * 3600  # 72-hour window
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO premium_users "
        "(username, email, stripe_customer_id, stripe_subscription_id, status, created_ts, setup_token, setup_token_expires) "
        "VALUES (?,?,?,?,'active',?,?,?)",
        (username, customer_email, customer_id, sub_id, time.time(), setup_token, setup_expires)
    )
    conn.commit()
    conn.close()
    print(f"[stripe] premium_users record created for '{username}'", flush=True)

    # 5. Welcome email
    _send_welcome_email(customer_email, username, setup_token, tier)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    if not _nc_validate_user(username, password):
        return jsonify({"error": "invalid credentials"}), 401
    token = _issue_session(username)
    sess = _get_session_by_token(token)
    from flask import make_response
    resp = make_response(jsonify({
        "token": token,
        "username": username,
        "is_premium": sess["is_premium"],
        "is_admin": sess["is_admin"],
    }))
    resp.set_cookie("bb_session", token, max_age=86400*30, httponly=True, samesite="Lax")
    return resp


def _get_session_by_token(token):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, expires_ts, is_admin, is_premium FROM sessions WHERE token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    username, expires_ts, is_admin, is_premium = row
    return {"username": username, "is_admin": bool(is_admin), "is_premium": bool(is_premium)}


@app.route("/api/logout", methods=["POST"])
def api_logout():
    sess = _get_session(request)
    if sess:
        token = request.cookies.get("bb_session") or \
                request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    from flask import make_response
    resp = make_response(jsonify({"status": "ok"}))
    resp.set_cookie("bb_session", "", expires=0)
    return resp


@app.route("/api/me")
def api_me():
    sess = _get_session(request)
    if not sess:
        return jsonify({"logged_in": False}), 200
    return jsonify({"logged_in": True, **sess})


# ---------------------------------------------------------------------------
# Stripe checkout + webhook
# ---------------------------------------------------------------------------

@app.route("/api/stripe/create_checkout", methods=["POST"])
def api_stripe_create_checkout():
    """Create a Stripe Checkout Session. Client sends username, display_name, plan."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "payments not configured"}), 503
    data = request.get_json(silent=True) or {}
    username     = (data.get("username") or "").strip().lower()
    display_name = (data.get("display_name") or username).strip()
    plan         = (data.get("plan") or "premium_monthly").strip()
    if not username:
        return jsonify({"error": "username required"}), 400
    if plan not in STRIPE_PLANS:
        return jsonify({"error": "invalid plan"}), 400

    plan_info   = STRIPE_PLANS[plan]
    nc_password = _secrets.token_urlsafe(12)

    try:
        session = _stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan_info["price_id"], "quantity": 1}],
            subscription_data={"trial_period_days": 7},
            success_url="https://battlebuddy.news/premium/welcome?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://battlebuddy.news/premium/",
            metadata={
                "username": username,
                "display_name": display_name,
                "nc_password": nc_password,
                "tier": plan_info["tier"],
            },
        )
        return jsonify({"checkout_url": session.url})
    except Exception as e:
        print(f"[stripe] create_checkout error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe sends signed events here. Verify signature, then provision."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = _stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except _stripe.error.SignatureVerificationError as e:
        print(f"[stripe] webhook signature invalid: {e}", flush=True)
        return jsonify({"error": "invalid signature"}), 400
    except Exception as e:
        print(f"[stripe] webhook parse error: {e}", flush=True)
        return jsonify({"error": "bad payload"}), 400

    event_type = event["type"]
    print(f"[stripe] webhook received: {event_type}", flush=True)

    if event_type == "checkout.session.completed":
        # Parse session from raw payload — avoids SDK v15 StripeObject attribute issues
        import json as _json
        session_data = _json.loads(payload)["data"]["object"]
        threading.Thread(
            target=_provision_premium_user,
            args=(session_data,),
            daemon=True
        ).start()

    elif event_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer", "")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE premium_users SET status='cancelled' WHERE stripe_customer_id=?",
            (customer_id,)
        )
        conn.commit()
        conn.close()
        print(f"[stripe] subscription cancelled for customer {customer_id}", flush=True)

    return jsonify({"status": "ok"})



# ---------------------------------------------------------------------------
# Commute travel time — premium feature
# ---------------------------------------------------------------------------

_COMMUTE_ALERT_ITYPES = {
    "SHOOTING", "OFFICER DOWN", "PURSUIT", "STRUCTURE FIRE",
    "HAZMAT", "WEAPONS", "CRASH/COLLISION", "STABBING", "MASS CASUALTY",
}
_COMMUTE_CORRIDOR_MILES = 3.0  # incident must be within this distance of route line


def _point_to_segment_distance_miles(px, py, ax, ay, bx, by) -> float:
    """Perpendicular distance (miles) from point P to line segment A→B."""
    import math
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        dx2 = px - ax; dy2 = py - ay
        return math.sqrt(dx2*dx2 + dy2*dy2) * 69.0
    t = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / (dx*dx + dy*dy)))
    cx2 = ax + t*dx; cy2 = ay + t*dy
    ddx = px - cx2; ddy = py - cy2
    deg = math.sqrt(ddx*ddx + ddy*ddy)
    return deg * 69.0  # rough degrees→miles


def _routes_travel_time(origin_lat, origin_lon, dest_lat, dest_lon, traffic=True) -> int | None:
    """Call Google Routes API; return travel time in minutes or None on error."""
    import json as _json
    preference = "TRAFFIC_AWARE" if traffic else "TRAFFIC_UNAWARE"
    body = _json.dumps({
        "origin":      {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lon}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat,   "longitude": dest_lon}}},
        "travelMode":  "DRIVE",
        "routingPreference": preference,
    }).encode()
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration",
        },
        method="POST",
    )
    try:
        resp  = urllib.request.urlopen(req, timeout=10).read().decode()
        data  = _json.loads(resp)
        dur   = data.get("routes", [{}])[0].get("duration", "0s")
        secs  = int(dur.rstrip("s")) if isinstance(dur, str) else 0
        return max(1, round(secs / 60))
    except Exception as e:
        print(f"[commute] Routes API error: {e}", flush=True)
        return None


def _commute_route_info(origin_addr: str, dest_addr: str, traffic: bool = False):
    """
    Call Routes API with raw address strings.
    Returns dict with keys: origin_lat, origin_lon, dest_lat, dest_lon, mins
    or None on failure. Google geocodes the addresses natively — no Nominatim needed.
    """
    import json as _json
    preference = "TRAFFIC_AWARE" if traffic else "TRAFFIC_UNAWARE"
    body = _json.dumps({
        "origin":      {"address": origin_addr},
        "destination": {"address": dest_addr},
        "travelMode":  "DRIVE",
        "routingPreference": preference,
    }).encode()
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.legs.startLocation,routes.legs.endLocation",
        },
        method="POST",
    )
    try:
        resp  = urllib.request.urlopen(req, timeout=10).read().decode()
        data  = _json.loads(resp)
        route = data.get("routes", [{}])[0]
        leg   = route.get("legs", [{}])[0]
        dur   = route.get("duration", "0s")
        secs  = int(dur.rstrip("s")) if isinstance(dur, str) else 0
        sloc  = leg.get("startLocation", {}).get("latLng", {})
        eloc  = leg.get("endLocation",   {}).get("latLng", {})
        if not sloc or not eloc:
            return None
        return {
            "origin_lat": sloc["latitude"],
            "origin_lon": sloc["longitude"],
            "dest_lat":   eloc["latitude"],
            "dest_lon":   eloc["longitude"],
            "mins":       max(1, round(secs / 60)),
        }
    except Exception as e:
        print(f"[commute] Routes API error: {e}", flush=True)
        return None


def _check_commute_alerts(inc_id: int, itype: str, inc_lat: float, inc_lon: float, description: str):
    """Check all premium users with saved commutes; alert those whose route passes near the incident."""
    if itype not in _COMMUTE_ALERT_ITYPES:
        return
    if inc_lat is None or inc_lon is None:
        return
    if not GOOGLE_ROUTES_KEY:
        return

    conn = sqlite3.connect(DB_PATH)
    users = conn.execute(
        "SELECT username, commute_origin, commute_origin_lat, commute_origin_lon, "
        "commute_dest, commute_dest_lat, commute_dest_lon, commute_baseline_mins "
        "FROM premium_users WHERE status='active' AND commute_origin_lat IS NOT NULL"
    ).fetchall()
    conn.close()

    for (username, origin, olat, olon, dest, dlat, dlon, baseline) in users:
        dist = _point_to_segment_distance_miles(inc_lat, inc_lon, olat, olon, dlat, dlon)
        if dist > _COMMUTE_CORRIDOR_MILES:
            continue

        # Fetch live travel time
        live_mins = _routes_travel_time(olat, olon, dlat, dlon, traffic=True)
        if live_mins is None:
            continue

        delta = live_mins - baseline if baseline else None
        delta_str = ""
        if delta is not None:
            if delta > 0:
                delta_str = f" (+{delta} min over normal)"
            elif delta < 0:
                delta_str = f" ({abs(delta)} min faster than normal)"

        # Trim description for message
        short_desc = description[:120].rsplit(" ", 1)[0] if len(description) > 120 else description

        msg = (
            f"\U0001f697 [COMMUTE ALERT] {itype} detected {dist:.1f} mi from your route\n"
            f"\U0001f552 Current travel time: {live_mins} min{delta_str}\n"
            f"\U0001f4cd {short_desc}"
        )

        # Send Talk DM
        try:
            token = _get_or_create_dm_room(username)
            if token: _bot_reply(token, msg)
            print(f"[commute] alert sent to {username}: {itype} {dist:.1f}mi, {live_mins}min", flush=True)
        except Exception as e:
            print(f"[commute] DM failed for {username}: {e}", flush=True)


@app.route("/api/commute/save", methods=["POST"])
@_require_premium
def api_commute_save():
    sess = _get_session(request)
    username = sess["username"]
    data = request.get_json(force=True) or {}
    origin = (data.get("origin") or "").strip()
    dest   = (data.get("destination") or "").strip()
    if not origin or not dest:
        return jsonify({"error": "origin and destination required"}), 400

    # Single Routes API call: geocodes addresses AND returns baseline travel time
    info = _commute_route_info(origin, dest, traffic=False)
    if not info:
        return jsonify({"error": "Could not resolve addresses or fetch travel time. Check that both addresses are valid US locations."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE premium_users SET commute_origin=?, commute_origin_lat=?, commute_origin_lon=?, "
        "commute_dest=?, commute_dest_lat=?, commute_dest_lon=?, commute_baseline_mins=? "
        "WHERE username=?",
        (origin, info["origin_lat"], info["origin_lon"],
         dest,   info["dest_lat"],   info["dest_lon"],
         info["mins"], username)
    )
    conn.commit()
    conn.close()
    print(f"[commute] {username} saved route: {origin} -> {dest}, baseline {info['mins']}min", flush=True)
    return jsonify({"ok": True, "baseline_mins": info["mins"], "origin": origin, "destination": dest})


@app.route("/api/commute/time", methods=["GET"])
@_require_premium
def api_commute_time():
    sess = _get_session(request)
    username = sess["username"]
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT commute_origin, commute_origin_lat, commute_origin_lon, "
        "commute_dest, commute_dest_lat, commute_dest_lon, commute_baseline_mins "
        "FROM premium_users WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    if not row or row[1] is None:
        return jsonify({"configured": False})
    origin, olat, olon, dest, dlat, dlon, baseline = row
    live = _routes_travel_time(olat, olon, dlat, dlon, traffic=True)
    if live is None:
        return jsonify({"error": "Routes API unavailable"}), 503
    delta = live - baseline if baseline else None
    return jsonify({
        "configured": True,
        "origin": origin,
        "destination": dest,
        "live_mins": live,
        "baseline_mins": baseline,
        "delta_mins": delta,
    })




@app.route("/api/commute/polyline", methods=["GET"])
def api_commute_polyline():
    """Return encoded polyline for user's commute route. Auth via session or share token."""
    import json as _json
    token = request.args.get("token")
    if token:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT commute_origin, commute_origin_lat, commute_origin_lon, "
            "commute_dest, commute_dest_lat, commute_dest_lon "
            "FROM premium_users WHERE commute_share_token=?", (token,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "invalid token"}), 403
        origin, olat, olon, dest, dlat, dlon = row
    else:
        sess = _get_session(request)
        if not sess:
            return jsonify({"error": "login required"}), 401
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT commute_origin, commute_origin_lat, commute_origin_lon, "
            "commute_dest, commute_dest_lat, commute_dest_lon "
            "FROM premium_users WHERE username=?", (sess["username"],)
        ).fetchone()
        conn.close()
        if not row or row[1] is None:
            return jsonify({"configured": False})
        origin, olat, olon, dest, dlat, dlon = row

    body = _json.dumps({
        "origin":      {"location": {"latLng": {"latitude": olat, "longitude": olon}}},
        "destination": {"location": {"latLng": {"latitude": dlat, "longitude": dlon}}},
        "travelMode":  "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }).encode()
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
            "X-Goog-FieldMask": "routes.polyline.encodedPolyline",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10).read().decode()
        data = _json.loads(resp)
        poly = data.get("routes", [{}])[0].get("polyline", {}).get("encodedPolyline", "")
        return jsonify({"configured": True, "polyline": poly, "origin": origin, "destination": dest})
    except Exception as e:
        print(f"[commute] polyline error: {e}", flush=True)
        return jsonify({"error": "Routes API unavailable"}), 503

@app.route("/api/commute/incidents", methods=["GET"])
def api_commute_incidents():
    """Return active incidents near the user's commute route. Auth via session or share token."""
    token = request.args.get("token")
    if token:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT username, commute_origin_lat, commute_origin_lon, "
            "commute_dest_lat, commute_dest_lon FROM premium_users WHERE commute_share_token=?",
            (token,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "invalid token"}), 403
        _, olat, olon, dlat, dlon = row
    else:
        sess = _get_session(request)
        if not sess:
            return jsonify({"error": "login required"}), 401
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT commute_origin_lat, commute_origin_lon, commute_dest_lat, commute_dest_lon "
            "FROM premium_users WHERE username=?", (sess["username"],)
        ).fetchone()
        conn.close()
        if not row or row[0] is None:
            return jsonify({"configured": False, "incidents": []})
        olat, olon, dlat, dlon = row

    cutoff = time.time() - 3600 * 4
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, itype, location, lat, lon, description, ts_start FROM incidents "
        "WHERE status='active' AND lat IS NOT NULL AND ts_start > ? AND is_test=0",
        (cutoff,)
    ).fetchall()
    conn.close()

    nearby = []
    for inc_id, itype, location, ilat, ilon, desc, ts in rows:
        dist = _point_to_segment_distance_miles(ilat, ilon, olat, olon, dlat, dlon)
        if dist <= 5.0:
            nearby.append({
                "id": inc_id,
                "itype": itype,
                "location": location,
                "lat": ilat,
                "lon": ilon,
                "description": (desc or "")[:200],
                "ts": ts,
                "dist_miles": round(dist, 1),
            })
    nearby.sort(key=lambda x: x["dist_miles"])
    return jsonify({"configured": True, "incidents": nearby})


@app.route("/api/commute/share_token", methods=["POST"])
@_require_premium
def api_commute_share_token():
    """Generate or return existing share token for the user's commute map."""
    import secrets as _secrets
    sess = _get_session(request)
    username = sess["username"]
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT commute_share_token FROM premium_users WHERE username=?", (username,)
    ).fetchone()
    token = row[0] if row and row[0] else None
    if not token:
        token = _secrets.token_urlsafe(24)
        conn.execute(
            "UPDATE premium_users SET commute_share_token=? WHERE username=?",
            (token, username)
        )
        conn.commit()
    conn.close()
    share_url = f"https://battlebuddy.news/premium/commute?token={token}"
    return jsonify({"token": token, "share_url": share_url})


@app.route("/premium/commute")
def premium_commute():
    """Live commute map — premium-gated or token-gated."""
    token = request.args.get("token")
    if not token:
        sess = _get_session(request)
        if not sess or not sess.get("is_premium"):
            return redirect("/premium/")
    maps_key = GOOGLE_MAPS_JS_KEY
    html = f"""<!DOCTYPE html>
<html><head>
<title>Commute Map — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:sans-serif;background:#0a0f1e;color:#eee;height:100vh;display:flex;flex-direction:column}}
  #header{{background:#111827;border-bottom:1px solid #1e3a5f;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}}
  #header h1{{font-size:1rem;color:#f8fafc;font-weight:700;letter-spacing:1px}}
  #travel-info{{display:flex;align-items:center;gap:20px}}
  #travel-mins{{font-size:2rem;font-weight:800;color:#f90;line-height:1}}
  #travel-delta{{font-size:0.8rem;color:#94a3b8}}
  #travel-route{{font-size:0.7rem;color:#475569;margin-top:2px}}
  #incident-bar{{background:#111827;border-bottom:1px solid #1e3a5f;padding:8px 20px;font-size:0.78rem;color:#94a3b8;flex-shrink:0;min-height:34px}}
  #incident-bar span.inc-badge{{display:inline-block;background:#7f1d1d;color:#fca5a5;border-radius:4px;padding:2px 8px;margin-right:6px;font-size:0.72rem;cursor:pointer}}
  #map{{flex:1}}
  #share-btn{{background:#1e3a5f;color:#93c5fd;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.75rem;white-space:nowrap}}
  #share-btn:hover{{background:#2d4f7c}}
  #updated{{font-size:0.65rem;color:#334155;margin-top:2px}}
  .no-route{{display:flex;align-items:center;justify-content:center;height:100%;color:#475569;font-size:1rem}}
</style>
</head><body>
<div id="header">
  <div>
    <h1>🚗 BATTLE BUDDY — COMMUTE MONITOR</h1>
    <div id="updated">Loading...</div>
  </div>
  <div id="travel-info">
    <div>
      <div id="travel-mins">--</div>
      <div id="travel-delta"></div>
      <div id="travel-route"></div>
    </div>
    <button id="share-btn" onclick="getShareLink()">Share Link</button>
  </div>
</div>
<div id="incident-bar">Checking for incidents near your route...</div>
<div id="map"></div>

<script>
const TOKEN = "{token or ''}";
const MAPS_KEY = "{maps_key}";
let map, directionsRenderer, incidentMarkers = [];

function initMap() {{
  map = new google.maps.Map(document.getElementById('map'), {{
    zoom: 12,
    center: {{lat: 30.267, lng: -97.743}},
    mapTypeId: 'roadmap',
    styles: [
      {{elementType:'geometry',stylers:[{{color:'#1d2c4d'}}]}},
      {{elementType:'labels.text.fill',stylers:[{{color:'#8ec3b9'}}]}},
      {{elementType:'labels.text.stroke',stylers:[{{color:'#1a3646'}}]}},
      {{featureType:'road',elementType:'geometry',stylers:[{{color:'#304a7d'}}]}},
      {{featureType:'road',elementType:'geometry.stroke',stylers:[{{color:'#255763'}}]}},
      {{featureType:'road.highway',elementType:'geometry',stylers:[{{color:'#2c6675'}}]}},
      {{featureType:'water',elementType:'geometry',stylers:[{{color:'#0e1626'}}]}},
    ]
  }});
  // directionsRenderer removed — using polyline decode instead
  refresh();
  setInterval(refresh, 300000);
}}

async function refresh() {{
  await Promise.all([loadTravel(), loadIncidents()]);
  document.getElementById('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}}

async function loadTravel() {{
  try {{
    const url = TOKEN ? `/api/commute/time` : `/api/commute/time`;
    const r = await fetch(url, {{credentials:'include'}});
    const d = await r.json();
    if (!d.configured) {{
      document.getElementById('travel-mins').textContent = '--';
      document.getElementById('travel-delta').textContent = 'No route configured';
      return;
    }}
    document.getElementById('travel-mins').textContent = d.live_mins + ' min';
    const delta = d.delta_mins;
    const deltaEl = document.getElementById('travel-delta');
    if (delta > 2)        deltaEl.style.color = '#f87171', deltaEl.textContent = '+' + delta + ' min vs normal';
    else if (delta < -2)  deltaEl.style.color = '#4ade80', deltaEl.textContent = delta + ' min vs normal';
    else                  deltaEl.style.color = '#94a3b8', deltaEl.textContent = 'Normal traffic';
    document.getElementById('travel-route').textContent = d.origin + ' → ' + d.destination;

    // Draw route via encoded polyline from server (avoids legacy Directions API)
    const purl = TOKEN ? `/api/commute/polyline?token=${{TOKEN}}` : `/api/commute/polyline`;
    fetch(purl, {{credentials:'include'}}).then(r => r.json()).then(pd => {{
      if (pd.polyline && window.google) {{
        const path = google.maps.geometry.encoding.decodePath(pd.polyline);
        new google.maps.Polyline({{
          path,
          map,
          strokeColor: '#f90',
          strokeWeight: 5,
          strokeOpacity: 0.85,
        }});
        // Fit map to route bounds
        const bounds = new google.maps.LatLngBounds();
        path.forEach(p => bounds.extend(p));
        map.fitBounds(bounds, {{top:40, right:40, bottom:40, left:40}});
      }}
    }}).catch(e => console.warn('polyline fetch failed', e));
  }} catch(e) {{
    document.getElementById('travel-delta').textContent = 'Travel data unavailable';
  }}
}}

async function loadIncidents() {{
  try {{
    const url = TOKEN ? `/api/commute/incidents?token=${{TOKEN}}` : `/api/commute/incidents`;
    const r = await fetch(url, {{credentials:'include'}});
    const d = await r.json();

    // Clear old markers
    incidentMarkers.forEach(m => m.setMap(null));
    incidentMarkers = [];

    if (!d.incidents || d.incidents.length === 0) {{
      document.getElementById('incident-bar').textContent = '✓ No active incidents near your route';
      return;
    }}

    const bar = d.incidents.map(inc =>
      `<span class="inc-badge" title="${{inc.description}}" onclick="focusIncident(${{inc.lat}},${{inc.lon}})">`+
      `${{inc.itype}} — ${{inc.dist_miles}}mi</span>`
    ).join('');
    document.getElementById('incident-bar').innerHTML = `⚠️ ${{d.incidents.length}} incident(s) near route: ` + bar;

    d.incidents.forEach(inc => {{
      const marker = new google.maps.Marker({{
        position: {{lat: inc.lat, lng: inc.lon}},
        map: map,
        icon: {{
          path: google.maps.SymbolPath.CIRCLE,
          scale: 10,
          fillColor: '#ef4444',
          fillOpacity: 0.9,
          strokeColor: '#fff',
          strokeWeight: 2,
        }},
        title: inc.itype,
      }});
      const info = new google.maps.InfoWindow({{
        content: `<div style="color:#111;max-width:220px"><strong>${{inc.itype}}</strong><br>${{inc.location || ''}}<br><small>${{inc.description}}</small></div>`
      }});
      marker.addListener('click', () => info.open(map, marker));
      incidentMarkers.push(marker);
    }});
  }} catch(e) {{
    document.getElementById('incident-bar').textContent = 'Incident data unavailable';
  }}
}}

function focusIncident(lat, lon) {{
  map.panTo({{lat, lng: lon}});
  map.setZoom(14);
}}

async function getShareLink() {{
  try {{
    const r = await fetch('/api/commute/share_token', {{method:'POST',credentials:'include'}});
    const d = await r.json();
    if (d.share_url) {{
      navigator.clipboard.writeText(d.share_url).then(() => {{
        document.getElementById('share-btn').textContent = 'Copied!';
        setTimeout(() => document.getElementById('share-btn').textContent = 'Share Link', 2000);
      }});
    }}
  }} catch(e) {{
    alert('Could not generate share link.');
  }}
}}
</script>
<script src="https://maps.googleapis.com/maps/api/js?key={maps_key}&libraries=geometry&callback=initMap" async defer></script>
</body></html>"""
    return html

# ---------------------------------------------------------------------------
# Intel Query — premium feature (5/month)
# ---------------------------------------------------------------------------

@app.route("/api/intel", methods=["POST"])
@_require_premium
def api_intel_query():
    sess = _get_session(request)
    username = sess["username"]

    # Check quota
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT intel_queries_used, intel_quota FROM premium_users WHERE username=?",
        (username,)
    ).fetchone()
    conn.close()

    if row:
        used, quota = row
        if used >= quota:
            return jsonify({"error": f"Monthly intel query limit reached ({quota}/month). Resets on the 1st."}), 429

    data = request.get_json(silent=True) or {}
    query_text = (data.get("query") or "").strip()
    if not query_text:
        return jsonify({"error": "query required"}), 400

    # Keyword extraction — simple approach, good enough for now
    keywords = re.findall(r'\b[A-Za-z]{4,}\b', query_text.lower())
    stop = {"what", "when", "where", "with", "have", "been", "that", "this",
            "from", "about", "were", "there", "they", "them", "their", "then"}
    keywords = [k for k in keywords if k not in stop][:8]

    # Build DB query — search transcripts and talkgroup names
    conn = sqlite3.connect(DB_PATH)
    results = []
    tgids_hit = set()

    if keywords:
        like_clauses = " OR ".join(["LOWER(transcript) LIKE ?" for _ in keywords])
        params = [f"%{k}%" for k in keywords]
        rows = conn.execute(
            f"SELECT ts, tgid, tag, transcript "
            f"FROM calls WHERE tgid != 0 AND ({like_clauses}) ORDER BY ts DESC LIMIT 50",
            params
        ).fetchall()
        for ts, tgid, tgname, transcript in rows:
            tgids_hit.add(str(tgid))
            results.append({
                "ts": ts,
                "tgid": tgid,
                "talkgroup": tgname or str(tgid),
                "transcript": transcript,
            })
    conn.close()

    if not results:
        return jsonify({
            "query": query_text,
            "summary": "No matching radio traffic found in the database for that query.",
            "calls_hit": 0,
            "tgids": [],
        })

    # Claude Haiku synthesis
    summary = f"Found {len(results)} matching call(s) across talkgroups: {', '.join(tgids_hit)}."
    if ANTHROPIC_ENABLED and results:
        excerpts = "\n".join(
            f"[{datetime.fromtimestamp(r['ts']).strftime('%Y-%m-%d %H:%M')}] "
            f"{r['talkgroup']}: {r['transcript'][:300]}"
            for r in results[:20]
        )
        prompt = (
            f"You are an intelligence analyst for Austin, TX first responder monitoring. "
            f"A user asked: \"{query_text}\"\n\n"
            f"Here are matching radio transcripts:\n{excerpts}\n\n"
            f"Write a concise intelligence summary (3-6 sentences) answering the user's question "
            f"based only on the transcripts above. Note dates, locations, and patterns if present. "
            f"Do not speculate beyond what the transcripts say."
        )
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                temperature=0.2,
                system="You are an intelligence analyst monitoring Austin, TX public safety radio traffic. You summarize publicly broadcast scanner data for journalists, researchers, and public safety professionals.",
                messages=[{"role": "user", "content": prompt}],
            )
            summary = response.content[0].text.strip()
        except Exception as e:
            print(f"[intel] Anthropic error: {e}", flush=True)

    # Increment usage counter and log
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE premium_users SET intel_queries_used = intel_queries_used + 1 WHERE username=?",
        (username,)
    )
    conn.execute(
        "INSERT INTO intel_queries (username, ts, query, result, tgids_hit, calls_hit) "
        "VALUES (?,?,?,?,?,?)",
        (username, time.time(), query_text, summary, ",".join(tgids_hit), len(results))
    )
    conn.commit()
    conn.close()

    return jsonify({
        "query": query_text,
        "summary": summary,
        "calls_hit": len(results),
        "tgids": list(tgids_hit),
        "quota_remaining": (quota - used - 1) if row else None,
    })


# ---------------------------------------------------------------------------
# Premium welcome page + subscription status
# ---------------------------------------------------------------------------

@app.route("/premium/welcome")
def premium_welcome():
    sess = _get_session(request)
    if sess and sess.get("is_premium"):
        from flask import redirect
        return redirect("/premium/")
    username = sess["username"] if sess else ""
    html = f"""<!DOCTYPE html>
<html><head><title>Welcome — Battle Buddy Premium</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:sans-serif;background:#111;color:#eee;max-width:600px;margin:60px auto;padding:20px}}
  h1{{color:#f90}}
  .box{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:24px;margin:20px 0}}
  a{{color:#f90}}
  input{{display:block;width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:10px;font-size:15px;margin-bottom:10px;box-sizing:border-box}}
  button{{background:#f90;color:#111;border:none;padding:12px 24px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:15px;width:100%}}
  button:hover{{background:#ffa820}}
  #login-err{{color:#f44;font-size:14px;margin-top:8px;display:none}}
</style></head><body>
<h1>Welcome to Battle Buddy Premium</h1>
<div class="box">
  <p>Payment confirmed. <strong>Check your email</strong> for a link to set your password.</p>
  <p>Once you've set your password, sign in here:</p>
  <h3>Sign In</h3>
  <input type="text" id="l-user" placeholder="Username" value="{username}" autocomplete="off">
  <input type="password" id="l-pass" placeholder="Password from your welcome email">
  <button onclick="doLogin()">Sign In to Dashboard</button>
  <div id="login-err"></div>
</div>
<div class="box">
  <h3>What's included:</h3>
  <ul>
    <li>Live incident alerts via Nextcloud Talk</li>
    <li>Priority notifications for high-severity events</li>
    <li>Intel Query — natural language search of Austin radio traffic (5/month)</li>
    <li>ATAK data package for field situational awareness</li>
    <li>Intel News Feed in Nextcloud News — auto-subscribed, live incident and press release updates</li>
  </ul>
  <p><a href="https://kevcloud.ddns.net" target="_blank">Open Nextcloud →</a></p>
</div>
<script>
async function doLogin() {{
  const username = document.getElementById('l-user').value.trim().toLowerCase();
  const password = document.getElementById('l-pass').value;
  const err = document.getElementById('login-err');
  err.style.display = 'none';
  if (!username || !password) {{ err.textContent = 'Enter username and password.'; err.style.display='block'; return; }}
  try {{
    const r = await fetch('/api/login', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{username, password}})
    }});
    if (r.ok) {{
      window.location.href = '/premium/';
    }} else {{
      const d = await r.json();
      err.textContent = d.error || 'Login failed. Check your credentials and try again.';
      err.style.display = 'block';
    }}
  }} catch(e) {{
    err.textContent = 'Network error. Try again.';
    err.style.display = 'block';
  }}
}}
</script>
</body></html>"""
    return html
@app.route("/premium/setup")
def premium_setup():
    token = request.args.get("token", "").strip()
    error = ""
    if not token:
        error = "Missing or invalid setup link."
    else:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT username, setup_token_expires FROM premium_users WHERE setup_token=?",
            (token,)
        ).fetchone()
        conn.close()
        if not row:
            error = "This setup link is invalid or has already been used."
        elif int(time.time()) > row[1]:
            error = "This setup link has expired. Contact support to get a new one."

    if error:
        return f"""<!DOCTYPE html>
<html><head><title>Setup — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:sans-serif;background:#111;color:#eee;max-width:500px;margin:80px auto;padding:20px}}
h1{{color:#f90}} .box{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:24px}} a{{color:#f90}}</style></head><body>
<h1>Battle Buddy Premium</h1><div class="box"><p style="color:#f44">{error}</p>
<p><a href="mailto:ops@mail.battlebuddy.news">Contact support</a></p></div></body></html>""", 400

    username = row[0]
    return f"""<!DOCTYPE html>
<html><head><title>Set Password — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:sans-serif;background:#111;color:#eee;max-width:500px;margin:80px auto;padding:20px}}
  h1{{color:#f90}}
  .box{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:24px;margin:20px 0}}
  input{{display:block;width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;
         padding:10px;font-size:15px;margin-bottom:12px;box-sizing:border-box}}
  button{{background:#f90;color:#111;border:none;padding:12px 24px;border-radius:6px;
          font-weight:bold;cursor:pointer;font-size:15px;width:100%}}
  button:hover{{background:#ffa820}}
  #err{{color:#f44;font-size:14px;margin-top:8px;display:none}}
</style></head><body>
<h1>Battle Buddy Premium</h1>
<div class="box">
  <p>Welcome, <strong>{username}</strong>. Set a password to access your dashboard.</p>
  <input type="password" id="pw1" placeholder="New password (min 8 characters)">
  <input type="password" id="pw2" placeholder="Confirm password">
  <button onclick="doSetup()">Set Password &amp; Sign In</button>
  <div id="err"></div>
</div>
<script>
async function doSetup() {{
  const pw1 = document.getElementById('pw1').value;
  const pw2 = document.getElementById('pw2').value;
  const err = document.getElementById('err');
  err.style.display = 'none';
  if (pw1.length < 8) {{ err.textContent = 'Password must be at least 8 characters.'; err.style.display='block'; return; }}
  if (pw1 !== pw2) {{ err.textContent = 'Passwords do not match.'; err.style.display='block'; return; }}
  const btn = document.querySelector('button');
  btn.textContent = 'Setting up...'; btn.disabled = true;
  try {{
    const r = await fetch('/api/premium/setpassword', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{token: '{token}', password: pw1}})
    }});
    const d = await r.json();
    if (r.ok) {{
      window.location.href = '/premium/';
    }} else {{
      err.textContent = d.error || 'Setup failed. Try again.';
      err.style.display = 'block';
      btn.textContent = 'Set Password & Sign In'; btn.disabled = false;
    }}
  }} catch(e) {{
    err.textContent = 'Network error. Try again.';
    err.style.display = 'block';
    btn.textContent = 'Set Password & Sign In'; btn.disabled = false;
  }}
}}
</script>
</body></html>"""


@app.route("/api/premium/setpassword", methods=["POST"])
def api_premium_setpassword():
    data = request.get_json(silent=True) or {}
    token    = (data.get("token") or "").strip()
    password = (data.get("password") or "").strip()
    if not token or len(password) < 8:
        return jsonify({"error": "Invalid request"}), 400

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, setup_token_expires FROM premium_users WHERE setup_token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Invalid or already-used setup link"}), 400
    username, expires = row
    if int(time.time()) > expires:
        return jsonify({"error": "Setup link has expired"}), 400

    # Change password via NC admin API
    nc_base   = "https://kevcloud.ddns.net/ocs/v2.php/cloud"
    auth_b64  = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers   = {"Authorization": f"Basic {auth_b64}", "OCS-APIREQUEST": "true",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"}
    body      = urllib.parse.urlencode({"key": "password", "value": password}).encode()
    req       = urllib.request.Request(
        f"{nc_base}/users/{username}", data=body, headers=headers, method="PUT"
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
            resp = json.loads(r.read())
            status = resp.get("ocs", {}).get("meta", {}).get("status")
            if status != "ok":
                print(f"[setup] NC password change failed for {username}: {resp}", flush=True)
                return jsonify({"error": "Password update failed. Contact support."}), 500
    except Exception as e:
        print(f"[setup] NC password change error for {username}: {e}", flush=True)
        return jsonify({"error": "Could not reach Nextcloud. Try again."}), 500

    # Invalidate token
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE premium_users SET setup_token=NULL, setup_token_expires=NULL WHERE username=?",
        (username,)
    )
    conn.commit()
    conn.close()

    # Issue session
    token_sess = _issue_session(username)
    from flask import make_response
    resp = make_response(jsonify({"status": "ok"}))
    resp.set_cookie("bb_session", token_sess, max_age=86400*30, httponly=True, samesite="Lax")
    print(f"[setup] password set and session issued for {username}", flush=True)
    return resp


@app.route("/api/subscription_status")
def api_subscription_status():
    sess = _get_session(request)
    if not sess:
        return jsonify({"premium": False, "logged_in": False})
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT status, intel_queries_used, intel_quota FROM premium_users WHERE username=?",
        (sess["username"],)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"premium": False, "logged_in": True, **sess})
    status, used, quota = row
    return jsonify({
        "logged_in": True,
        "premium": status == "active",
        "status": status,
        "intel_queries_used": used,
        "intel_quota": quota,
        "intel_quota_remaining": max(0, quota - used),
        **sess,
    })



# ---------------------------------------------------------------------------
# Weather API (NWS — no key required)
# ---------------------------------------------------------------------------
_nws_cache = {}        # keyed by (lat_rounded, lon_rounded)
_NWS_CACHE_TTL = 900  # 15 min

def _get_nws_weather(lat=30.2672, lon=-97.7431):
    import time as _time
    now  = _time.time()
    key  = (round(lat, 2), round(lon, 2))
    cached = _nws_cache.get(key)
    if cached and now - cached["ts"] < _NWS_CACHE_TTL:
        return cached["data"]
    try:
        headers = {"User-Agent": "BattleBuddy/2.0 (ops@battlebuddy.news)"}
        req = urllib.request.Request(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            pts = json.loads(r.read())
        forecast_url = pts["properties"]["forecast"]
        hourly_url   = pts["properties"]["forecastHourly"]

        req2 = urllib.request.Request(forecast_url, headers=headers)
        with urllib.request.urlopen(req2, timeout=8) as r:
            fc = json.loads(r.read())

        req3 = urllib.request.Request(hourly_url, headers=headers)
        with urllib.request.urlopen(req3, timeout=8) as r:
            hr = json.loads(r.read())

        periods  = fc["properties"]["periods"]
        current  = hr["properties"]["periods"][0]

        # Build 5-day forecast (daytime periods only)
        days = []
        for p in periods:
            if p["isDaytime"] and len(days) < 5:
                days.append({
                    "name":      p["name"],
                    "temp":      p["temperature"],
                    "unit":      p["temperatureUnit"],
                    "icon":      p.get("icon", ""),
                    "short":     p["shortForecast"],
                    "precip":    p.get("probabilityOfPrecipitation", {}).get("value") or 0,
                })

        result = {
            "current": {
                "temp":    current["temperature"],
                "unit":    current["temperatureUnit"],
                "wind":    current.get("windSpeed", ""),
                "desc":    current["shortForecast"],
                "icon":    current.get("icon", ""),
            },
            "forecast": days,
        }
        _nws_cache[key] = {"data": result, "ts": now}
        return result
    except Exception as e:
        print(f"[weather] NWS fetch error: {e}", flush=True)
        return None


@app.route("/api/premium/homicides/summary")
def api_premium_homicides_summary():
    sess = _get_session(request)
    if not sess or not sess.get("is_premium"):
        return jsonify({"error": "premium required"}), 403
    import os
    seed_count = 0
    seed_path  = "/opt/battlebuddy/homicides_2026.json"
    if os.path.exists(seed_path):
        try:
            import json as _json
            seed_data = _json.load(open(seed_path))
            seed_count = sum(int(e.get("count", 1)) for e in seed_data)
        except Exception:
            pass
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT ts_start, location FROM incidents
           WHERE itype = 'HOMICIDE'
             AND lat IS NOT NULL AND lon IS NOT NULL
             AND ts_start > strftime('%s','2026-01-01')
             AND is_test = 0
           ORDER BY ts_start DESC"""
    ).fetchall()
    conn.close()
    live_count = len(rows)
    # Derive "last" — prefer live geocoded entry, fall back to seed file
    last = None
    if rows:
        import datetime as _dt
        last = {
            "date":     _dt.datetime.fromtimestamp(rows[0][0]).strftime("%b %d"),
            "location": rows[0][1] or "",
        }
    elif seed_count:
        try:
            import json as _json2
            seed_data = sorted(_json2.load(open(seed_path)), key=lambda x: x.get("date",""))
            newest = seed_data[-1]
            from datetime import datetime as _dt2
            last = {
                "date":     _dt2.strptime(newest["date"], "%Y-%m-%d").strftime("%b %d"),
                "location": newest.get("address", ""),
            }
        except Exception:
            pass
    return jsonify({
        "ytd":   seed_count + live_count,
        "year":  2026,
        "last":  last,
    })


@app.route("/api/premium/atak/status")
def api_premium_atak_status():
    sess = _get_session(request)
    if not sess or not sess.get("is_premium"):
        return jsonify({"error": "premium required"}), 403
    with _fts_lock:
        connected = _fts_socket is not None
    return jsonify({"connected": connected, "fts_enabled": FTS_ENABLED})


@app.route("/api/premium/weather")
def api_premium_weather():
    sess = _get_session(request)
    if not sess or not sess.get("is_premium"):
        return jsonify({"error": "premium required"}), 403
    # Use commute origin coords if saved, otherwise default to Austin
    lat, lon, location_label = 30.2672, -97.7431, "Austin TX"
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            "SELECT commute_origin_lat, commute_origin_lon, commute_origin "
            "FROM premium_users WHERE username=?", (sess["username"],)
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            lat, lon = row[0], row[1]
            # Use first part of address as label (strip to city if possible)
            raw = (row[2] or "").strip()
            # "S IH-35 and Slaughter Ln, Austin TX" → "Austin TX"
            if "," in raw:
                location_label = raw.split(",")[-1].strip()
            elif raw:
                location_label = raw
    except Exception as e:
        print(f"[weather] DB lookup error: {e}", flush=True)
    data = _get_nws_weather(lat, lon)
    if not data:
        return jsonify({"error": "weather unavailable"}), 503
    data["location"] = location_label
    return jsonify(data)


# ---------------------------------------------------------------------------
# Premium Display Dashboard (/premium/display)
# ---------------------------------------------------------------------------
@app.route("/premium/display")
def premium_display():
    sess = _get_session(request)
    html = """<!DOCTYPE html>
<html><head>
<title>Battle Buddy Display</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#e2e8f0;min-height:100vh;padding:16px}
  #login-overlay{position:fixed;inset:0;background:#0a0a0f;display:flex;align-items:center;justify-content:center;z-index:100}
  .login-box{background:#111827;border:1px solid #1e3a5f;border-radius:12px;padding:36px;width:340px;text-align:center}
  .login-box h2{color:#f90;margin-bottom:20px;font-size:22px}
  .login-box input{width:100%;background:#1f2937;color:#eee;border:1px solid #374151;border-radius:6px;padding:10px;font-size:15px;margin-bottom:10px}
  .login-box button{width:100%;background:#f90;color:#111;border:none;padding:11px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:16px}
  .login-box button:hover{background:#ffa820}
  .login-err{color:#f44;font-size:13px;margin-top:8px;display:none}

  #dashboard{display:none}
  .top-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
  .clock{font-size:48px;font-weight:700;color:#fff;letter-spacing:-1px;line-height:1}
  .clock-date{font-size:15px;color:#94a3b8;margin-top:3px}
  .top-links{display:flex;gap:12px;align-items:center}
  .top-links a{color:#64748b;font-size:13px;text-decoration:none}
  .top-links a:hover{color:#f90}
  .panel-toggles{display:flex;gap:8px;flex-wrap:wrap}
  .ptog{background:#1f2937;color:#94a3b8;border:1px solid #374151;border-radius:20px;padding:4px 12px;font-size:12px;cursor:pointer;user-select:none}
  .ptog.on{background:#1a2e1a;color:#4ade80;border-color:#16a34a}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
  .panel{background:#111827;border:1px solid #1e293b;border-radius:12px;padding:18px}
  .panel-title{font-size:11px;font-weight:700;color:#475569;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:12px}

  /* Weather */
  .wx-current{display:flex;align-items:center;gap:16px;margin-bottom:14px}
  .wx-temp{font-size:56px;font-weight:700;color:#fff;line-height:1}
  .wx-desc{font-size:15px;color:#94a3b8;margin-top:4px}
  .wx-wind{font-size:13px;color:#64748b}
  .wx-days{display:flex;gap:8px;flex-wrap:wrap}
  .wx-day{flex:1;min-width:80px;background:#0f172a;border-radius:8px;padding:8px;text-align:center}
  .wx-day-name{font-size:11px;color:#64748b;margin-bottom:4px}
  .wx-day-temp{font-size:18px;font-weight:700;color:#f90}
  .wx-day-short{font-size:11px;color:#94a3b8;margin-top:3px}
  .wx-day-precip{font-size:11px;color:#38bdf8;margin-top:2px}

  /* Incidents */
  .inc-list{display:flex;flex-direction:column;gap:8px;max-height:280px;overflow:hidden}
  .inc-item{display:flex;gap:10px;align-items:flex-start;padding:8px;background:#0f172a;border-radius:8px}
  .inc-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;margin-top:4px}
  .inc-type{font-size:13px;font-weight:600;color:#e2e8f0}
  .inc-loc{font-size:12px;color:#64748b;margin-top:2px}
  .inc-time{font-size:11px;color:#475569;white-space:nowrap;flex-shrink:0}
  .inc-none{color:#475569;font-size:14px;padding:12px 0}

  /* Commute */
  .commute-time{font-size:42px;font-weight:700;color:#fff;line-height:1}
  .commute-vs{font-size:14px;margin-top:6px}
  .commute-faster{color:#4ade80}
  .commute-slower{color:#f87171}
  .commute-normal{color:#94a3b8}
  .commute-setup{color:#64748b;font-size:14px;line-height:1.6}
  .commute-setup a{color:#f90;text-decoration:none}

  /* Headlines */
  .hl-list{display:flex;flex-direction:column;gap:6px}
  .hl-item{padding:7px 0;border-bottom:1px solid #1e293b;font-size:13px;color:#cbd5e1;line-height:1.4}
  .hl-item:last-child{border-bottom:none}
  .hl-time{font-size:11px;color:#475569;margin-top:2px}

  .dot-red{background:#ef4444}
  .dot-orange{background:#f97316}
  .dot-yellow{background:#eab308}
  .dot-blue{background:#3b82f6}
  .dot-gray{background:#6b7280}

  @media(max-width:600px){.clock{font-size:32px}.wx-temp{font-size:40px}}
  .atak-status{display:flex;align-items:center;gap:6px;font-size:11px;color:#64748b;margin-top:4px}
  .atak-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
  .atak-dot.on{background:#4ade80;box-shadow:0 0 6px #4ade80}
  .atak-dot.off{background:#ef4444;box-shadow:0 0 6px #ef4444}
</style>
</head><body>

<div id="login-overlay">
  <div class="login-box">
    <h2>Battle Buddy</h2>
    <input type="text" id="l-user" placeholder="Username" autocomplete="username">
    <input type="password" id="l-pass" placeholder="Password" autocomplete="current-password">
    <button onclick="doLogin()">Sign In</button>
    <div class="login-err" id="l-err"></div>
  </div>
</div>

<div id="dashboard">
  <div class="top-bar">
    <div>
      <div class="clock" id="clock">--:--</div>
      <div class="clock-date" id="clock-date"></div>
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
      <div class="top-links">
        <a href="/premium/">Dashboard</a>
        <a href="https://kevcloud.ddns.net" target="_blank">Nextcloud</a>
        <a href="#" onclick="toggleFullscreen()" id="fs-btn">⛶ Full Screen</a>
        <a href="#" onclick="doLogout()" style="color:#ef4444">Sign out</a>
      </div>
      <div class="atak-status">
        <div class="atak-dot off" id="atak-dot"></div>
        <span id="atak-label">TAK Server</span>
      </div>
      <div class="panel-toggles" id="panel-toggles"></div>
    </div>
  </div>
  <div class="grid" id="grid"></div>
</div>

<script>
const PANELS = [
  {id:'weather',   label:'Weather'},
  {id:'incidents', label:'Incidents'},
  {id:'commute',   label:'Commute'},
  {id:'headlines', label:'Headlines'},
  {id:'homicides', label:'Homicides'},
];

let state = {weather:null, incidents:null, commute:null, headlines:null, homicides:null};
let lastUpdated = {weather:null, incidents:null, commute:null, headlines:null, homicides:null};
let username = '';

// ── Clock ────────────────────────────────────────────────────────────────────
function tickClock(){
  const now = new Date();
  const h = now.getHours(), m = now.getMinutes();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12  = h % 12 || 12;
  document.getElementById('clock').textContent = h12 + ':' + String(m).padStart(2,'0') + ' ' + ampm;
  document.getElementById('clock-date').textContent = now.toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric',year:'numeric'});
}
setInterval(tickClock, 1000);
tickClock();

// ── Panel toggles ────────────────────────────────────────────────────────────
function getVisible(){
  try { return JSON.parse(localStorage.getItem('bb_display_panels') || 'null') || PANELS.map(p=>p.id); }
  catch(e){ return PANELS.map(p=>p.id); }
}
function setVisible(ids){ localStorage.setItem('bb_display_panels', JSON.stringify(ids)); }

function buildToggles(){
  const vis = getVisible();
  const el = document.getElementById('panel-toggles');
  el.innerHTML = '';
  PANELS.forEach(p => {
    const on = vis.includes(p.id);
    const b = document.createElement('span');
    b.className = 'ptog' + (on ? ' on' : '');
    b.textContent = p.label;
    b.onclick = () => {
      let v = getVisible();
      if(v.includes(p.id)) v = v.filter(x=>x!==p.id);
      else v.push(p.id);
      setVisible(v);
      buildToggles();
      renderGrid();
    };
    el.appendChild(b);
  });
}

// ── Auth ─────────────────────────────────────────────────────────────────────
async function checkSession(){
  const r = await fetch('/api/me');
  const d = await r.json();
  if(d.logged_in && d.is_premium){
    username = d.username;
    showDashboard();
  }
}
async function doLogin(){
  const u = document.getElementById('l-user').value.trim().toLowerCase();
  const p = document.getElementById('l-pass').value;
  const err = document.getElementById('l-err');
  err.style.display='none';
  if(!u||!p){err.textContent='Enter username and password.';err.style.display='block';return;}
  const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
  const d = await r.json();
  if(d.token && d.is_premium){ username=d.username; showDashboard(); }
  else if(d.token){ err.textContent='This account does not have premium access.'; err.style.display='block'; }
  else { err.textContent=d.error||'Invalid credentials.'; err.style.display='block'; }
}
function toggleFullscreen(){
  const el = document.documentElement;
  if (!document.fullscreenElement) {
    el.requestFullscreen().catch(()=>{});
    document.getElementById("fs-btn").textContent = "✕ Exit Full Screen";
  } else {
    document.exitFullscreen();
    document.getElementById("fs-btn").textContent = "⛶ Full Screen";
  }
}
document.addEventListener("fullscreenchange", () => {
  const btn = document.getElementById("fs-btn");
  if(btn) btn.textContent = document.fullscreenElement ? "✕ Exit Full Screen" : "⛶ Full Screen";
});
async function pollAtak(){
  try{
    const r = await fetch('/api/premium/atak/status');
    const d = await r.json();
    const dot   = document.getElementById('atak-dot');
    const label = document.getElementById('atak-label');
    if(d.connected){
      dot.className='atak-dot on';
      label.textContent='TAK Server';
    } else {
      dot.className='atak-dot off';
      label.textContent='TAK Offline';
    }
  }catch(e){}
}
async function doLogout(){
  await fetch('/api/logout',{method:'POST'});
  location.reload();
}
function showDashboard(){
  document.getElementById('login-overlay').style.display='none';
  document.getElementById('dashboard').style.display='block';
  buildToggles();
  loadAll();
  pollAtak();
  setInterval(loadAll, 60000);
  setInterval(pollAtak, 30000);
}

// ── Data loaders ─────────────────────────────────────────────────────────────
async function loadAll(){
  const vis = getVisible();
  if(vis.includes('weather'))   loadWeather();
  if(vis.includes('incidents')) loadIncidents();
  if(vis.includes('commute'))   loadCommute();
  if(vis.includes('headlines')) loadHeadlines();
  if(vis.includes('homicides')) loadHomicides();
}

async function loadWeather(){
  try{
    const r = await fetch('/api/premium/weather');
    state.weather = await r.json();
    if(!state.weather.error) lastUpdated.weather = new Date();
  }catch(e){ state.weather = null; }
  renderGrid();
}
async function loadIncidents(){
  try{
    const r = await fetch('/api/incidents/active');
    state.incidents = await r.json();
    lastUpdated.incidents = new Date();
  }catch(e){ state.incidents = null; }
  renderGrid();
}
async function loadCommute(){
  try{
    const r = await fetch('/api/commute/time');
    state.commute = await r.json();
    lastUpdated.commute = new Date();
  }catch(e){ state.commute = null; }
  renderGrid();
}
async function loadHomicides(){
  try{
    const r = await fetch('/api/premium/homicides/summary');
    state.homicides = await r.json();
    lastUpdated.homicides = new Date();
  }catch(e){ state.homicides = null; }
  renderGrid();
}
async function loadHeadlines(){
  try{
    const r = await fetch('/api/premium/headlines');
    state.headlines = await r.json();
    lastUpdated.headlines = new Date();
  }catch(e){ state.headlines = null; }
  renderGrid();
}

// ── Renderers ─────────────────────────────────────────────────────────────────
function incDotClass(itype){
  const t = (itype||'').toUpperCase();
  if(['SHOOTING','OFFICER DOWN','PURSUIT','WEAPONS','STABBING','MASS CASUALTY'].includes(t)) return 'dot-red';
  if(['STRUCTURE FIRE','HAZMAT','AIR ASSET ACTIVE'].includes(t)) return 'dot-orange';
  if(['FIRE DISPATCH','EMS DISPATCH'].includes(t)) return 'dot-yellow';
  if(['CRASH/COLLISION'].includes(t)) return 'dot-blue';
  return 'dot-gray';
}

function _ts(key){
  const d = lastUpdated[key];
  if(!d) return '';
  return `<div style="font-size:10px;color:#334155;margin-top:10px;text-align:right">Updated ${d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})}</div>`;
}
function renderWeather(w){
  if(!w || w.error) return '<div class="panel"><div class="panel-title">Weather</div><div class="inc-none">Weather unavailable</div></div>';
  const c = w.current;
  const days = (w.forecast||[]).slice(0,5).map(d=>`
    <div class="wx-day">
      <div class="wx-day-name">${d.name}</div>
      <div class="wx-day-temp">${d.temp}&deg;</div>
      <div class="wx-day-short">${d.short}</div>
      ${d.precip>0?`<div class="wx-day-precip">&#9730; ${d.precip}%</div>`:''}
    </div>`).join('');
  return `<div class="panel">
    <div class="panel-title">Weather &mdash; ${w.location||'Austin TX'}</div>
    <div class="wx-current">
      <div>
        <div class="wx-temp">${c.temp}&deg;${c.unit}</div>
        <div class="wx-desc">${c.desc}</div>
        <div class="wx-wind">${c.wind}</div>
      </div>
    </div>
    <div class="wx-days">${days}</div>
    ${_ts("weather")}
  </div>`;
}

function renderIncidents(data){
  let items = Array.isArray(data) ? data : (data&&data.incidents ? data.incidents : []);
  items = items.slice(0,6);
  const rows = items.length ? items.map(i=>{
    const t = new Date(i.started_at*1000||i.ts*1000||Date.now());
    const age = Math.round((Date.now()-t)/60000);
    const ageStr = age < 60 ? age+'m ago' : Math.round(age/60)+'h ago';
    const artLink = i.article_url
      ? `<a href="${i.article_url}" target="_blank" rel="noopener" style="display:block;font-size:11px;color:#38bdf8;margin-top:3px;text-decoration:none">📰 Press coverage ↗</a>`
      : '';
    return `<div class="inc-item">
      <div class="inc-dot ${incDotClass(i.itype)}"></div>
      <div style="flex:1">
        <div class="inc-type">${i.itype||'Unknown'}</div>
        <div class="inc-loc">${i.location||i.description||''}</div>
        ${artLink}
      </div>
      <div class="inc-time">${ageStr}</div>
    </div>`;
  }).join('') : '<div class="inc-none">No active incidents</div>';
  return `<div class="panel"><div class="panel-title">Active Incidents</div><div class="inc-list">${rows}</div>${_ts("incidents")}</div>`;
}

function renderCommute(data){
  if(!data || data.error === 'no_route'){
    return `<div class="panel"><div class="panel-title">Commute</div>
      <div class="commute-setup">No commute route configured.<br><br>
      <a href="/premium/">Set up your commute &rarr;</a></div></div>`;
  }
  if(!data || data.error){
    return `<div class="panel"><div class="panel-title">Commute</div><div class="inc-none">Traffic unavailable</div></div>`;
  }
  const mins = data.live_mins||0;
  const base = data.baseline_mins||0;
  const diff = mins - base;
  let vs = '', cls = 'commute-normal';
  if(base > 0){
    if(diff > 3){ vs=`+${diff} min vs normal`; cls='commute-slower'; }
    else if(diff < -3){ vs=`${diff} min vs normal`; cls='commute-faster'; }
    else{ vs='On time'; cls='commute-normal'; }
  }
  const h = Math.floor(mins/60), m = mins%60;
  const timeStr = h>0 ? `${h}h ${m}m` : `${m} min`;
  return `<div class="panel"><div class="panel-title">Commute &mdash; ${data.origin||''} &rarr; ${data.dest||''}</div>
    <div class="commute-time">${timeStr}</div>
    ${vs?`<div class="commute-vs ${cls}">${vs}</div>`:''}
    ${_ts("commute")}
  </div>`;
}

function renderHeadlines(data){
  if(!Array.isArray(data)||data.length===0)
    return `<div class="panel"><div class="panel-title">Incident Headlines</div><div class="inc-none">No press coverage in last 24h</div>${_ts("headlines")}</div>`;
  const rows = data.map(h=>{
    const age = Math.round((Date.now()-h.ts*1000)/60000);
    const ageStr = age<60?age+'m ago':(age<1440?Math.round(age/60)+'h ago':Math.round(age/1440)+'d ago');
    const src = h.source?`<span style="color:#475569;font-size:11px"> · ${h.source}</span>`:'';
    const link = h.url
      ? `<a href="${h.url}" target="_blank" rel="noopener" style="color:#38bdf8;text-decoration:none;font-weight:500">${h.headline.replace(/ - [^-]+$/,"")}</a>`
      : `<span>${h.headline}</span>`;
    let radio = '';
    if(h.incident){
      const inc = h.incident;
      const lead = inc.ts_start ? Math.round((h.ts-inc.ts_start)/60) : null;
      const leadStr = lead!==null?(lead>=0?lead+'m before article':Math.abs(lead)+'m after'):'';
      const loc = inc.location?` · ${inc.location}`:'';
      radio = `<div style="font-size:11px;color:#4ade80;margin-top:3px">&#128225; Radio: ${inc.itype||'??'}${loc}${leadStr?" ("+leadStr+")":""}</div>`;
    }
    return `<div class="hl-item">${link}${src}${radio}<div class="hl-time">${ageStr}</div></div>`;
  }).join('');
  return `<div class="panel"><div class="panel-title">Incident Headlines</div><div class="hl-list">${rows}</div>${_ts("headlines")}</div>`;
}

function renderHomicides(data){
  if(!data || data.error) return '<div class="panel"><div class="panel-title">Homicides YTD</div><div class="inc-none">Unavailable</div></div>';
  const last = data.last ? `<div style="font-size:12px;color:#64748b;margin-top:6px">Last: ${data.last.date}${data.last.location ? ' &mdash; ' + data.last.location : ''}</div>` : '';
  return `<div class="panel">
    <div class="panel-title">Homicides &mdash; Austin ${data.year} YTD</div>
    <div style="font-size:72px;font-weight:800;color:#ef4444;line-height:1">${data.ytd}</div>
    ${last}
    ${_ts("homicides")}
  </div>`;
}
function renderGrid(){
  const vis = getVisible();
  const map = {
    weather:   renderWeather(state.weather),
    incidents: renderIncidents(state.incidents),
    commute:   renderCommute(state.commute),
    headlines: renderHeadlines(state.headlines),
    homicides: renderHomicides(state.homicides),
  };
  document.getElementById('grid').innerHTML = vis.map(id=>map[id]||'').join('');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.getElementById('l-pass').addEventListener('keydown', e => {
  if(e.key === 'Enter') doLogin();
});
// Apply ?zoom= URL param
const _zp = new URLSearchParams(location.search).get("zoom");
if(_zp && parseFloat(_zp) > 0) document.body.style.zoom = (parseFloat(_zp) * 100) + "%";
checkSession();
</script>
</body></html>"""
    return html



@app.route("/api/premium/headlines")
def premium_headlines():
    """Return incident_articles linked in the last 24h for the Headlines panel."""
    sess = _get_session(request)
    if not sess or (not sess.get("is_admin") and not sess.get("is_premium")):
        return jsonify({"error": "unauthorized"}), 401
    cutoff = time.time() - 24 * 3600
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT ia.id, ia.ts, ia.headline, ia.url, ia.source, ia.snippet, ia.match_score,
               i.id   AS inc_id,    i.itype,    i.location,
               i.ts_start,          i.description
        FROM incident_articles ia
        LEFT JOIN incidents i ON ia.incident_id = i.id
        WHERE ia.ts >= ?
        ORDER BY ia.ts DESC LIMIT 20
    """, (cutoff,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id":          r["id"],
            "ts":          r["ts"],
            "headline":    r["headline"],
            "url":         r["url"],
            "source":      r["source"],
            "snippet":     r["snippet"],
            "match_score": r["match_score"],
            "incident": {
                "id":       r["inc_id"],
                "itype":    r["itype"],
                "location": r["location"],
                "ts_start": r["ts_start"],
            } if r["inc_id"] else None,
        })
    return jsonify(out)


@app.route("/api/premium/citizen_intel")
def api_premium_citizen_intel():
    sess = _get_session(request)
    if not sess:
        return jsonify({"error": "unauthorized"}), 401
    cutoff = time.time() - 7 * 86400
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT r.ts, r.post_id, r.subreddit, r.title, r.url, r.author,
               r.keywords, r.incident_id, r.match_score,
               i.ts_start, i.itype, i.location,
               (SELECT COUNT(*) FROM incident_calls ic WHERE ic.incident_id = r.incident_id) as call_count
        FROM reddit_intel r
        LEFT JOIN incidents i ON r.incident_id = i.id
        WHERE r.ts > ?
        ORDER BY r.ts DESC LIMIT 40
    """, (cutoff,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        ts,post_id,subreddit,title,url,author,keywords,inc_id,score,inc_ts,itype,location,calls = row
        result.append({
            "ts": ts, "post_id": post_id, "subreddit": subreddit,
            "title": title, "url": url, "author": author,
            "keywords": keywords or "", "incident_id": inc_id,
            "match_score": score, "incident_ts": inc_ts,
            "incident_type": itype, "incident_location": location,
            "call_count": calls or 0
        })
    return jsonify(result)

@app.route("/premium/")
def premium_index():
    sess = _get_session(request)
    logged_in = sess is not None
    is_premium = sess["is_premium"] if sess else False
    is_admin   = sess["is_admin"]   if sess else False
    username = sess["username"] if sess else ""

    if is_premium or is_admin:
        # Redirect active premium members to dashboard
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT intel_queries_used, intel_quota FROM premium_users WHERE username=?",
            (username,)
        ).fetchone()
        conn.close()
        used = row[0] if row else 0
        quota = row[1] if row else 5
        remaining = max(0, quota - used)
        dashboard = f"""<!DOCTYPE html>
<html><head><title>Premium Dashboard — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:sans-serif;background:#111;color:#eee;max-width:700px;margin:40px auto;padding:20px}}
  h1{{color:#f90;margin-bottom:4px}}
  .sub{{color:#888;margin-bottom:30px}}
  .card{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:20px;margin:16px 0}}
  .card h3{{margin:0 0 8px;color:#f90}}
  .badge{{display:inline-block;background:#f90;color:#111;font-weight:bold;border-radius:4px;padding:2px 10px;font-size:13px}}
  .quota{{font-size:28px;font-weight:bold;color:#f90}}
  textarea{{width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:10px;font-size:14px;resize:vertical}}
  button{{background:#f90;color:#111;border:none;padding:10px 24px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:15px}}
  button:hover{{background:#ffa820}}
  #result{{background:#222;border:1px solid #333;border-radius:6px;padding:16px;margin-top:16px;display:none;white-space:pre-wrap;line-height:1.6}}
  .logout{{float:right;color:#888;text-decoration:none;font-size:13px}}
  .logout:hover{{color:#eee}}
</style></head><body>
<a class="logout" href="#" onclick="logout()">Sign out</a>
<h1>Battle Buddy Premium</h1>
<p class="sub">Welcome back, <strong>{username}</strong> &nbsp;<span class="badge">PREMIUM</span></p>

<div class="card">
  <h3>Intel Query</h3>
  <p>Ask a question about Austin radio traffic. We search the database and synthesize an intelligence summary.</p>
  <p>Queries remaining this month: <span class="quota">{remaining}</span> / {quota}</p>
  <textarea id="q" rows="3" placeholder="e.g. Any shootings near North Lamar in the last week?"></textarea><br><br>
  <button onclick="runQuery()">Run Intel Query</button>
  <div id="result"></div>
</div>

<div class="card">
  <h3>&#128269; Citizen Intel &mdash; Reddit &times; Radio</h3>
  <p style="font-size:13px;color:#888;margin-bottom:14px">Citizens reporting incidents on r/Austin, auto cross-referenced against captured radio traffic. Updated every 5 minutes.</p>
  <div id="ci-list"><div style="color:#666;font-size:13px">Loading...</div></div>
</div>

<div class="card">
  <h3>Live Alerts</h3>
  <p>You are enrolled in priority incident alerts via Nextcloud Talk.</p>
  <p><a href="https://kevcloud.ddns.net" target="_blank" style="color:#f90">Open Nextcloud Talk →</a></p>
</div>

<div class="card">
  <h3>🚗 Commute Monitor</h3>
  <p>Save your commute route and Battle Buddy will alert you via Talk when an active incident is detected near your path — with current travel time vs your normal commute.</p>
  <div id="commute-status" style="margin:12px 0;font-size:14px;color:#aaa">Loading...</div>
  <div id="commute-form" style="display:none">
    <input type="text" id="commute-origin" placeholder="Origin — include city & state (e.g. Slaughter Ln &amp; I-35, Austin TX)"
      style="width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:8px;font-size:14px;margin-bottom:8px;box-sizing:border-box">
    <input type="text" id="commute-dest" placeholder="Destination — include city &amp; state (e.g. 220 E 6th St, Austin TX)"
      style="width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:8px;font-size:14px;margin-bottom:8px;box-sizing:border-box">
    <button onclick="saveCommute()" style="background:#f90;color:#111;border:none;padding:8px 20px;border-radius:6px;font-weight:bold;cursor:pointer">Save Route</button>
    <div id="commute-err" style="color:#f44;font-size:13px;margin-top:6px;display:none"></div>
  </div>
  <div id="commute-live" style="display:none;margin-top:10px">
    <div style="font-size:28px;font-weight:bold;color:#f90" id="commute-mins">--</div>
    <div style="font-size:13px;color:#888" id="commute-delta"></div>
    <div style="font-size:12px;color:#555;margin-top:4px" id="commute-route"></div>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <a href="/premium/commute" target="_blank" style="background:#f90;color:#111;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:bold;text-decoration:none">Open Live Map →</a>
      <button onclick="showCommuteForm()" style="background:#333;color:#eee;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">Change Route</button>
    </div>
  </div>
</div>

<div class="card">
  <h3>Intel News Feed</h3>
  <p>You are auto-subscribed to the <strong style="color:#f90">Battle Buddy Intel Feed</strong> in Nextcloud News. Every confirmed incident, APD press release, and homicide update — one scrollable feed, updates continuously.</p>
  <p><a href="https://kevcloud.ddns.net/apps/news" target="_blank" style="color:#f90">Open Nextcloud News →</a></p>
  <p style="margin-top:10px;font-size:12px;color:#666">You can also subscribe any RSS reader to the feed directly:<br>
  <code style="color:#aaa;font-size:11px">https://battlebuddy.news/public/feed.rss</code></p>
</div>

<div class="card">
  <h3>ATAK Field Package</h3>
  <p>Contact Battle Buddy Ops to receive your ATAK data package for field situational awareness.</p>
</div>

<script>
async function runQuery() {{
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const btn = document.querySelector('button');
  btn.textContent = 'Searching...';
  btn.disabled = true;
  const res = document.getElementById('result');
  res.style.display = 'block';
  res.textContent = 'Querying database and synthesizing...';
  try {{
    const r = await fetch('/api/intel', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{query: q}})
    }});
    const d = await r.json();
    if (d.error) {{
      res.textContent = 'Error: ' + d.error;
    }} else {{
      res.textContent = d.summary + '\\n\\n[' + d.calls_hit + ' call(s) matched | TGIDs: ' + (d.tgids.join(', ') || 'none') + ' | ' + (d.quota_remaining ?? '?') + ' queries remaining]';
    }}
  }} catch(e) {{
    res.textContent = 'Request failed: ' + e;
  }}
  btn.textContent = 'Run Intel Query';
  btn.disabled = false;
}}
async function logout() {{
  await fetch('/api/logout', {{method:'POST'}});
  window.location.href = '/premium/';
}}

async function loadCommuteStatus() {{
  try {{
    const r = await fetch('/api/commute/time');
    const d = await r.json();
    const status = document.getElementById('commute-status');
    const live   = document.getElementById('commute-live');
    const form   = document.getElementById('commute-form');
    status.style.display = 'none';
    if (!d.configured) {{
      form.style.display = 'block';
      live.style.display = 'none';
    }} else {{
      form.style.display = 'none';
      live.style.display = 'block';
      document.getElementById('commute-mins').textContent = d.live_mins + ' min';
      const delta = d.delta_mins;
      const deltaEl = document.getElementById('commute-delta');
      if (delta > 0)       deltaEl.textContent = '+' + delta + ' min over normal';
      else if (delta < 0)  deltaEl.textContent = Math.abs(delta) + ' min faster than normal';
      else                 deltaEl.textContent = 'Normal commute time';
      document.getElementById('commute-route').textContent = d.origin + ' → ' + d.destination;
    }}
  }} catch(e) {{
    document.getElementById('commute-status').textContent = 'Commute data unavailable.';
  }}
}}

function showCommuteForm() {{
  document.getElementById('commute-live').style.display = 'none';
  document.getElementById('commute-form').style.display = 'block';
}}

async function saveCommute() {{
  const origin = document.getElementById('commute-origin').value.trim();
  const dest   = document.getElementById('commute-dest').value.trim();
  const err    = document.getElementById('commute-err');
  err.style.display = 'none';
  if (!origin || !dest) {{ err.textContent = 'Both addresses required.'; err.style.display='block'; return; }}
  const btn = document.querySelector('#commute-form button');
  btn.textContent = 'Saving...'; btn.disabled = true;
  try {{
    const r = await fetch('/api/commute/save', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{origin, destination: dest}})
    }});
    const d = await r.json();
    if (d.ok) {{
      await loadCommuteStatus();
    }} else {{
      err.textContent = d.error || 'Save failed.'; err.style.display='block';
    }}
  }} catch(e) {{
    err.textContent = 'Network error.'; err.style.display='block';
  }}
  btn.textContent = 'Save Route'; btn.disabled = false;
}}

loadCommuteStatus();

async function loadCitizenIntel() {{
  try {{
    const r = await fetch('/api/premium/citizen_intel');
    const posts = await r.json();
    const el = document.getElementById('ci-list');
    if (!posts || !posts.length) {{
      el.innerHTML = '<div style="color:#666;font-size:13px">No recent citizen reports.</div>';
      return;
    }}
    const HI_KW = ['standoff','barricade','swat','shooting','shots','hostage','pursuit','homicide','murder','stabbing','armed','crime scene','avoid the area'];
    el.innerHTML = posts.map(p => {{
      const age = Math.round((Date.now()/1000 - p.ts) / 60);
      const ageStr = age < 60 ? age + 'm ago' : (Math.round(age/60) + 'h ago');
      const kws = (p.keywords||'').split(',').filter(Boolean).slice(0,5);
      const isHi = kws.some(k => HI_KW.includes(k.trim()));
      const kwHtml = kws.map(k =>
        '<span style="background:#2a1a0a;color:#f90;border-radius:3px;padding:1px 6px;font-size:11px;margin-right:3px">' + k.trim() + '</span>'
      ).join('');
      const badge = isHi
        ? '<span style="background:#7f1d1d;color:#fca5a5;border-radius:3px;padding:1px 7px;font-size:10px;font-weight:bold;margin-left:8px">HIGH</span>'
        : '';
      let matchHtml = '';
      if (p.incident_id) {{
        const iAge = p.incident_ts ? Math.round((Date.now()/1000 - p.incident_ts) / 60) : null;
        const iAgeStr = iAge !== null ? (iAge < 60 ? iAge + 'm ago' : Math.round(iAge/60) + 'h ago') : '';
        const loc = p.incident_location || 'Location TBD';
        const itype = p.incident_type || 'INCIDENT';
        const calls = p.call_count || 0;
        matchHtml = '<div style="margin-top:10px;padding-top:10px;border-top:1px solid #252525">'
          + '<div style="background:#0d1f0d;border:1px solid #1a4a1a;border-radius:6px;padding:10px 14px">'
          + '<div style="font-size:10px;color:#4ade80;font-weight:bold;letter-spacing:1.5px;margin-bottom:5px">&#9654; RADIO CROSS-REFERENCE (score: ' + p.match_score + ')</div>'
          + '<div style="font-size:14px;color:#eee;font-weight:bold">' + itype + '</div>'
          + '<div style="font-size:12px;color:#888;margin-top:2px">' + loc + (iAgeStr ? ' &middot; ' + iAgeStr : '') + '</div>'
          + '<div style="font-size:12px;color:#4ade80;margin-top:4px">&#128225; ' + calls + ' radio call' + (calls!==1?'s':'') + ' captured</div>'
          + '</div></div>';
      }}
      const borderStyle = p.incident_id ? 'border-color:#1a3a1a' : '';
      return '<div style="border:1px solid #252525;border-radius:8px;padding:14px;margin-bottom:10px;background:#161616;' + borderStyle + '">'
        + '<div style="margin-bottom:6px">'
        + '<a href="' + p.url + '" target="_blank" style="color:#f90;font-size:14px;font-weight:bold;text-decoration:none;line-height:1.4">' + p.title + '</a>' + badge
        + '</div>'
        + '<div style="font-size:11px;color:#555;margin-bottom:7px">u/' + p.author + ' &middot; r/' + p.subreddit + ' &middot; ' + ageStr + '</div>'
        + '<div style="margin-bottom:4px">' + kwHtml + '</div>'
        + matchHtml
        + '</div>';
    }}).join('');
  }} catch(e) {{
    document.getElementById('ci-list').innerHTML = '<div style="color:#666;font-size:13px">Intel unavailable.</div>';
  }}
}}
loadCitizenIntel();
setInterval(loadCitizenIntel, 300000);
</script>
</body></html>"""
        return dashboard

    # Not premium — show subscribe page
    subscribe = """<!DOCTYPE html>
<html><head><title>Battle Buddy — Subscribe</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box}
  body{font-family:sans-serif;background:#111;color:#eee;max-width:760px;margin:40px auto;padding:20px}
  h1{color:#f90;margin-bottom:4px}
  .tagline{font-size:17px;color:#aaa;margin-bottom:18px}
  .trial-badge{display:inline-block;background:#1a3a1a;color:#4f4;border:1px solid #2d6b2d;border-radius:20px;padding:5px 14px;font-size:13px;font-weight:bold;margin-bottom:22px}
  .toggle{display:flex;gap:0;margin-bottom:24px;border:1px solid #444;border-radius:8px;overflow:hidden;width:fit-content}
  .toggle button{background:transparent;color:#aaa;border:none;padding:8px 20px;cursor:pointer;font-size:14px;font-weight:bold;transition:all .2s}
  .toggle button.active{background:#f90;color:#111}
  .plans{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
  .card{flex:1;min-width:280px;background:#1a1a1a;border:2px solid #333;border-radius:12px;padding:24px;cursor:pointer;transition:border-color .2s}
  .card:hover{border-color:#f90}
  .card.selected{border-color:#f90;background:#1f1a0d}
  .card-label{font-size:11px;font-weight:bold;color:#888;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
  .card-name{font-size:22px;font-weight:bold;color:#fff;margin-bottom:2px}
  .card-price{font-size:36px;font-weight:bold;color:#f90;margin:12px 0 2px}
  .card-price span{font-size:15px;color:#888;font-weight:normal}
  .card-save{font-size:12px;color:#4f4;font-weight:bold;margin-bottom:14px;min-height:18px}
  .features{list-style:none;padding:0;margin:0 0 20px}
  .features li{padding:5px 0;font-size:14px;color:#ccc}
  .features li::before{content:"✓ ";color:#f90;font-weight:bold}
  .features li.no::before{content:"✗ ";color:#555}
  .features li.no{color:#555}
  .form-row{display:flex;gap:12px;margin-bottom:10px;flex-wrap:wrap}
  input{flex:1;min-width:200px;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:10px;font-size:15px}
  input::placeholder{color:#666}
  button.sub-btn{width:100%;background:#f90;color:#111;border:none;padding:14px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:17px;margin-top:8px}
  button.sub-btn:hover{background:#ffa820}
  button.sub-btn:disabled{opacity:.5;cursor:not-allowed}
  .note{color:#666;font-size:12px;margin-top:10px;text-align:center}
  #err{color:#f44;font-size:14px;margin-top:8px;display:none}
  .login-link{text-align:center;margin-top:20px;color:#888;font-size:14px}
  .login-link a{color:#f90;cursor:pointer}
</style></head><body>
<h1>Battle Buddy</h1>
<p class="tagline">Real-time Austin intelligence for people who need to know.</p>
<div class="trial-badge">🎁 7-day free trial — cancel anytime</div>

<div class="toggle">
  <button id="btn-monthly" class="active" onclick="setBilling('monthly')">Monthly</button>
  <button id="btn-annual" onclick="setBilling('annual')">Annual &nbsp;· Save 30%</button>
</div>

<div class="plans">
  <div class="card" id="card-basic" onclick="selectPlan('basic')">
    <div class="card-label">Entry</div>
    <div class="card-name">Basic</div>
    <div class="card-price" id="price-basic">$4 <span>/ month</span></div>
    <div class="card-save" id="save-basic"></div>
    <ul class="features">
      <li>Live incident alerts via Nextcloud Talk</li>
      <li>Commute Monitor with incident corridor alerts</li>
      <li>Intel News Feed in Nextcloud News</li>
      <li>Priority DMs for high-severity events</li>
      <li class="no">ATAK field data package</li>
      <li class="no">SITREP access</li>
    </ul>
  </div>
  <div class="card selected" id="card-premium" onclick="selectPlan('premium')">
    <div class="card-label">Full Access</div>
    <div class="card-name">Premium</div>
    <div class="card-price" id="price-premium">$11 <span>/ month</span></div>
    <div class="card-save" id="save-premium"></div>
    <ul class="features">
      <li>Live incident alerts via Nextcloud Talk</li>
      <li>Commute Monitor with incident corridor alerts</li>
      <li>Intel News Feed in Nextcloud News</li>
      <li>Priority DMs for high-severity events</li>
      <li>ATAK field data package (WinTAK / ATAK / iTAK)</li>
      <li>SITREP access</li>
    </ul>
  </div>
</div>

<div class="form-row">
  <input type="text" id="username" placeholder="Choose a username" autocomplete="off">
  <input type="text" id="display" placeholder="Your name (optional)">
</div>
<button class="sub-btn" id="sub-btn" onclick="subscribe()">Start Free Trial — Premium Monthly →</button>
<div id="err"></div>
<p class="note">7-day free trial. Card required but not charged until trial ends.</p>

<div class="login-link">Already a member? <a onclick="showLogin()">Sign in</a></div>

<div id="login-box" style="display:none;margin-top:20px">
  <input type="text" id="l-user" placeholder="Username" style="display:block;width:100%;margin-bottom:10px;box-sizing:border-box">
  <input type="password" id="l-pass" placeholder="Password" style="display:block;width:100%;margin-bottom:10px;box-sizing:border-box">
  <button onclick="login()">Sign In</button>
  <div id="login-err" style="color:#f44;font-size:14px;margin-top:8px;display:none"></div>
</div>

<script>
function showLogin() {
  document.getElementById('login-box').style.display = 'block';
}
let billing = 'monthly';
let tier = 'premium';
const prices = {
  basic:   {monthly:{plan:'basic_monthly',   label:'$4',  per:'/ month',save:''},
             annual: {plan:'basic_annual',    label:'$36', per:'/ year', save:'Save $12/yr'}},
  premium: {monthly:{plan:'premium_monthly', label:'$11', per:'/ month',save:''},
             annual: {plan:'premium_annual',  label:'$99', per:'/ year', save:'Save $33/yr'}}
};
function setBilling(b){
  billing=b;
  document.getElementById('btn-monthly').classList.toggle('active',b==='monthly');
  document.getElementById('btn-annual').classList.toggle('active',b==='annual');
  updateUI();
}
function selectPlan(t){
  tier=t;
  document.getElementById('card-basic').classList.toggle('selected',t==='basic');
  document.getElementById('card-premium').classList.toggle('selected',t==='premium');
  updateUI();
}
function updateUI(){
  ['basic','premium'].forEach(t=>{
    const p=prices[t][billing];
    document.getElementById('price-'+t).innerHTML=p.label+' <span>'+p.per+'</span>';
    document.getElementById('save-'+t).textContent=p.save;
  });
  const p=prices[tier][billing];
  const tname=tier.charAt(0).toUpperCase()+tier.slice(1);
  const bname=billing.charAt(0).toUpperCase()+billing.slice(1);
  document.getElementById('sub-btn').textContent='Start Free Trial — '+tname+' '+bname+' →';
}
// Pre-select tier + billing from ?plan= URL param (used by libertas.mobi deep links)
(function(){
  var params = new URLSearchParams(window.location.search);
  var p = params.get('plan');
  var valid = ['basic_monthly','basic_annual','premium_monthly','premium_annual'];
  if (p && valid.indexOf(p) !== -1) {
    var parts = p.split('_');
    selectPlan(parts[0]);
    setBilling(parts[1]);
  }
})();
function showLogin(){document.getElementById('login-box').style.display='block';}
async function subscribe() {
  const username = document.getElementById('username').value.trim().toLowerCase();
  const display  = document.getElementById('display').value.trim();
  const err = document.getElementById('err');
  err.style.display = 'none';
  if (!username) { err.textContent='Please enter a username.'; err.style.display='block'; return; }
  const btn = document.getElementById('sub-btn');
  btn.disabled=true; btn.textContent='Redirecting to Stripe...';
  const plan = prices[tier][billing].plan;
  try {
    const r = await fetch('/api/stripe/create_checkout', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, display_name: display||username, plan})
    });
    const d = await r.json();
    if (d.checkout_url) { window.location.href=d.checkout_url; }
    else { err.textContent=d.error||'Checkout failed. Try again.'; err.style.display='block'; btn.disabled=false; updateUI(); }
  } catch(e) { err.textContent='Network error. Try again.'; err.style.display='block'; btn.disabled=false; updateUI(); }
}
async function login() {
  const username = document.getElementById('l-user').value.trim().toLowerCase();
  const password = document.getElementById('l-pass').value;
  const err = document.getElementById('login-err');
  err.style.display = 'none';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password})
    });
    if (r.ok) {
      window.location.href = '/premium/';
    } else {
      const d = await r.json();
      err.textContent = d.error || 'Login failed.';
      err.style.display = 'block';
    }
  } catch(e) {
    err.textContent = 'Network error.';
    err.style.display = 'block';
  }
}
</script>
</body></html>"""
    return subscribe



# ---------------------------------------------------------------------------
# OpenClaw Control UI — Nextcloud admin auth gate (used by nginx auth_request)
# ---------------------------------------------------------------------------

@app.route("/auth/nc_admin")
def auth_nc_admin():
    """nginx auth_request endpoint. Returns 200 if caller is a Nextcloud admin, 401 otherwise."""
    from flask import make_response
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("basic "):
        resp = make_response("", 401)
        resp.headers["WWW-Authenticate"] = "Basic realm=\"OpenClaw\""
        return resp
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="replace")
        username, _, password = decoded.partition(":")
    except Exception:
        return make_response("", 401)
    if not username or not password:
        return make_response("", 401)
    if not _nc_validate_user(username, password):
        resp = make_response("", 401)
        resp.headers["WWW-Authenticate"] = "Basic realm=\"OpenClaw\""
        return resp
    if not _is_admin(username):
        return make_response("", 403)
    return make_response("", 200)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",        type=int, default=9001)
    ap.add_argument("--enable-hold", action="store_true",
                    help="Enable OP25 hold/skip commands to Pi 1 (test carefully)")
    args = ap.parse_args()

    if args.enable_hold:
        HOLD_ENABLED = True
        print("[hold] OP25 hold/skip ENABLED — will send commands to Pi 1", flush=True)
    else:
        print("[hold] OP25 hold/skip DISABLED (run with --enable-hold to activate)", flush=True)

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    load_talkgroups()
    init_db()
    _geocode_load_db()
    _load_active_incidents_from_db()
    _atak_resync_on_startup()

    print(f"[brain] Battle Buddy v2.0 starting on port {args.port}", flush=True)
    print(f"[brain] Transcription: faster-whisper large-v3-turbo INT8 (local, offline-ready)", flush=True)
    print(f"[brain] DB: {DB_PATH}", flush=True)

    threading.Thread(target=_get_fw_model,            daemon=True).start()  # warm model at startup
    if FTS_ENABLED:
        _fts_connect()
        threading.Thread(target=_fts_keepalive_thread, daemon=True).start()
        threading.Thread(target=_atak_resync_thread,   daemon=True).start()
    threading.Thread(target=incident_cleanup_thread,  daemon=True).start()
    threading.Thread(target=hold_watchdog_thread,     daemon=True).start()
    threading.Thread(target=pi_watchdog_thread,       daemon=True).start()
    threading.Thread(target=afd_open_data_thread,     daemon=True).start()
    threading.Thread(target=traffic_open_data_thread, daemon=True).start()
    threading.Thread(target=atxfloods_thread,         daemon=True).start()
    threading.Thread(target=austin_events_thread,    daemon=True).start()
    threading.Thread(target=apd_news_thread,          daemon=True).start()
    threading.Thread(target=reddit_intel_thread,      daemon=True).start()
    threading.Thread(target=adsb_air_asset_thread,    daemon=True).start()
    queue_manager.start_queue(process_call_audio)

    app.run(host="0.0.0.0", port=args.port, threaded=True)
