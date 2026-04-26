import os
import re
from modules.config import TGID_TSV

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

CAT_COORDS = {
    "APD":          (30.2672, -97.7431),
    "AFD":          (30.2672, -97.7431),
    "TCEMS":        (30.2672, -97.7431),
    "ABIA":         (30.1975, -97.6664),
    "TCSO":         (30.2672, -97.7431),
    "TCFD":         (30.2672, -97.7431),
    "UTPD":         (30.2849, -97.7341),
    "DPS":          (30.2747, -97.7404),
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
    "DPS":          "#fbbf24",
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

CAPITOL_KEYWORDS = ["capitol", "state capitol", "congress ave", "11th street", "governor",
                    "state cemetery", "governor's mansion", "mansion"]

TRANSIT_TGIDS  = set(range(972, 988))
LOCUTION_TGIDS = {1147, 1162}

LOCUTION_CORRECTIONS = [
    (re.compile(r'(?i)assault'), 'stabbing'),
    (re.compile(r'(?i)a salt'),  'stabbing'),
]

ABIA_ALERT_TGIDS = set()
ABIA_OPS_TGIDS   = {1471, 1472, 1474, 1476, 1478, 1479, 1480, 1481, 1487}
AIR_ASSET_TGIDS  = {989, 1521, 1522, 1523}

AIR_ASSET_PATTERN = re.compile(
    r'\b(helo|helicopter|air\s*(?:unit|support|asset|one|two)|aviation|'
    r'bird\s*(?:up|in\s*the\s*air|is\s*up|overhead)|'
    r'chopper|aircraft|fly[ing]*\s*over|eye\s*in\s*the\s*sky|'
    r'unit\s*(?:air|a/?c)|airship|rotary)\b', re.I
)

AIR_ASSET_CONTEXT = {
    "APD":     "pursuit, active shooter perimeter, search, or crowd overwatch",
    "DPS":     "dignitary protection, Capitol overwatch, or major protest response",
    "TCSO":    "rural search, pursuit, or major incident perimeter",
    "AFD":     "aerial water drop or large structure fire recon",
    "ABIA":    "aircraft emergency or airfield security",
    "default": "major law enforcement or emergency response operation",
}

IGNORE_TGIDS: set = set()
TGID_META: dict   = {}


def detect_dps_assets(transcript: str) -> list:
    if not transcript:
        return []
    return [label for pattern, label in DPS_ASSET_PATTERNS if pattern.search(transcript)]


def is_capitol_area(transcript: str, location) -> bool:
    text = (transcript + " " + (location or "")).lower()
    return any(k in text for k in CAPITOL_KEYWORDS)


def mentions_dps(transcript: str) -> bool:
    return bool(DPS_MENTION_PATTERNS.search(transcript or ""))


def detect_air_asset(tgid: int, transcript: str, category: str):
    if tgid in AIR_ASSET_TGIDS or AIR_ASSET_PATTERN.search(transcript or ""):
        return AIR_ASSET_CONTEXT.get(category, AIR_ASSET_CONTEXT["default"])
    return None


def _apply_locution_corrections(transcript: str) -> str:
    for pattern, replacement in LOCUTION_CORRECTIONS:
        transcript = pattern.sub(replacement, transcript)
    return transcript


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
                cat      = _tag_to_category(tag)
                lat, lon = CAT_COORDS.get(cat, CAT_COORDS["Unknown"])
                TGID_META[tgid] = {"tag": tag, "cat": cat, "lat": lat, "lon": lon}
                loaded += 1
    print(f"[tg] {loaded} talkgroups loaded, {ignored} on ignore list", flush=True)
