"""
Battle Buddy -- modules/config.py
Centralised constants and configuration.
No functions, no logic, no side effects.
Extracted from audio_receiver.py (PR 1 -- modularisation refactor).
"""

import os
import re

try:
    import anthropic as _anthropic_mod
except ImportError:
    _anthropic_mod = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DB_PATH       = "/opt/battlebuddy/calls.db"
TIPS_UPLOAD_DIR = "/opt/battlebuddy/static/tips"
TGID_TSV      = "/opt/battlebuddy/gatrrs-tags.tsv"
PI1_OP25_URL  = "http://radiodesk.ddns.net:8080/"

# ---------------------------------------------------------------------------
# API keys / credentials
# ---------------------------------------------------------------------------

# Groq -- LLM incident analysis (llama-3.3-70b), called directly from Contabo
# Audio transcription is LOCAL (faster-whisper), works offline in the field
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL          = "llama-3.3-70b-versatile"
GROQ_ENABLED        = bool(GROQ_API_KEY)
GROQ_API_BASE       = "https://api.groq.com/openai/v1"

# Anthropic Claude -- used for intel query synthesis
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_ENABLED   = bool(ANTHROPIC_API_KEY) and (_anthropic_mod is not None)

# Nextcloud Talk -- post each transcript to the BattleBuddy room
TALK_BASE    = "https://kevcloud.ddns.net/ocs/v2.php/apps/spreed/api/v1"
TALK_USER    = "battlebuddy"
TALK_PASS    = os.environ.get("TALK_PASS", "")
TALK_ENABLED = True

# Mailgun email alerts
MAILGUN_API_KEY  = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN   = os.environ.get("MAILGUN_DOMAIN", "")
MAILGUN_FROM     = f"Battle Buddy <mailgun@{MAILGUN_DOMAIN}>"
ALERT_EMAIL      = "k.watkins@me.com"

# Google Custom Search -- article URL resolution for APD news poller
GOOGLE_CSE_API_KEY  = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_ID       = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_ROUTES_KEY   = os.environ.get("GOOGLE_ROUTES_KEY", "")
GOOGLE_MAPS_JS_KEY  = os.environ.get("GOOGLE_MAPS_JS_KEY", "")

# Pi5 Fetch Agent -- residential IP article fetcher (fetch_agent.py on Pi)
# Quick-tunnel URL changes on Pi reboot; update PI_FETCH_URL in .env after restart.
# Upgrade to a named Cloudflare Tunnel for a stable subdomain.
PI_FETCH_URL     = os.environ.get("PI_FETCH_URL", "").rstrip("/")
PI_FETCH_TOKEN   = os.environ.get("PI_FETCH_TOKEN", "")
PI_FETCH_ENABLED = bool(PI_FETCH_URL and PI_FETCH_TOKEN)

# FreeTAKServer ATAK integration
FTS_HOST      = "radiodesk.ddns.net"
FTS_REST_PORT = 19023
FTS_COT_PORT  = 8089
FTS_TOKEN     = os.environ.get("FTS_TOKEN", "")
FTS_ENABLED   = True

# Deck integration
DECK_BASE     = "https://kevcloud.ddns.net/index.php/apps/deck/api/v1.0"

# Nextcloud WebDAV -- incident snapshot exports
NC_WEBDAV     = "https://kevcloud.ddns.net/remote.php/dav/files/kevin"
NC_USER       = os.environ.get("NC_USER", "")
NC_PASS       = os.environ.get("NC_PASS", "")
NC_REPORT_DIR = "PresentationNotes/FlaggedIncidents"
DECK_BOARD_ID = 2
DECK_STACK_NEW = 5        # New
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

# ---------------------------------------------------------------------------
# Talk rooms
# ---------------------------------------------------------------------------

# Beat rooms -- each category routes to its own room
# Main room is the original catch-all (kept for bot commands)
TALK_ROOM    = "iyidr3xy"   # general / catch-all (kxq9mkms was deleted)
TALK_ROOMS   = {
    "incidents": "89q5fnh5",  # priority alerts only
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

# Talk bot shared secret -- must match what is registered with occ talk:bot:install
TALK_BOT_SECRET = os.environ.get("TALK_BOT_SECRET", "")

# ---------------------------------------------------------------------------
# Hold / incident timing
# ---------------------------------------------------------------------------

# Hold/skip commands to Pi 1 OP25 -- OFF until behavior is verified.
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
_INCIDENT_TIMEOUT_DEFAULT = 10  # minutes -- crash, generic keyword hits

# Multi-agency convergence window (minutes)
MULTIAGENCY_WINDOW_MIN = 15

# APD surge detection
APD_SURGE_WINDOW_MIN  = 10
APD_SURGE_THRESHOLD   = 4   # calls within window to trigger

# ---------------------------------------------------------------------------
# Talkgroup loading -- from RadioReference TSV export
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

# Map tag substrings -> agency category
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
    "DPS":          "#fbbf24",   # Gold -- state agency
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
# Austin is the Texas state capital. DPS is not just highway patrol --
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

# ---------------------------------------------------------------------------
# Talkgroup sets
# ---------------------------------------------------------------------------

# APD Metro 1-10 (972-987) -- active only for Cap Metro transit incidents
TRANSIT_TGIDS = set(range(972, 988))

# Fire dispatch channels -- when active, a significant fire response is likely
LOCUTION_TGIDS = {1147, 1162}   # AFD Locution, TCFD Locution

# Known Whisper misreads on locution transcripts.
# Locution systems read a CAD incident type code as the first word(s) of each
# dispatch message (e.g. "Stabbing, check for staging..." or "Structure Fire...").
# Whisper occasionally mishears these short phonetic codes.  These corrections
# are applied only to LOCUTION_TGID transcripts before classification so that
# Rule 3 (keyword match) and Rule 4 (locution dispatch) get clean input.
LOCUTION_CORRECTIONS: list = [
    # "Assault" at the start of a locution dispatch -> likely "A stab" or
    # "Stabbing" misheard by Whisper.  Fire/EMS locutions use "Assault" as the
    # CAD code for a stabbing/cutting victim, so this also normalises genuine
    # CAD-coded assaults that are physically stabbings.
    (re.compile(r'(?i)\bassault\b'), 'stabbing'),
    # "A salt" / "a salt" -> occasionally produced by Whisper for "assault"
    (re.compile(r'(?i)\ba salt\b'), 'stabbing'),
]

# Airport emergency -- tgid 1481 turned out to be routine ops chatter, not alerts.
# Leaving this empty until the real ABIA emergency channel is identified.
ABIA_ALERT_TGIDS: set = set()

# ABIA operational talkgroups -- routine airport ops that use alarming-sounding
# language (barricade, hostage, weapons, code red) in normal daily context.
# Exclude from keyword matching to prevent false positives.
ABIA_OPS_TGIDS = {1471, 1472, 1474, 1476, 1478, 1479, 1480, 1481, 1487}

# Air asset talkgroups -- ANY activity here is high-signal news.
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

# What air asset deployment typically signals -- for reporter context
AIR_ASSET_CONTEXT = {
    "APD":   "pursuit, active shooter perimeter, search, or crowd overwatch",
    "DPS":   "dignitary protection, Capitol overwatch, or major protest response",
    "TCSO":  "rural search, pursuit, or major incident perimeter",
    "AFD":   "aerial water drop or large structure fire recon",
    "ABIA":  "aircraft emergency or airfield security",
    "default": "major law enforcement or emergency response operation",
}

IGNORE_TGIDS: set = set()
TGID_META: dict = {}

# ---------------------------------------------------------------------------
# Processing thread limits
# ---------------------------------------------------------------------------

_MAX_PROCESS_THREADS = 20   # hard cap on concurrent process() threads
_BROADCASTIFY_MAX    = 15   # broadcastify can hold at most this many slots (reserves 5 for pi5)

# ---------------------------------------------------------------------------
# Incident detection engine
# ---------------------------------------------------------------------------

# Ordered by priority -- first match wins for a given call
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
    ("assault",        "STABBING"),   # locution CAD code for stabbing/cutting victim
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
    # Fatal crash -- longer phrases before generic "crash" so they match first
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

# Ordered escalation stages -- higher index = more serious
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
ITYPE_SEVERITY: dict = {
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

# Compatible itype groups -- incidents of these types merge even if itype differs.
# Key: new itype being detected. Value: set of existing itypes it can merge into.
ITYPE_MERGE_COMPAT: dict = {
    "FATAL CRASH":         {"CRASH/COLLISION", "PEDESTRIAN INCIDENT", "DEATH INVESTIGATION"},
    "CRASH/COLLISION":     {"FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "PEDESTRIAN INCIDENT": {"CRASH/COLLISION", "FATAL CRASH"},
    "DEATH INVESTIGATION": {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
}

# ---------------------------------------------------------------------------
# TGID tier map -- higher tier = more tactically specific channel.
# When escalation stage rises, _consider_hold switches to higher-tier TGIDs.
# Tier 0 = non-public-safety (water, parking, energy) -- never hold for crime.
# Tier 1 = dispatch (initial report + coordinator traffic).
# Tier 2 = metro/field (unit coordination, pursuits).
# Tier 3 = tactical (SWAT, K9, air operations).
# ---------------------------------------------------------------------------
TGID_TIER: dict = {
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
ESCALATION_MIN_TIER: dict = {
    "welfare":     1,  # any dispatch is fine
    "disturbance": 1,
    "weapons":     1,
    "pursuit":     2,  # need metro -- units are moving
    "backup":      2,
    "k9":          2,
    "tactical":    3,  # need TAC -- SWAT/negotiators on scene
    "air":         3,
}

# Location match radius -- calls within this many km link to an existing incident
INCIDENT_LOCATION_RADIUS_KM = 0.5
