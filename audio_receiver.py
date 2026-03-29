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
import tempfile
import threading
import time
import urllib.request
import wave
from datetime import datetime

import whisper
from flask import Flask, jsonify, render_template_string, request, send_from_directory

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH       = "/opt/battlebuddy/calls.db"
TGID_TSV      = "/opt/battlebuddy/gatrrs-tags.tsv"
WHISPER_MODEL = "base"
PI1_OP25_URL  = "http://192.168.1.103:8080/"

# Nextcloud Talk — post each transcript to the BattleBuddy room
TALK_BASE    = "https://kevcloud.ddns.net/ocs/v2.php/apps/spreed/api/v1"
TALK_USER    = "battlebuddy"
TALK_PASS    = "TALK_PASS_REMOVED"
TALK_ENABLED = True

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

# Airport emergency
ABIA_ALERT_TGIDS = {1481}

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
    return [dict(r) for r in rows]


def get_all_incidents(limit=50) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY ts_start DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def extract_location(text: str) -> tuple[float | None, float | None, str | None]:
    if not text:
        return None, None, None
    lower = text.lower()
    for keyword, lat, lon in LOCATION_HINTS:
        if keyword in lower:
            return lat, lon, keyword.title()
    return None, None, None


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_lock  = threading.Lock()


def get_model():
    global _whisper_model
    if _whisper_model is None:
        print(f"[whisper] loading model '{WHISPER_MODEL}'...", flush=True)
        _whisper_model = whisper.load_model(WHISPER_MODEL)
        print("[whisper] model ready", flush=True)
    return _whisper_model


def transcribe(wav_bytes: bytes) -> str:
    with _whisper_lock:
        model = get_model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp = f.name
        try:
            result = model.transcribe(tmp, fp16=False)
            return result["text"].strip()
        except Exception as e:
            print(f"[whisper] error: {e}", flush=True)
            return ""
        finally:
            os.unlink(tmp)


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
_incident_lock = threading.Lock()


def analyze_for_incident(call: dict):
    """Run after each call is stored. Detect and record incidents."""
    tgid  = call.get("tgid", 0)
    cat   = call.get("category", "Unknown")
    text  = (call.get("transcript") or "").lower()
    ts    = call.get("ts", time.time())

    flags = []   # list of (priority, itype, description)

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
    for kw, itype in INCIDENT_KEYWORDS:
        if kw in text:
            flags.append((20, itype,
                          f"'{kw}' detected on {call.get('tag', tgid)}"))
            break

    # --- Rule 4: Locution dispatch ---
    if tgid in LOCUTION_TGIDS and len(text) > 8:
        flags.append((15, "FIRE DISPATCH",
                      f"Locution active ({call.get('tag', tgid)}): {text[:80]}"))

    # --- Rule 5: Multi-agency convergence ---
    window = calls_since(ts - MULTIAGENCY_WINDOW_MIN * 60)
    active_cats = {c["category"] for c in window
                   if c["category"] not in (None, "Unknown", "TXDOT", "Interop")}
    ps_cats = active_cats & {"APD", "AFD", "TCEMS", "ABIA", "TCSO", "TCFD"}
    if len(ps_cats) >= 2:
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
    stage = _detect_escalation_stage(call.get("transcript") or "")
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
            _consider_hold(tgid, _active_incidents[loc_match]["itype"])
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
        _consider_hold(tgid, itype)


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
                threading.Thread(target=clear_banner, args=(itype,), daemon=True).start()


# ---------------------------------------------------------------------------
# OP25 hold / skip control
# ---------------------------------------------------------------------------

_current_hold_tgid: int | None = None
_last_hold_activity: float = 0.0
_hold_lock = threading.Lock()


def _consider_hold(tgid: int, itype: str):
    global _current_hold_tgid, _last_hold_activity
    with _hold_lock:
        if _current_hold_tgid == tgid:
            _last_hold_activity = time.time()
            return
        _send_hold(tgid)


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

PI_WATCHDOG_INTERVAL   = 300   # check every 5 minutes
PI_CALL_SILENCE_MINS   = 5     # alert if no calls received for this long
PI_ALERT_USERS         = ["kevin"]  # Talk usernames to DM on outage
PI1_OP25_CMD_URL       = "http://192.168.1.103:8080/"  # OP25 command endpoint

_pi_was_down       = False
_op25_was_dead     = False
_calls_were_silent = False
_last_call_ts      = time.time()   # updated on every received call


def _pi_watchdog_alert(msg: str):
    """Send a DM alert to watchdog users."""
    for username in PI_ALERT_USERS:
        token = _get_or_create_dm_room(username)
        if not token:
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
        except Exception as e:
            print(f"[watchdog] DM failed for {username}: {e}", flush=True)


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

        # --- Check 3: Calls received recently ---
        silence_secs = time.time() - _last_call_ts
        if silence_secs > PI_CALL_SILENCE_MINS * 60 and not _calls_were_silent:
            _calls_were_silent = True
            mins = int(silence_secs // 60)
            _pi_watchdog_alert(
                f"⚠️ BATTLE BUDDY ALERT: No audio received from OP25 in {mins} minutes — check SDR or collector."
            )
        elif silence_secs <= PI_CALL_SILENCE_MINS * 60 and _calls_were_silent:
            _calls_were_silent = False
            _pi_watchdog_alert("✅ Audio feed is active again — calls resuming.")


# ---------------------------------------------------------------------------
# Sitrep generator
# ---------------------------------------------------------------------------

def build_sitrep(minutes=60) -> str:
    calls = calls_for_sitrep(minutes)
    incidents = active_incidents()

    lines = [
        f"SITUATION REPORT — last {minutes} min — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Total calls: {len(calls)}",
    ]

    if incidents:
        lines.append("")
        lines.append("*** ACTIVE INCIDENTS ***")
        for inc in incidents:
            age = int((time.time() - inc["ts_start"]) / 60)
            updated = int((time.time() - inc["ts_updated"]) / 60)
            agencies = ", ".join(json.loads(inc["agencies"] or "[]"))
            loc = f" @ {inc['location']}" if inc.get("location") else ""
            lines.append(
                f"  [{inc['itype']}]{loc} — started {age}m ago, "
                f"last activity {updated}m ago — agencies: {agencies}"
            )
            lines.append(f"  {inc['description']}")
        lines.append("*** END ACTIVE INCIDENTS ***")

    if not calls:
        lines.append(f"\nNo calls in the last {minutes} minutes.")
        return "\n".join(lines)

    by_cat: dict[str, list] = {}
    for c in calls:
        cat = c["category"] or "Unknown"
        by_cat.setdefault(cat, []).append(c)

    lines.append("")
    for cat, items in sorted(by_cat.items()):
        lines.append(f"[ {cat} ] — {len(items)} call(s)")
        for c in items[:5]:
            ts  = datetime.fromtimestamp(c["ts"]).strftime("%H:%M")
            loc = f" @ {c['location']}" if c.get("location") else ""
            txt = c["transcript"] or "(no transcript)"
            if len(txt) > 120:
                txt = txt[:120] + "..."
            lines.append(f"  {ts} {c['tag'] or c['tgid']}{loc}: {txt}")
        if len(items) > 5:
            lines.append(f"  ... and {len(items)-5} more")
        lines.append("")

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

    # --- Priority flag ---
    if matched_itype in _HIGH_PRIORITY or any(k in text_lower for k in _HIGH_KW):
        priority = "🔴"
    elif matched_itype in _MED_PRIORITY or any(k in text_lower for k in _MED_KW):
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


@app.route("/api/incidents")
def api_incidents():
    return jsonify(get_all_incidents(50))


@app.route("/api/incidents/active")
def api_incidents_active():
    return jsonify(active_incidents())


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
            f"Whisper model: {WHISPER_MODEL}"
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

    elif command == "!help":
        respond(
            "🤖 Battle Buddy Commands\n\n"
            "!sitrep [minutes] — Situation report (default 60m, max 360m)\n"
            "!incidents — List active incidents\n"
            "!status — System status and call volume\n"
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
</div>
<div id="breaking"></div>
<div id="map"></div>
<div id="legend">
  <h4>Agencies</h4>
  <div class="leg-item"><div class="leg-dot" style="background:#3b82f6"></div><span>APD / Law Enforcement</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#f97316"></div><span>AFD / Fire</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#22c55e"></div><span>EMS</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#a855f7"></div><span>DPS / State</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#ef4444"></div><span>&#9654; Active Incident</span></div>
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

const map = L.map('map').setView([30.2672, -97.7431], 11);
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
  const pts = calls.filter(c => c.lat && c.lon).map(c => [c.lat, c.lon, 0.6]);
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

async function loadIncidents() {
  const [activeResp, allResp] = await Promise.all([
    fetch('/api/incidents/active'), fetch('/api/incidents')]);
  const active = await activeResp.json();
  const all    = await allResp.json();
  document.getElementById('s-incidents').textContent = all.length;
  document.getElementById('s-active').textContent    = active.length;

  // Breaking bar
  const bar = document.getElementById('breaking');
  if (active.length > 0) {
    bar.textContent = '⚠ BREAKING: ' + active.map(i =>
      i.itype + (i.location ? ' @ ' + i.location : '')).join('  ·  ');
    bar.classList.add('show');
  } else {
    bar.classList.remove('show');
  }

  // Clear old markers
  Object.values(incidentMarkers).forEach(m => map.removeLayer(m));

  // Add incident markers
  all.filter(i => i.lat && i.lon).forEach(inc => {
    const isActive = inc.status === 'active';
    const color = isActive ? '#ef4444' : '#334155';
    const glow  = isActive ? `box-shadow:0 0 14px #ef4444` : '';
    const icon = L.divIcon({
      html: `<div style="width:${isActive?22:14}px;height:${isActive?22:14}px;background:${color};border:2px solid ${isActive?'#fca5a5':'#475569'};border-radius:50%;${glow}"></div>`,
      iconSize:[isActive?22:14,isActive?22:14], iconAnchor:[isActive?11:7,isActive?11:7], className:''
    });
    const m = L.marker([inc.lat, inc.lon], {icon}).addTo(map);
    let agencies = '';
    try { agencies = JSON.parse(inc.agencies||'[]').join(', '); } catch(e){}
    m.bindPopup(`
      <div class="popup-custom">
        <div class="itype">${inc.itype}</div>
        <div class="meta">${new Date(inc.ts_start*1000).toLocaleString()} · ${timeAgo(inc.ts_start)} · ${inc.status.toUpperCase()}</div>
        ${inc.location ? `<div class="meta">📍 ${inc.location}</div>` : ''}
        <div class="meta">Agencies: ${agencies||'unknown'}</div>
        <div class="transcript">${inc.description||''}</div>
      </div>
    `);
    incidentMarkers[inc.id] = m;
  });
}

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

  // Breaking bar
  const bar = document.getElementById('breaking');
  if (active.length) { bar.textContent='⚠ BREAKING: '+active.map(i=>i.itype+(i.location?' @ '+i.location:'')).join(' · '); bar.classList.add('show'); }
  else bar.classList.remove('show');

  // Incidents
  const inc = document.getElementById('incidents-section');
  if (!all.length) { inc.innerHTML='<p style="color:#475569;font-size:0.8rem">No incidents in the last 48 hours.</p>'; }
  else inc.innerHTML = all.map(i => {
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
    ap.add_argument("--model",       default=WHISPER_MODEL)
    ap.add_argument("--enable-hold", action="store_true",
                    help="Enable OP25 hold/skip commands to Pi 1 (test carefully)")
    args = ap.parse_args()

    WHISPER_MODEL = args.model
    if args.enable_hold:
        HOLD_ENABLED = True
        print("[hold] OP25 hold/skip ENABLED — will send commands to Pi 1", flush=True)
    else:
        print("[hold] OP25 hold/skip DISABLED (run with --enable-hold to activate)", flush=True)

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    load_talkgroups()
    init_db()

    print(f"[brain] Battle Buddy v2.0 starting on port {args.port}", flush=True)
    print(f"[brain] Whisper model: {WHISPER_MODEL}", flush=True)
    print(f"[brain] DB: {DB_PATH}", flush=True)

    threading.Thread(target=get_model,                daemon=True).start()
    threading.Thread(target=incident_cleanup_thread,  daemon=True).start()
    threading.Thread(target=hold_watchdog_thread,     daemon=True).start()
    threading.Thread(target=pi_watchdog_thread,       daemon=True).start()

    app.run(host="0.0.0.0", port=args.port, threaded=True)
