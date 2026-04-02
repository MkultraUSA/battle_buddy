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
import base64
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave
from datetime import datetime

# Bypass SSL cert verification for all urllib calls (Nextcloud snap cert not in system store)
_ssl_ctx = ssl._create_unverified_context()
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_ctx))
)

from flask import Flask, jsonify, render_template_string, request, send_from_directory

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH       = "/opt/battlebuddy/calls.db"
TGID_TSV      = "/opt/battlebuddy/gatrrs-tags.tsv"
PI1_OP25_URL  = "http://192.168.1.158:8080/"

# Groq — LLM incident analysis (llama-3.3-70b), called directly from Contabo
# Audio transcription is LOCAL (faster-whisper), works offline in the field
GROQ_API_KEY        = "GROQ_API_KEY_REMOVED"
GROQ_MODEL          = "llama-3.3-70b-versatile"
GROQ_ENABLED        = bool(GROQ_API_KEY)
GROQ_API_BASE       = "https://api.groq.com/openai/v1"

# Nextcloud Talk — post each transcript to the BattleBuddy room
TALK_BASE    = "https://kevcloud.ddns.net/ocs/v2.php/apps/spreed/api/v1"
TALK_USER    = "battlebuddy"
TALK_PASS    = "TALK_PASS_REMOVED"
TALK_ENABLED = True

# Mailgun email alerts
MAILGUN_API_KEY  = "MAILGUN_API_KEY_REMOVED"
MAILGUN_DOMAIN   = "MAILGUN_DOMAIN_REMOVED"
MAILGUN_FROM     = f"Battle Buddy <mailgun@{MAILGUN_DOMAIN}>"
ALERT_EMAIL      = "k.watkins@me.com"

# FreeTAKServer ATAK integration
FTS_HOST      = "192.168.1.158"
FTS_REST_PORT = 19023
FTS_COT_PORT  = 8087
FTS_TOKEN     = "token"
FTS_ENABLED   = True

# Deck integration
DECK_BASE     = "https://kevcloud.ddns.net/index.php/apps/deck/api/v1.0"
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
TALK_ROOM    = "ao24p89o"   # original / catch-all
TALK_ROOMS   = {
    "incidents": "65pksvdw",  # 🔴/🟡 priority alerts only
    "apd":       "pegcoq3c",  # APD traffic
    "fire-ems":  "78ncp9se",  # AFD, TCFD, TCEMS
    "general":   "qd5bk9pm",  # everything else
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
TALK_BOT_SECRET = "TALK_BOT_SECRET_REMOVED"

# Hold/skip commands to Pi 1 OP25 — OFF until behavior is verified.
# Run with --enable-hold to turn on.
HOLD_ENABLED = False

# Release hold after this many minutes of silence on the held channel.
HOLD_RELEASE_MINUTES = 5

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


def insert_call(ts, tgid, tag, category, node, duration, transcript, lat, lon, location) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO calls (ts,tgid,tag,category,node,duration,transcript,lat,lon,location) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, tgid, tag, category, node, duration, transcript, lat, lon, location)
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
]


_geocode_cache: dict[str, tuple[float, float] | None] = {}
_geocode_lock  = threading.Lock()

# Rough bounding box for Austin/Travis County metro — reject geocodes outside this
_GEO_BOUNDS = (29.85, -98.25, 30.70, -97.25)  # (min_lat, min_lon, max_lat, max_lon)

# Regex patterns that suggest a real street address in the transcript
_ADDR_RE = re.compile(
    r'\b(\d{3,5})\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b'   # e.g. "2525 West Anderson Lane"
    r'|'
    r'\b(\d{1,2}(?:st|nd|rd|th))\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'  # e.g. "15th and West"
    r'|'
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'  # e.g. "Porter and Vargas"
)


def _geocode_address(address: str) -> tuple[float, float] | None:
    """Geocode an address string in Austin, TX. Cached. Returns (lat, lon) or None."""
    key = address.lower().strip()
    with _geocode_lock:
        if key in _geocode_cache:
            return _geocode_cache[key]
    try:
        from geopy.geocoders import Nominatim
        from geopy.exc import GeocoderTimedOut
        geo = Nominatim(user_agent="battlebuddy/1.0")
        full = f"{address}, Austin, TX"
        result = geo.geocode(full, timeout=4)
        if result:
            lat, lon = result.latitude, result.longitude
            min_lat, min_lon, max_lat, max_lon = _GEO_BOUNDS
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                with _geocode_lock:
                    _geocode_cache[key] = (lat, lon)
                print(f"[geocode] '{address}' → {lat:.4f},{lon:.4f}", flush=True)
                return lat, lon
        with _geocode_lock:
            _geocode_cache[key] = None
    except Exception as exc:
        print(f"[geocode] error for '{address}': {exc}", flush=True)
        with _geocode_lock:
            _geocode_cache[key] = None
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
    for m in _ADDR_RE.finditer(text):
        candidate = m.group(0).strip()
        result = _geocode_address(candidate)
        if result:
            return result[0], result[1], candidate
    return None, None, None


# ---------------------------------------------------------------------------
# Transcription — faster-whisper base.en INT8 (local, offline-capable)
# 4-8x faster than openai-whisper, ~200MB RAM, works with no internet
# ---------------------------------------------------------------------------

from faster_whisper import WhisperModel as _FasterWhisperModel

_fw_model      = None
_fw_model_lock = threading.Lock()

def _get_fw_model() -> _FasterWhisperModel:
    global _fw_model
    if _fw_model is None:
        print("[whisper] loading faster-whisper base.en int8...", flush=True)
        _fw_model = _FasterWhisperModel("base.en", device="cpu", compute_type="int8",
                                        cpu_threads=2, num_workers=1)
        print("[whisper] model ready", flush=True)
    return _fw_model


def transcribe(wav_bytes: bytes) -> str:
    """Transcribe audio locally with faster-whisper. No external API needed."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp = f.name
    try:
        model = _get_fw_model()
        segments, _ = model.transcribe(tmp, language="en", beam_size=1,
                                       vad_filter=True)
        return " ".join(s.text for s in segments).strip()
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
  "STRUCTURE FIRE", "HAZMAT", "HOSTAGE/BARRICADE", "CRASH/COLLISION",
  "FIRE DISPATCH", "TRANSIT INCIDENT", "AIRPORT EMERGENCY",
  "MULTI-AGENCY RESPONSE", "APD SURGE", "AIR ASSET ACTIVE", "DPS CAPITOL ACTIVATION"

Priority rules:
  HIGH — active life threat: officer down, shooting, structure fire, mass casualty, hostage/barricade, aircraft emergency
  MED  — significant incident: crash, hazmat, fire dispatch, multi-agency, surge, transit, airport alert
  NONE — routine: traffic stop, medical assist, disturbance, welfare check, normal patrol, minor fender bender

should_hold: true only if the event is actively unfolding and worth continuous monitoring.

Be conservative. Most radio traffic is routine. Only flag genuine emergencies."""


_groq_call_times: list = []
_GROQ_RATE_LIMIT = 10   # max calls per 60 seconds (free tier is 30, stay well under)

def groq_analyze(call: dict, recent_calls: list) -> dict | None:
    """Call Groq LLM to analyze a call. Returns parsed JSON dict or None on failure."""
    if not GROQ_ENABLED:
        return None
    transcript = call.get("transcript") or ""
    if not transcript or len(transcript) < 5:
        return None
    # Rate limit — skip if we've already sent too many calls in the last 60s
    now = time.time()
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
        return result
    except Exception as exc:
        print(f"[groq] error: {exc}", flush=True)
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
    (" shot ",         "SHOOTING"),
    ("stabbing",       "STABBING"),
    (" stab",          "STABBING"),
    ("aircraft",       "AIRCRAFT EMERGENCY"),
    ("mass casualty",  "MASS CASUALTY"),
    ("mci",            "MASS CASUALTY"),
    ("structure fire", "STRUCTURE FIRE"),
    ("working fire",   "STRUCTURE FIRE"),
    ("fully involved", "STRUCTURE FIRE"),
    ("hazmat",         "HAZMAT"),
    ("chemical spill", "HAZMAT"),
    ("hostage",        "HOSTAGE/BARRICADE"),
    ("barricade",      "HOSTAGE/BARRICADE"),
    ("crash",          "CRASH/COLLISION"),
    ("collision",      "CRASH/COLLISION"),
    ("rollover",       "CRASH/COLLISION"),
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


# In-memory active incident state: {db_id: {itype, ts_updated, agencies, tgids, lat, lon, escalation_stage}}
_active_incidents: dict[int, dict] = {}
_atak_markers: dict[int, str] = {}  # incident_id → FTS uid, for deletion on clear
_incident_lock = threading.Lock()


def analyze_for_incident(call: dict):
    """Run after each call is stored. Detect and record incidents."""
    tgid  = call.get("tgid", 0)
    cat   = call.get("category", "Unknown")
    text  = (call.get("transcript") or "").lower()
    ts    = call.get("ts", time.time())

    flags = []   # list of (priority, itype, description)

    # --- Groq LLM result (primary signal — overrides keyword rules if present) ---
    groq = call.get("groq") or {}
    groq_itype = groq.get("incident_type")
    groq_pri   = groq.get("priority", "NONE")
    if groq_itype and groq_itype not in (None, "ROUTINE"):
        pri_score = {"HIGH": 5, "MED": 15, "NONE": 30}.get(groq_pri, 20)
        flags.append((pri_score, groq_itype,
                      groq.get("description") or f"{groq_itype} detected by LLM"))

    # --- Rule 1: Transit channels active (APD Metro 1-10) ---
    if tgid in TRANSIT_TGIDS:
        flags.append((10, "TRANSIT INCIDENT",
                      f"APD transit channel active: {call.get('tag', tgid)} — "
                      f"Cap Metro bus/rail event likely"))

    # --- Rule 2: Airport alert ---
    if tgid in ABIA_ALERT_TGIDS:
        flags.append((5, "AIRPORT EMERGENCY",
                      "ABIA Alert channel activated"))

    # --- Rule 3: Keyword in transcript ---
    # Skip ABIA operational channels — airport security/ops uses words like
    # "barricade", "hostage", "weapons" in routine daily context.
    if tgid not in ABIA_OPS_TGIDS:
        for kw, itype in INCIDENT_KEYWORDS:
            if kw in text:
                flags.append((20, itype,
                              f"'{kw}' detected on {call.get('tag', tgid)}"))
                break

    # --- Rule 4: Locution dispatch ---
    # Skip pure EMS calls — cardiac arrests, medical assists, seizures, etc.
    # are routine EMS responses, not newsworthy fire incidents.
    EMS_ONLY_KEYWORDS = (
        "cardiac arrest", "medical assist", "seizure", "sick person",
        "respiratory", "difficulty breathing", "chest pain", "diabetic",
        "unconscious", "fall victim", "overdose", "stroke",
    )
    FIRE_KEYWORDS = (
        "fire", "smoke", "explosion", "hazmat", "brush", "structure",
        "vehicle fire", "trash fire", "alarm", "rescue",
    )
    if tgid in LOCUTION_TGIDS and len(text) > 8:
        tl = text.lower()
        is_ems_only = (
            any(kw in tl for kw in EMS_ONLY_KEYWORDS)
            and not any(kw in tl for kw in FIRE_KEYWORDS)
        )
        if not is_ems_only:
            flags.append((15, "FIRE DISPATCH",
                          f"Locution active ({call.get('tag', tgid)}): {text[:80]}"))

    # --- Rule 5: Multi-agency convergence ---
    # Only fires when another rule has already detected something real.
    # ABIA is excluded because it runs 24/7 and is rarely co-responding with
    # ground units on the same incident — including it caused constant false positives.
    # Requires 3+ ground agencies to filter out normal paired APD+AFD dispatches.
    if flags:
        window = calls_since(ts - MULTIAGENCY_WINDOW_MIN * 60)
        active_cats = {c["category"] for c in window
                       if c["category"] not in (None, "Unknown", "TXDOT", "Interop", "ABIA")}
        ps_cats = active_cats & {"APD", "AFD", "TCEMS", "TCSO", "TCFD"}
        if len(ps_cats) >= 3:
            flags.append((30, "MULTI-AGENCY RESPONSE",
                          f"Agencies active in last {MULTIAGENCY_WINDOW_MIN}m: "
                          f"{', '.join(sorted(ps_cats))}"))

    # --- Rule 6a: Air asset active ---
    # A police/fire helicopter in the air is one of the strongest early
    # indicators of a newsworthy event — pursuit, active shooter perimeter,
    # search, crowd overwatch, or dignitary movement. Flag it immediately.
    air_context = detect_air_asset(tgid, call.get("transcript") or "", cat)
    if air_context:
        flags.append((8, "AIR ASSET ACTIVE",
                      f"{cat} air asset aloft — likely: {air_context} "
                      f"({call.get('tag', tgid)})"))

    # --- Rule 6b: DPS Capitol activation ---
    # DPS protects the Capitol complex with assets most agencies don't have.
    # Any DPS talkgroup activity OR cross-agency DPS mention near downtown
    # signals a potential dignitary, protest, or Capitol security event.
    if cat == "DPS" or mentions_dps(text):
        assets = detect_dps_assets(call.get("transcript") or "")
        asset_note = f" — assets: {', '.join(assets)}" if assets else ""
        capitol = is_capitol_area(call.get("transcript") or "", call.get("location"))
        if capitol or assets:
            flags.append((25, "DPS CAPITOL ACTIVATION",
                          f"DPS activity detected{asset_note}"
                          + (" near Capitol complex" if capitol else "")))

    # --- Rule 7: APD surge (APD-only events like the bus stabbing) ---
    apd_calls = [c for c in calls_since(ts - APD_SURGE_WINDOW_MIN * 60)
                 if c["category"] == "APD"]
    if len(apd_calls) >= APD_SURGE_THRESHOLD:
        # Exclude pure dispatch/metro-only surges from noise
        ops_calls = [c for c in apd_calls
                     if c["tgid"] not in (967,)]  # 967 = APD Dispatch (high volume normal)
        if len(ops_calls) >= APD_SURGE_THRESHOLD:
            flags.append((35, "APD SURGE",
                          f"{len(ops_calls)} APD operational calls in "
                          f"{APD_SURGE_WINDOW_MIN} min — possible major incident"))

    # --- Escalation check on ALL calls (even routine ones) ---
    # A welfare check that turns into a SWAT standoff must be linked.
    call_id = call.get("id")
    stage = _detect_escalation_stage(call.get("transcript") or "") or groq.get("escalation_stage")
    loc_match = _find_incident_by_location(call.get("lat"), call.get("lon"), ts)
    if loc_match is not None:
        # Link this call to the nearby incident
        if call_id:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT OR IGNORE INTO incident_calls (incident_id, call_id) VALUES (?,?)",
                         (loc_match, call_id))
            conn.commit()
            conn.close()
        if stage:
            _record_escalation(loc_match, stage,
                               f"{call.get('tag','?')}: {(call.get('transcript') or '')[:80]}", ts)

    if not flags:
        if HOLD_ENABLED and loc_match:
            _consider_hold(tgid, _active_incidents[loc_match]["itype"],
                           escalation_stage=stage or _active_incidents[loc_match].get("escalation_stage"))
        return

    # Use lowest priority number (highest urgency)
    flags.sort(key=lambda x: x[0])
    _, itype, desc = flags[0]

    with _incident_lock:
        # Match by location first, then fall back to same itype within window
        matched_id = loc_match
        if matched_id is None:
            for inc_id, inc in _active_incidents.items():
                if inc["itype"] == itype and (ts - inc["ts_updated"]) < MULTIAGENCY_WINDOW_MIN * 60:
                    matched_id = inc_id
                    break

        if matched_id is not None:
            _update_incident(matched_id, call, ts, desc)
        else:
            _create_incident(itype, desc, call, ts)
            matched_id = max(_active_incidents.keys())  # just created

    if stage:
        _record_escalation(matched_id, stage,
                           f"{call.get('tag','?')}: {(call.get('transcript') or '')[:80]}", ts)
    if call_id:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO incident_calls (incident_id, call_id) VALUES (?,?)",
                     (matched_id, call_id))
        conn.commit()
        conn.close()

    if HOLD_ENABLED:
        _consider_hold(tgid, itype, escalation_stage=stage or groq.get("escalation_stage"))


def _atak_send_cot(xml: str):
    """Send a raw CoT XML string to FreeTAKServer port 8087."""
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((FTS_HOST, FTS_COT_PORT))
    s.sendall(xml.encode("utf-8"))
    s.close()


def _atak_post_marker(incident_id: int, lat: float, lon: float, itype: str, location: str | None):
    """Post a CoT marker directly to FTS port 8087 for a new incident."""
    if not FTS_ENABLED:
        return
    uid   = f"BB-INC-{incident_id}"
    label = f"{itype} @ {location}" if location else itype
    label = label[:60]
    now   = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.0Z")
    stale = datetime.utcnow().replace(hour=23, minute=59).strftime("%Y-%m-%dT%H:%M:%S.0Z")
    xml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<event version='2.0' uid='{uid}' type='a-h-G' "
        f"time='{now}' start='{now}' stale='{stale}' how='m-g'>"
        f"<point lat='{lat}' lon='{lon}' hae='9999999.0' ce='9999999.0' le='9999999.0'/>"
        f"<detail>"
        f"<contact callsign='{label}'/>"
        f"<remarks>{label}</remarks>"
        f"</detail></event>"
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
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.0Z")
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
    if call.get("lat") is not None and call.get("lon") is not None:
        threading.Thread(target=_atak_post_marker,
                         args=(inc_id, call["lat"], call["lon"], itype, call.get("location")),
                         daemon=True).start()


def _update_incident(inc_id: int, call: dict, ts: float, desc: str):
    inc = _active_incidents[inc_id]
    inc["ts_updated"] = ts
    inc["agencies"].add(call.get("category"))
    inc["tgids"].add(call.get("tgid"))
    agencies = json.dumps(sorted(x for x in inc["agencies"] if x))
    tgids    = json.dumps(sorted(x for x in inc["tgids"]    if x is not None))
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE incidents SET ts_updated=?, agencies=?, tgids=?, description=? WHERE id=?",
        (ts, agencies, tgids, desc, inc_id)
    )
    conn.commit()
    conn.close()
    print(f"[incident] UPD  {inc['itype']} (id={inc_id}): {desc}", flush=True)


def incident_cleanup_thread():
    """Mark incidents as cleared when they've had no updates for HOLD_RELEASE_MINUTES."""
    while True:
        time.sleep(60)
        cutoff = time.time() - HOLD_RELEASE_MINUTES * 60
        with _incident_lock:
            to_clear = [iid for iid, inc in _active_incidents.items()
                        if inc["ts_updated"] < cutoff]
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
PI1_OP25_CMD_URL       = "http://192.168.1.158:8080/"  # OP25 command endpoint
PI1_SSH_HOST           = "192.168.1.158"
PI1_SSH_USER           = "pi"
PI1_SSH_KEY            = "/root/.ssh/id_ed25519"

_pi_was_down         = False
_op25_was_dead       = False
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

        if not pi_up and not _pi_was_down:
            _pi_was_down = True
            _pi_watchdog_alert(
                f"⚠️ BATTLE BUDDY ALERT: Pi 1 (OP25) is UNREACHABLE at {PI1_OP25_URL} — radio feed is down."
            )
        elif pi_up and _pi_was_down:
            _pi_was_down = False
            _pi_watchdog_alert("✅ Pi 1 (OP25) is back online — radio feed restored.")

        # --- Check 2: OP25 actively decoding (trunk_update) ---
        if pi_up:
            op25_active = _poll_op25_trunk()
            if not op25_active and not _op25_was_dead:
                _op25_was_dead = True
                _pi_watchdog_alert(
                    "⚠️ BATTLE BUDDY ALERT: Pi is up but OP25 is NOT returning trunk data — decoder may have crashed or lost the control channel."
                )
            elif op25_active and _op25_was_dead:
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

                # If unmatched and has coords, post an ATAK marker too
                if matched_id is None and lat is not None and lon is not None:
                    # Use a negative sentinel incident_id to avoid colliding with real ones
                    afd_marker_id = hash(rid) % 100000 * -1
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(afd_marker_id, lat, lon, itype, address),
                        daemon=True
                    ).start()


# ---------------------------------------------------------------------------
# Sitrep generator
# ---------------------------------------------------------------------------

def build_sitrep(minutes=60) -> str:
    calls     = calls_for_sitrep(minutes)
    incidents = [i for i in active_incidents() if not i.get("is_test")]

    lines = [
        f"SITUATION REPORT — last {minutes} min — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
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
            "structure fire", "mass casualty", "hostage", "barricade", "10-99"]
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

    # Skip routine calls entirely — Talk is for actionable intelligence only.
    # Priority is determined below; we pre-check here using Groq + keywords
    # to avoid building the full message for calls we'll discard.
    groq_pri_early = (call.get("groq") or {}).get("priority", "NONE")
    has_high_kw = any(k in text_lower for k in _HIGH_KW)
    has_med_kw  = any(k in text_lower for k in _MED_KW)
    with _incident_lock:
        linked_to_incident = any(
            tgid in inc.get("tgids", set()) or cat in inc.get("agencies", set())
            for inc in _active_incidents.values()
        )
    if groq_pri_early == "NONE" and not has_high_kw and not has_med_kw and not linked_to_incident:
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

    # --- Priority flag (Groq takes precedence over keyword rules) ---
    groq_pri = (call.get("groq") or {}).get("priority", "NONE")
    if groq_pri == "HIGH" or matched_itype in _HIGH_PRIORITY or any(k in text_lower for k in _HIGH_KW):
        priority = "🔴"
    elif groq_pri == "MED" or matched_itype in _MED_PRIORITY or any(k in text_lower for k in _MED_KW):
        priority = "🟡"
    else:
        priority = "⚪"

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

    def process():
        global _last_call_ts
        _last_call_ts = time.time()
        transcript = transcribe(wav_bytes)
        lat, lon, location = extract_location(transcript)
        if lat is None:
            lat, lon = def_lat, def_lon
            location = None
        print(f"[recv] {tag}: {transcript[:80]}", flush=True)
        call_id = insert_call(ts, tgid, tag, category, node, duration, transcript, lat, lon, location)
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

    now = datetime.now().strftime("%-I:%M %p")
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
            f"Transcription: faster-whisper base.en INT8 (local)"
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

poll();
pollIncidents();
setInterval(poll, 5000);
setInterval(pollIncidents, 15000);
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
      <a href="/public" class="btn-primary">View Live Map</a>
      <a href="/public/feed" class="btn-secondary">Live Feed</a>
      <a href="/public/about" class="btn-secondary">Learn More</a>
    </div>
    <div class="stats-row" id="stats">
      <div class="stat"><div class="stat-num" id="s-calls">—</div><div class="stat-label">Calls Monitored</div></div>
      <div class="stat"><div class="stat-num" id="s-incidents">—</div><div class="stat-label">Incidents Detected</div></div>
      <div class="stat"><div class="stat-num" id="s-agencies">6</div><div class="stat-label">Agencies Monitored</div></div>
    </div>
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
  </div>
</section>

<section class="final-cta">
  <h2>Get Subscriber Access</h2>
  <p>Built for journalists and news desks covering the Austin metro area.</p>
  <a href="mailto:admin@libertas.mobi" class="btn-primary">Request Access</a>
</section>

<footer>
  &copy; 2026 Battle Buddy &nbsp;·&nbsp; Austin Metro Public Safety Intelligence &nbsp;·&nbsp;
  <a href="/public" style="color:#3b82f6;text-decoration:none">Live Map</a> &nbsp;·&nbsp;
  <a href="/public/feed" style="color:#3b82f6;text-decoration:none">Feed</a> &nbsp;·&nbsp;
  <a href="/public/about" style="color:#3b82f6;text-decoration:none">About</a>
</footer>

<script>
async function loadStats() {
  try {
    const [callsR, incR] = await Promise.all([fetch('/api/calls'), fetch('/api/incidents')]);
    const calls = await callsR.json();
    const incidents = await incR.json();
    document.getElementById('s-calls').textContent = calls.length.toLocaleString();
    document.getElementById('s-incidents').textContent = incidents.length.toLocaleString();
  } catch(e) {}
}
loadStats();
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
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
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

async function loadHeatmap() {
  const resp = await fetch('/api/calls');
  const calls = await resp.json();
  const pts = calls.filter(c => c.lat && c.lon && AUSTIN_BOUNDS.contains([c.lat, c.lon])).map(c => [c.lat, c.lon, 0.6]);
  if (heatLayer) map.removeLayer(heatLayer);
  heatLayer = L.heatLayer(pts, {radius:22, blur:18, maxZoom:13,
    gradient:{0.2:'#1e3a5f', 0.5:'#3b82f6', 0.8:'#f97316', 1.0:'#ef4444'}
  }).addTo(map);
  document.getElementById('s-calls').textContent = calls.length;
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
  document.getElementById('s-incidents').textContent = realAll.length;
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

  // Add incident markers
  all.filter(i => i.lat && i.lon && AUSTIN_BOUNDS.contains([i.lat, i.lon])).forEach(inc => {
    // Jitter approximate locations so stacked markers spread out (~500m radius)
    if (!inc.location) {
      inc.lat += (Math.random() - 0.5) * 0.009;
      inc.lon += (Math.random() - 0.5) * 0.009;
    }
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

loadHeatmap();
loadIncidents();
setInterval(loadHeatmap, 15000);
setInterval(loadIncidents, 10000);
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
    <a href="/public/feed" class="active">Live Feed</a>
    <a href="/public/about">About</a>
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
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — About</title>
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
#content { max-width: 720px; margin: 0 auto; padding: 48px 16px; }
h1 { font-size: 2rem; color: #f8fafc; margin-bottom: 8px; }
.sub { color: #64748b; margin-bottom: 40px; font-size: 1rem; }
h2 { font-size: 1rem; color: #3b82f6; text-transform: uppercase; letter-spacing: 2px; margin: 32px 0 12px; }
p { color: #94a3b8; line-height: 1.7; margin-bottom: 12px; font-size: 0.95rem; }
.feature-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 16px 0; }
.feature { background: #0f1729; border: 1px solid #1e3a5f; border-radius: 8px; padding: 16px; }
.feature .icon { font-size: 1.4rem; margin-bottom: 8px; }
.feature h3 { font-size: 0.85rem; color: #f8fafc; margin-bottom: 4px; }
.feature p { font-size: 0.78rem; margin: 0; }
.cta { margin-top: 40px; background: linear-gradient(135deg,#1e3a5f,#0f1729); border: 1px solid #3b82f6; border-radius: 10px; padding: 28px; text-align: center; }
.cta h2 { color: #3b82f6; margin: 0 0 8px; }
.cta p { color: #94a3b8; margin-bottom: 20px; font-size: 0.9rem; }
.cta a { display: inline-block; background: #3b82f6; color: white; padding: 10px 28px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem; }
.cta a:hover { background: #2563eb; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about" class="active">About</a>
  </nav>
</div>
<div id="content">
  <h1>Austin's Public Safety Intelligence Platform</h1>
  <p class="sub">Real-time radio monitoring, AI transcription, and incident detection for the Austin metro area.</p>

  <h2>What Is Battle Buddy?</h2>
  <p>Battle Buddy monitors Austin-area public safety radio traffic around the clock. Every transmission from APD, AFD, Travis County EMS, DPS, UT Police, and other agencies is captured, transcribed by AI, and analyzed for newsworthy events — automatically, in real time.</p>
  <p>When shots are fired, a structure catches fire, a helicopter goes up, or DPS activates at the Capitol, Battle Buddy knows within seconds — before any news article exists, before a tweet is posted, before a camera crew arrives on scene.</p>

  <h2>How It Works</h2>
  <div class="feature-grid">
    <div class="feature"><div class="icon">📡</div><h3>P25 Radio Monitoring</h3><p>Software-defined radio captures the Austin P25 trunked radio system across all public safety agencies simultaneously.</p></div>
    <div class="feature"><div class="icon">🤖</div><h3>AI Transcription</h3><p>OpenAI Whisper transcribes every transmission in near-real-time, converting radio audio to searchable text.</p></div>
    <div class="feature"><div class="icon">🔍</div><h3>Incident Detection</h3><p>Intelligent analysis detects and classifies incidents — shootings, fires, pursuits, SWAT activations, air assets, and more.</p></div>
    <div class="feature"><div class="icon">📈</div><h3>Escalation Tracking</h3><p>Incidents are tracked as they evolve — from the first welfare check to a full SWAT response, the complete chain is documented.</p></div>
    <div class="feature"><div class="icon">🗺️</div><h3>Live Map</h3><p>All radio activity and incidents are plotted geographically in real time, showing where things are happening across the metro.</p></div>
    <div class="feature"><div class="icon">⚡</div><h3>Instant Alerts</h3><p>Subscribers receive direct alerts the moment a critical incident is detected — before any public notification.</p></div>
  </div>

  <h2>Who Is This For?</h2>
  <p>Battle Buddy is built for journalists, news desks, and media organizations covering the Austin metro area. In a city this size with this many agencies on the air, no human can monitor the scanner effectively. Battle Buddy does it automatically and delivers what matters.</p>

  <div class="cta">
    <h2>Get Access</h2>
    <p>Battle Buddy subscriber access includes real-time alerts, full incident history, and intelligence tools built for working journalists.</p>
    <a href="mailto:admin@libertas.mobi">Request Access</a>
  </div>
</div>
</body>
</html>
"""


@app.route("/splash")
def public_splash():
    return PUBLIC_SPLASH_HTML

@app.route("/public")
def public_map():
    return PUBLIC_MAP_HTML

@app.route("/public/feed")
def public_feed():
    return PUBLIC_FEED_HTML

@app.route("/public/about")
def public_about():
    return PUBLIC_ABOUT_HTML


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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

    print(f"[brain] Battle Buddy v2.0 starting on port {args.port}", flush=True)
    print(f"[brain] Transcription: faster-whisper base.en INT8 (local, offline-ready)", flush=True)
    print(f"[brain] DB: {DB_PATH}", flush=True)

    threading.Thread(target=_get_fw_model,            daemon=True).start()  # warm model at startup
    threading.Thread(target=incident_cleanup_thread,  daemon=True).start()
    threading.Thread(target=hold_watchdog_thread,     daemon=True).start()
    threading.Thread(target=pi_watchdog_thread,       daemon=True).start()
    threading.Thread(target=afd_open_data_thread,     daemon=True).start()

    app.run(host="0.0.0.0", port=args.port, threaded=True)
