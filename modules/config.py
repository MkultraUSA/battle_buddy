import os
import re

# ---------------------------------------------------------------------------
# Core paths and connectivity
# ---------------------------------------------------------------------------
DB_PATH = "/opt/battlebuddy/calls.db"

# Hard cap on concurrent audio processing threads
_MAX_PROCESS_THREADS = 20

# ---------------------------------------------------------------------------
# Talkgroup classification constants
# ---------------------------------------------------------------------------

# Fire/EMS locution dispatch channels (AFD Locution, TCFD Locution)
LOCUTION_TGIDS = {1147, 1162}

# APD Metro 1-10 (972-987) — active only for Cap Metro transit incidents
TRANSIT_TGIDS = set(range(972, 988))

# Airport emergency channels
ABIA_ALERT_TGIDS = set()

# ABIA operational talkgroups — routine ops; exclude from keyword matching
ABIA_OPS_TGIDS = {1471, 1472, 1474, 1476, 1478, 1479, 1480, 1481, 1487}

# Air asset talkgroups — APD Air/K9, APD Aviation 1/2/CID
AIR_ASSET_TGIDS = {989, 1521, 1522, 1523}

# ---------------------------------------------------------------------------
# Incident keyword detection — ordered by priority (first match wins)
# ---------------------------------------------------------------------------
INCIDENT_KEYWORDS = [
    ("officer down",            "OFFICER DOWN"),
    ("10-99",                   "OFFICER DOWN"),
    ("shots fired",             "SHOOTING"),
    ("shooting",                "SHOOTING"),
    ("person shot",             "SHOOTING"),
    ("subject shot",            "SHOOTING"),
    ("victim shot",             "SHOOTING"),
    ("shot victim",             "SHOOTING"),
    ("homicide",                "SHOOTING"),
    ("found dead",              "SHOOTING"),
    ("body found",              "SHOOTING"),
    ("gsw",                     "SHOOTING"),
    ("gunshot",                 "SHOOTING"),
    ("gun shot",                "SHOOTING"),
    ("stabbing",                "STABBING"),
    (" stab",                   "STABBING"),
    ("assault",                 "STABBING"),   # locution CAD code for stabbing
    ("aircraft",                "AIRCRAFT EMERGENCY"),
    ("mass casualty",           "MASS CASUALTY"),
    ("mci",                     "MASS CASUALTY"),
    ("cardiac arrest",          "EMS DISPATCH"),
    ("multiple patients",       "EMS DISPATCH"),
    ("trauma",                  "EMS DISPATCH"),
    ("structure fire",          "STRUCTURE FIRE"),
    ("working fire",            "STRUCTURE FIRE"),
    ("fully involved",          "STRUCTURE FIRE"),
    ("hazmat",                  "HAZMAT"),
    ("chemical spill",          "HAZMAT"),
    ("hostage",                 "HOSTAGE/BARRICADE"),
    ("barricade",               "HOSTAGE/BARRICADE"),
    ("fatal crash",             "FATAL CRASH"),
    ("fatal accident",          "FATAL CRASH"),
    ("fatality",                "FATAL CRASH"),
    ("start a dts",             "FATAL CRASH"),
    ("crash",                   "CRASH/COLLISION"),
    ("collision",               "CRASH/COLLISION"),
    ("rollover",                "CRASH/COLLISION"),
    ("medical examiner",        "DEATH INVESTIGATION"),
    ("jp responding",           "DEATH INVESTIGATION"),
    ("justice of the peace",    "DEATH INVESTIGATION"),
    ("pronounce",               "DEATH INVESTIGATION"),
    ("pronounced at",           "DEATH INVESTIGATION"),
    ("death investigation",     "DEATH INVESTIGATION"),
    ("signal 48",               "DEATH INVESTIGATION"),
]

# ---------------------------------------------------------------------------
# Escalation chain detection — ordered stages (higher index = more serious)
# ---------------------------------------------------------------------------
ESCALATION_STAGES = [
    ("welfare",     ["welfare check", "well-being check", "wbc", "check on subject"]),
    ("disturbance", ["disturbance", "domestic", "fight", "altercation", "argument"]),
    ("pursuit",     ["pursuit", "foot chase", "fleeing", "chase"]),
    ("weapons",     ["weapon", "armed", "firearm", "gun", "knife", "rifle"]),
    ("backup",      ["need backup", "requesting backup", "all units", "code 3", "lights and sirens"]),
    ("tactical",    ["swat", "tac team", "tactical", "negotiat", "standoff", "barricaded"]),
    ("k9",          ["k-9", "k9", "canine", "dog track", "dog unit"]),
    ("air",         ["air1", "air 1", "helicopter", "aviation", "bird in the air"]),
]

ESCALATION_STAGE_NAMES = [s[0] for s in ESCALATION_STAGES]

# ---------------------------------------------------------------------------
# Incident severity — higher value = more urgent (used for itype upgrades)
# ---------------------------------------------------------------------------
ITYPE_SEVERITY: dict[str, int] = {
    "CRASH/COLLISION":        1,
    "PEDESTRIAN INCIDENT":    2,
    "FIRE DISPATCH":          2,
    "TRANSIT INCIDENT":       2,
    "DEATH INVESTIGATION":    3,
    "FATAL CRASH":            4,
    "SHOOTING":               5,
    "STABBING":               5,
    "WEAPONS":                5,
    "STRUCTURE FIRE":         5,
    "HAZMAT":                 5,
    "OFFICER DOWN":           6,
    "MASS CASUALTY":          6,
    "HOSTAGE/BARRICADE":      6,
    "AIRCRAFT EMERGENCY":     6,
}

# Compatible itype groups — incidents of these types merge even if itype differs
ITYPE_MERGE_COMPAT: dict[str, set] = {
    "FATAL CRASH":         {"CRASH/COLLISION", "PEDESTRIAN INCIDENT", "DEATH INVESTIGATION"},
    "CRASH/COLLISION":     {"FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "PEDESTRIAN INCIDENT": {"CRASH/COLLISION", "FATAL CRASH"},
    "DEATH INVESTIGATION": {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
}

# ---------------------------------------------------------------------------
# Per-type incident timeout (minutes of silence before auto-close)
# ---------------------------------------------------------------------------
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
INCIDENT_TIMEOUT_DEFAULT = 10  # minutes — generic keyword hits

# ---------------------------------------------------------------------------
# Multi-agency / surge detection
# ---------------------------------------------------------------------------
MULTIAGENCY_WINDOW_MIN = 15
APD_SURGE_WINDOW_MIN   = 10
APD_SURGE_THRESHOLD    = 4

# Location match radius — calls within this many km link to an existing incident
INCIDENT_LOCATION_RADIUS_KM = 0.5

# OP25 hold control
HOLD_ENABLED          = False
HOLD_RELEASE_MINUTES  = 5

# ---------------------------------------------------------------------------
# TGID tier map — used by _consider_hold escalation logic
# ---------------------------------------------------------------------------
TGID_TIER: dict[int, int] = {
    **{tgid: 1 for tgid in range(960, 970)},    # APD Dispatch 1-10
    **{tgid: 2 for tgid in range(972, 988)},    # APD Metro 1-16
    **{tgid: 3 for tgid in [1000, 1001, 1002]}, # APD TAC 1-3
    1121: 1, 1122: 1,                            # AFD Dispatch 1-2
    1155: 2,                                     # AFD TAC
    1162: 1,                                     # TCFD Locution
    1371: 1, 1377: 1, 1378: 1,                  # AFD zonal
    1471: 1, 1472: 2, 1473: 2,
    1474: 2, 1480: 3, 1481: 3,
    989: 2,                                      # APD Air/K9
    **{tgid: 2 for tgid in range(1020, 1027)},  # APD Narc 1-7
    1274: 2,                                     # TCEMS SWAT
    2409: 3, 2410: 3,                            # TCSO SWAT 1-2
    5291: 2, 5292: 2,                            # Austin/Travis Interop 1-2
}

ESCALATION_MIN_TIER: dict[str, int] = {
    "welfare":     1,
    "disturbance": 1,
    "weapons":     1,
    "pursuit":     2,
    "backup":      2,
    "k9":          2,
    "tactical":    3,
    "air":         3,
}

# ---------------------------------------------------------------------------
# DPS / Capitol intelligence patterns
# ---------------------------------------------------------------------------
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

DPS_MENTION_PATTERNS = re.compile(
    r'\b(dps|state\s*trooper|highway\s*patrol|texas\s*ranger|ranger\s*unit|capitol\s*police|'
    r'protect.*detail|executive\s*protect)\b', re.I
)

CAPITOL_KEYWORDS = [
    "capitol", "state capitol", "congress ave", "11th street", "governor",
    "state cemetery", "governor's mansion", "mansion",
]

# Transcript patterns for air asset detection across any agency
AIR_ASSET_PATTERN = re.compile(
    r'\b(helo|helicopter|air\s*(?:unit|support|asset|one|two)|aviation|'
    r'bird\s*(?:up|in\s*the\s*air|is\s*up|overhead)|'
    r'chopper|aircraft|fly[ing]*\s*over|eye\s*in\s*the\s*sky|'
    r'unit\s*(?:air|a/?c)|airship|rotary)\b', re.I
)

AIR_ASSET_CONTEXT = {
    "APD":   "pursuit, active shooter perimeter, search, or crowd overwatch",
    "DPS":   "dignitary protection, Capitol overwatch, or major protest response",
    "TCSO":  "rural search, pursuit, or major incident perimeter",
    "AFD":   "aerial water drop or large structure fire recon",
    "ABIA":  "aircraft emergency or airfield security",
    "default": "major law enforcement or emergency response operation",
}

# Known Whisper misreads on locution transcripts
LOCUTION_CORRECTIONS = [
    (re.compile(r'(?i)\bassault\b'), 'stabbing'),
    (re.compile(r'(?i)\ba salt\b'),  'stabbing'),
]
