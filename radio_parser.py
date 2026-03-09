#!/usr/bin/env python3
"""
Battle Buddy — Radio Log → Incident Parser  v1.4
Uses Claude API (Haiku) to extract incidents from raw radio transcription.
Incremental: only processes new lines since last run via logs/.processed_offset

Changes in v1.4:
  - Replaced Ollama/llama3 with Claude API (claude-haiku) — faster, more accurate,
    no longer dependent on local GPU/CPU inference
  - Expanded incident type library with 15+ new major incident categories:
      air unit search, swat/tactical, barricaded subject, shots fired, shooting,
      armed robbery, officer needs assistance, officer down, missing person,
      amber alert, death investigation, homicide, kidnapping, bank robbery,
      major accident, water rescue, hazmat, structure fire, explosion,
      DWI, hit and run, bomb threat
  - ANTHROPIC_API_KEY loaded from config.env / environment

Changes in v1.3:
  - Expanded geocoding from Travis County only → full Austin Metro region
      → now covers Travis, Williamson, Hays, Bastrop counties
      → includes Pflugerville, Round Rock, Cedar Park, Georgetown, Kyle,
        Buda, San Marcos, Lago Vista, Leander, Manor, Elgin + all ISDs
  - Renamed TRAVIS_COUNTY constant → AUSTIN_METRO
  - Fixed verify bug: PhoneTrack getlastpositions returns list not dict
      → was crashing with 'list object has no attribute values'

Changes in v1.2:
  - PhoneTrack push now uses logGet (HTTP GET) instead of OwnTracks (POST)
      → simpler, more reliable, no JSON body
  - Each incident TYPE becomes its own PhoneTrack device name, so incidents
    appear as separate tracks on the map (Welfare_Check, Collision, etc.)
  - POST verify: after pushing, script queries /api/getlastpositions to confirm
    the point landed (optional, enabled with --verify)
  - PHONETRACK_BASE_URL replaces old single PHONETRACK_URL constant
  - All incidents and [HEARD] lines now written to SQLite DB (battle_buddy_db.py)
      → enables heatmaps, historical queries, archive enrichment

Changes in v1.1:
  - Geocoder now bounded to Austin Metro, TX — no more global false matches
  - Improved JSON parser handles trailing commas and single quotes
  - Ollama timeout increased to 120s
  - Batch size increased to 15 (fewer LLM calls)
  - Fixed datetime.utcnow() deprecation warning
  - --reset now prints a clear warning before proceeding
  - Tightened LLM prompt to reject garbage addresses before geocoding

Usage:
    cd ~/battle_buddy
    python3 radio_parser.py --log logs/radio_law_20260306.log
    python3 radio_parser.py --log logs/radio_law_20260306.log --push
    python3 radio_parser.py --log logs/radio_law_20260306.log --push --verify
    python3 radio_parser.py --log logs/radio_law_20260306.log --reset --push

Pipeline:
    [HEARD] lines → llama3 (Ollama) → structured incidents
                  → Nominatim geocoder (Austin Metro bounded)
                  → GeoJSON file  +  optional PhoneTrack push (per-type device)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# Load config.env if present
_config_env = Path(__file__).parent / "config.env"
if _config_env.exists():
    for _line in _config_env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# Optional DB integration — gracefully skipped if battle_buddy_db.py not present
try:
    from battle_buddy_db import BattleBuddyDB
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

VERSION         = "1.4"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL    = "claude-haiku-4-5-20251001"
BATCH_SIZE      = 20          # [HEARD] lines per LLM call
BATCH_OVERLAP   = 2           # lines of overlap between batches (context continuity)
GEOCODE_DELAY   = 1.1         # seconds between Nominatim requests

# Austin Metro bounding box — covers Travis, Williamson, Hays, Bastrop counties
# Includes: Austin, Pflugerville, Round Rock, Cedar Park, Georgetown,
#           Kyle, Buda, San Marcos, Lago Vista, Leander, Manor, Elgin
#           + all ISDs and regional agencies on GATRRS
AUSTIN_METRO = {
    "countrycodes": "us",
    "viewbox":      "-98.4000,29.8000,-97.0000,30.9000",  # west,south,east,north
    "bounded":      "1",
}

# PhoneTrack — logGet endpoint (GET request, no JSON body needed)
# Full URL per device:
#   {BASE}/{SESSION_TOKEN}/{DEVICE_NAME}?lat=LAT&lon=LON&timestamp=TS&acc=ACC
PHONETRACK_BASE     = os.environ.get("PHONETRACK_BASE",     "https://kevcloud.ddns.net/apps/phonetrack/logGet")
PHONETRACK_TOKEN    = os.environ.get("PHONETRACK_TOKEN",    "")
PHONETRACK_API_BASE = os.environ.get("PHONETRACK_API_BASE", "https://kevcloud.ddns.net/apps/phonetrack/api/getlastpositions")

# Map incident types → PhoneTrack device names (spaces → underscores, URL-safe)
# Each device name becomes its own track/layer on the PhoneTrack map.
INCIDENT_DEVICE_MAP = {
    # ── Major / high-priority ──────────────────────────────────────────────
    "air unit search":        "Air_Search",
    "air unit":               "Air_Search",
    "helicopter search":      "Air_Search",
    "swat":                   "SWAT",
    "tactical":               "SWAT",
    "barricaded subject":     "SWAT",
    "barricaded":             "SWAT",
    "shots fired":            "Shots_Fired",
    "shooting":               "Shooting",
    "armed robbery":          "Armed_Robbery",
    "officer needs assistance": "Officer_Assist",
    "officer down":           "Officer_Assist",
    "officer assist":         "Officer_Assist",
    "missing person":         "Missing_Person",
    "amber alert":            "Missing_Person",
    "death investigation":    "Death_Investigation",
    "homicide":               "Death_Investigation",
    "murder":                 "Death_Investigation",
    "kidnapping":             "Kidnapping",
    "abduction":              "Kidnapping",
    "bank robbery":           "Bank_Robbery",
    "robbery":                "Armed_Robbery",
    "major accident":         "Major_Accident",
    "highway accident":       "Major_Accident",
    "water rescue":           "Water_Rescue",
    "flood":                  "Water_Rescue",
    "swift water":            "Water_Rescue",
    "hazmat":                 "Hazmat",
    "gas leak":               "Hazmat",
    "chemical":               "Hazmat",
    "structure fire":         "Structure_Fire",
    "building fire":          "Structure_Fire",
    "house fire":             "Structure_Fire",
    "explosion":              "Explosion",
    "bomb threat":            "Bomb_Threat",
    "dwi":                    "DWI",
    "hit and run":            "Hit_And_Run",
    "pursuit":                "Pursuit",
    # ── Standard ──────────────────────────────────────────────────────────
    "welfare check":          "Welfare_Check",
    "medical":                "Medical",
    "mental health":          "Mental_Health",
    "collision":              "Collision",
    "accident":               "Collision",
    "fire":                   "Fire",
    "disturbance":            "Disturbance",
    "fight":                  "Disturbance",
    "suspicious person":      "Suspicious",
    "suspicious vehicle":     "Suspicious",
    "arrest":                 "Arrest",
    "warrant":                "Arrest",
    "theft":                  "Theft",
    "burglary":               "Theft",
    "trespass":               "Trespass",
    "criminal trespass":      "Trespass",
    "panic alarm":            "Alarm",
}
DEFAULT_DEVICE = "Incident"   # fallback for unrecognised types

SEVERITY_COLORS = {
    "high":    "#e63946",
    "medium":  "#f4a261",
    "low":     "#2a9d8f",
    "unknown": "#adb5bd",
}

INCIDENT_ICONS = {
    # ── Major / high-priority ──────────────────────────────────────────────
    "air unit":               "🚁",
    "helicopter":             "🚁",
    "swat":                   "🛡️",
    "tactical":               "🛡️",
    "barricaded":             "🛡️",
    "shots fired":            "🔫",
    "shooting":               "🔫",
    "armed robbery":          "🔫",
    "robbery":                "🔫",
    "officer needs":          "🚨",
    "officer down":           "🚨",
    "missing person":         "👤",
    "amber alert":            "👶",
    "death investigation":    "💀",
    "homicide":               "💀",
    "murder":                 "💀",
    "kidnapping":             "👤",
    "abduction":              "👤",
    "bank robbery":           "🏦",
    "major accident":         "🚧",
    "highway accident":       "🚧",
    "water rescue":           "💧",
    "swift water":            "💧",
    "flood":                  "💧",
    "hazmat":                 "☣️",
    "gas leak":               "☣️",
    "structure fire":         "🏠",
    "building fire":          "🏠",
    "house fire":             "🏠",
    "explosion":              "💥",
    "bomb threat":            "💣",
    "dwi":                    "🍺",
    "hit and run":            "🚗",
    "pursuit":                "🚔",
    # ── Standard ──────────────────────────────────────────────────────────
    "welfare check":          "🏥",
    "collision":              "🚗",
    "accident":               "🚗",
    "suspicious person":      "🚔",
    "suspicious vehicle":     "🚔",
    "arrest":                 "🚔",
    "warrant":                "🚔",
    "disturbance":            "⚠️",
    "fight":                  "⚠️",
    "fire":                   "🔥",
    "medical":                "🏥",
    "mental health":          "🏥",
    "theft":                  "🚔",
    "burglary":               "🚔",
    "trespass":               "🚔",
    "panic alarm":            "🚨",
    "criminal trespass":      "🚔",
}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def incident_device_name(inc_type: str) -> str:
    """Return the PhoneTrack device name for this incident type."""
    t = inc_type.lower().strip()
    for key, name in INCIDENT_DEVICE_MAP.items():
        if key in t:
            return name
    return DEFAULT_DEVICE


# ---------------------------------------------------------------------------
# 1. STATE — track how far we've processed the log
# ---------------------------------------------------------------------------

def get_offset_path(log_path: str) -> Path:
    return Path(log_path).parent / f".{Path(log_path).stem}_offset"


def load_offset(log_path: str) -> int:
    p = get_offset_path(log_path)
    try:
        return int(p.read_text().strip())
    except Exception:
        return 0


def save_offset(log_path: str, offset: int) -> None:
    get_offset_path(log_path).write_text(str(offset))


# ---------------------------------------------------------------------------
# 2. LOG READING — only new [HEARD] lines since last offset
# ---------------------------------------------------------------------------

HEARD_PATTERN = re.compile(
    r"\[(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[HEARD\] (?P<text>.+)"
)


def read_new_lines(log_path: str) -> tuple[list[dict], int]:
    """Return new [HEARD] lines since last saved offset, and new byte offset."""
    offset = load_offset(log_path)
    lines = []
    new_offset = offset

    with open(log_path, "r", encoding="utf-8") as f:
        f.seek(offset)
        for raw in f:
            new_offset += len(raw.encode("utf-8"))
            m = HEARD_PATTERN.match(raw.strip())
            if m:
                lines.append({
                    "timestamp": m.group("timestamp"),
                    "text":      m.group("text").strip(),
                })

    print(f"[reader] {len(lines)} new [HEARD] lines since last run")
    return lines, new_offset


# ---------------------------------------------------------------------------
# 3. LLM PARSING — batch [HEARD] lines through local llama3
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a police dispatch analyst for Travis County, Texas (Austin area).
You will receive raw radio transcription lines from a police scanner.

RADIO CODE REFERENCE — Travis County / Austin PD / TCSO:
10-4=acknowledged, 10-7=out of service, 10-8=in service, 10-15=prisoner in custody,
10-20=location, 10-22=disregard, 10-23=arrived at scene, 10-29=wants/warrants check,
10-32=man with a gun, 10-33=emergency, 10-38=traffic stop, 10-50=traffic accident,
10-52=ambulance needed, 10-55=intoxicated driver/DWI, 10-57=hit and run,
10-65=armed robbery, 10-67=person calling for help/death report, 10-70=fire alarm,
10-71=shooting/officer needs assistance, 10-78=need assistance, 10-80=in pursuit,
10-89=bomb threat, 10-90=alarm, 10-93=disturbance, 10-95=prisoner/subject in custody,
10-96=mental health subject, 10-99=wanted/warrant outstanding,
99=welfare check, BOL/BOLO=be on the lookout, ATL=attempt to locate,
EDP=emotionally disturbed person, DOA=dead on arrival, MVC=motor vehicle collision,
DV=domestic violence, GOA=gone on arrival, UTL=unable to locate,
Code 3=lights and siren/emergency response, Code 4=all clear,
Priority 0=life threatening in progress, Priority 1=serious/just occurred,
Priority 2=urgent, Priority 3=non-emergency.
APD sectors: ADAM=Northwest, BAKER=Downtown/West, CHARLIE=East,
DAVID=Southwest, EDWARD=Northeast, FRANK=Southeast.

Extract ONLY incidents that have a COMPLETE, SPECIFIC street address.

A COMPLETE address requires ALL of these:
- A street NUMBER (e.g. 1500, 604, 2908)
- A street NAME (e.g. West 71st Street, Rainbow Cove, Howard Lane)

REJECT addresses that are:
- Landmarks only (e.g. "McNeil Falls", "Methodist Church", "Smith Elementary", "Papa John's")
- Intersections without numbers (e.g. "Bannister and Second Street")
- Partial (e.g. "29th Street", "Lincoln", "Belmont", "1300 block of North")
- Numbers only (e.g. "1108", "2327", "1875", "2729")
- Vague (e.g. "the residence", "construction site", "union building", "south side of road")
- Highways without mile markers (e.g. "620 North", "183 Northbound")

For each valid incident return a JSON array with exactly these fields:
- "timestamp": copy from the line (YYYY-MM-DD HH:MM:SS)
- "type": plain English incident type — choose from:
    MAJOR: air unit search, swat, barricaded subject, shots fired, shooting, armed robbery,
           officer needs assistance, officer down, missing person, amber alert,
           death investigation, homicide, kidnapping, bank robbery, major accident,
           water rescue, hazmat, structure fire, explosion, bomb threat
    STANDARD: welfare check, collision, disturbance, suspicious person, suspicious vehicle,
              arrest, theft, burglary, trespass, fire, medical, mental health,
              panic alarm, pursuit, criminal trespass, DWI, hit and run
- "address": complete address with number and street name, append city and state if not present
- "severity": "high" (air unit search, swat, barricaded, shots fired, shooting, officer down,
               officer needs assistance, amber alert, kidnapping, bank robbery, explosion),
              "medium" (armed robbery, death investigation, major accident, hazmat,
               structure fire, missing person, water rescue, bomb threat, pursuit),
              "low" (everything else)

Return ONLY a valid JSON array. No explanation, no markdown, no commentary.
If no valid incidents found, return exactly: []
"""


def call_claude(batch: list[dict]) -> str:
    """Send a batch of [HEARD] lines to Claude Haiku for incident extraction."""
    import anthropic
    lines_text = "\n".join(
        f"[{l['timestamp']}] {l['text']}" for l in batch
    )
    prompt = f"Analyze these Travis County radio transcription lines:\n\n{lines_text}"

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  [claude] ERROR: {e}", file=sys.stderr)
        return "[]"


def parse_llm_response(raw: str) -> list[dict]:
    """Safely parse JSON array from LLM response, handling common llama3 quirks."""
    # Strip markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    # Find first [ ... ] block
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    text = m.group()
    # Fix trailing commas before ] or } (common llama3 mistake)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Fix single-quoted strings → double-quoted
    text = re.sub(r"(?<![\\])'", '"', text)
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError as e:
        print(f"  [parse] JSON error: {e}", file=sys.stderr)
        return []


def extract_incidents(heard_lines: list[dict]) -> list[dict]:
    """Run all [HEARD] lines through llama3 in batches."""
    all_incidents = []
    total_batches = max(1, (len(heard_lines) - BATCH_OVERLAP) // (BATCH_SIZE - BATCH_OVERLAP))

    for i in range(0, len(heard_lines), BATCH_SIZE - BATCH_OVERLAP):
        batch = heard_lines[i: i + BATCH_SIZE]
        batch_num = (i // (BATCH_SIZE - BATCH_OVERLAP)) + 1
        print(f"  [llm] Batch {batch_num}/{total_batches} ({len(batch)} lines) …", end=" ", flush=True)

        raw = call_claude(batch)
        incidents = parse_llm_response(raw)

        print(f"{len(incidents)} incident(s) found")
        all_incidents.extend(incidents)

    # Deduplicate by (timestamp, address)
    seen = set()
    unique = []
    for inc in all_incidents:
        key = (inc.get("timestamp", ""), inc.get("address", "").lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(inc)

    print(f"[llm] Total: {len(unique)} unique incidents extracted")
    return unique


# ---------------------------------------------------------------------------
# 4. GEOCODING — Travis County bounded
# ---------------------------------------------------------------------------

_geocache: dict[str, tuple[float, float]] = {}


def geocode(address: str) -> tuple[float, float] | None:
    if address in _geocache:
        return _geocache[address]

    params = {
        "q":      address,
        "format": "json",
        "limit":  1,
        **AUSTIN_METRO,
    }
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "BattleBuddy/1.2"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data:
            coords = float(data[0]["lon"]), float(data[0]["lat"])
            _geocache[address] = coords
            return coords
    except Exception as e:
        print(f"  [geocode] ERROR '{address}': {e}", file=sys.stderr)
    return None


def geocode_incidents(incidents: list[dict]) -> list[dict]:
    geocoded = []
    for i, inc in enumerate(incidents, 1):
        print(f"  [{i}/{len(incidents)}] Geocoding: {inc['address']}")
        coords = geocode(inc["address"])
        if coords:
            inc["lon"], inc["lat"] = coords
            geocoded.append(inc)
            print(f"    → {coords[1]:.4f}, {coords[0]:.4f}")
        else:
            print(f"    → SKIPPED (not found in Austin Metro)")
        time.sleep(GEOCODE_DELAY)
    print(f"[geocode] {len(geocoded)}/{len(incidents)} resolved in Austin Metro")
    return geocoded


# ---------------------------------------------------------------------------
# 5. GEOJSON EXPORT
# ---------------------------------------------------------------------------

def build_geojson(incidents: list[dict]) -> dict:
    features = []
    for inc in incidents:
        sev   = inc.get("severity", "unknown").lower()
        color = SEVERITY_COLORS.get(sev, SEVERITY_COLORS["unknown"])
        itype = inc.get("type", "").lower()
        icon  = next((v for k, v in INCIDENT_ICONS.items() if k in itype), "⚠️")
        device = incident_device_name(itype)

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [inc["lon"], inc["lat"]],
            },
            "properties": {
                "title": f"{icon} {inc['type']} — {inc['severity']}",
                "description": (
                    f"<b>Address:</b> {inc['address']}<br>"
                    f"<b>Time:</b> {inc['timestamp']}<br>"
                    f"<b>Severity:</b> {inc['severity']}<br>"
                    f"<b>Device:</b> {device}"
                ),
                "timestamp":    inc["timestamp"],
                "type":         inc["type"],
                "severity":     inc["severity"],
                "address":      inc["address"],
                "device":       device,
                "marker-color": color,
                "marker-size":  "large" if sev == "high" else "medium",
            },
        })

    return {
        "type": "FeatureCollection",
        "name": f"Battle Buddy Incidents v{VERSION} — {datetime.now(timezone.utc).isoformat()}",
        "features": features,
    }


def load_existing_geojson(path: str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text())
        return data.get("features", [])
    except Exception:
        return []


def save_geojson(incidents: list[dict], out_path: str) -> None:
    existing = load_existing_geojson(out_path)
    geojson  = build_geojson(incidents)
    geojson["features"] = existing + geojson["features"]
    Path(out_path).write_text(json.dumps(geojson, indent=2, ensure_ascii=False))
    print(f"[export] {len(geojson['features'])} total features → {out_path}")
    print(f"         ({len(existing)} existing + {len(incidents)} new)")


# ---------------------------------------------------------------------------
# 6. PHONETRACK PUSH  (logGet — simple HTTP GET, per-type device name)
# ---------------------------------------------------------------------------

def build_logget_url(inc: dict, ts: int) -> str:
    """Build the PhoneTrack logGet URL for a single incident."""
    device = incident_device_name(inc.get("type", ""))
    device_encoded = urllib.parse.quote(device)

    params = urllib.parse.urlencode({
        "lat":       inc["lat"],
        "lon":       inc["lon"],
        "timestamp": ts,
        "acc":       50,          # accuracy in metres — we don't have GPS acc, use 50m
        "alt":       0,
    })

    return f"{PHONETRACK_BASE}/{PHONETRACK_TOKEN}/{device_encoded}?{params}"


def verify_last_position(device: str, expected_ts: int) -> bool:
    """
    Query PhoneTrack REST API to confirm the latest point for this device
    matches what we just pushed (within 5 seconds).
    GET /apps/phonetrack/api/getlastpositions/{token}/{device}
    """
    device_encoded = urllib.parse.quote(device)
    url = f"{PHONETRACK_API_BASE}/{PHONETRACK_TOKEN}/{device_encoded}"
    req = urllib.request.Request(url, headers={"User-Agent": "BattleBuddy/1.2"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        # Response can be a dict OR a list depending on PhoneTrack version
        items = data.values() if isinstance(data, dict) else data
        for dev_data in items:
            if isinstance(dev_data, dict):
                got_ts = dev_data.get("timestamp", 0)
                if abs(int(got_ts) - expected_ts) <= 5:
                    return True
    except Exception as e:
        print(f"    [verify] ERROR: {e}", file=sys.stderr)
    return False


def push_to_phonetrack(incidents: list[dict], verify: bool = False, db=None, db_ids: dict = None) -> None:
    # Group by device for a tidy summary line
    device_counts: dict[str, int] = {}
    for inc in incidents:
        d = incident_device_name(inc.get("type", ""))
        device_counts[d] = device_counts.get(d, 0) + 1

    print(f"[phonetrack] Pushing {len(incidents)} incidents via logGet …")
    print(f"             Devices: { {k: v for k, v in sorted(device_counts.items())} }")

    pushed = 0
    errors = 0

    for inc in incidents:
        device = incident_device_name(inc.get("type", ""))
        try:
            ts = int(datetime.fromisoformat(inc["timestamp"]).timestamp())
        except Exception:
            ts = int(time.time())

        url = build_logget_url(inc, ts)

        req = urllib.request.Request(url, headers={"User-Agent": "BattleBuddy/1.2"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                body   = resp.read().decode(errors="replace").strip()

            if status == 200:
                pushed += 1
                verified_str = ""
                if verify:
                    time.sleep(0.5)   # give server a moment to write
                    ok = verify_last_position(device, ts)
                    verified_str = " ✓ verified" if ok else " ✗ NOT verified"
                print(
                    f"  [push] {device:20s} | {inc['type']:22s} @ {inc['address']}"
                    f"  → {status}{verified_str}"
                )
                if db and db_ids:
                    row_id = db_ids.get(incidents.index(inc))
                    if row_id:
                        db.mark_pushed(row_id, device)
            else:
                errors += 1
                print(
                    f"  [push] {device:20s} | {inc['type']:22s} @ {inc['address']}"
                    f"  → HTTP {status}  body: {body[:80]}",
                    file=sys.stderr,
                )
        except Exception as e:
            errors += 1
            print(
                f"  [push] ERROR  {device} | {inc['type']} @ {inc['address']}: {e}",
                file=sys.stderr,
            )

        time.sleep(0.3)   # polite rate limiting

    print(f"[phonetrack] Done — {pushed} pushed, {errors} errors")
    if device_counts:
        print(f"             Tracks on map: {', '.join(sorted(device_counts.keys()))}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description=(
            f"Battle Buddy v{VERSION} — radio log → incidents "
            f"(Ollama llama3 + Nominatim/Austin Metro + PhoneTrack logGet)"
        )
    )
    p.add_argument("--log",    default="logs/radio_law_20260306.log", help="Radio log file")
    p.add_argument("--out",    default="logs/incidents.geojson",      help="GeoJSON output")
    p.add_argument("--push",   action="store_true",  help="Push to PhoneTrack after geocoding")
    p.add_argument("--verify", action="store_true",  help="After each push, query API to confirm point landed (requires --push)")
    p.add_argument("--reset",  action="store_true",  help="⚠️  Reprocess ENTIRE log from beginning (clears offset)")
    args = p.parse_args()

    if args.verify and not args.push:
        print("--verify requires --push")
        sys.exit(1)

    if args.reset:
        print("⚠️  WARNING: --reset will reprocess the entire log from the beginning.")
        print("   All previously processed lines will be re-extracted and re-pushed.")
        confirm = input("   Type YES to continue: ").strip()
        if confirm != "YES":
            print("Aborted.")
            sys.exit(0)
        offset_file = get_offset_path(args.log)
        if offset_file.exists():
            offset_file.unlink()
        print("[reset] Offset cleared — reprocessing full log\n")

    # Determine stream name from log filename
    log_stem = Path(args.log).stem          # e.g. radio_law_20260306
    stream = "law"
    for s in ("law", "fire", "ems"):
        if s in log_stem:
            stream = s
            break

    # Open DB if available
    db = None
    if _DB_AVAILABLE:
        db = BattleBuddyDB()
        print(f"[db] Connected → {db.db_path}")
    else:
        print("[db] battle_buddy_db.py not found — skipping DB writes (GeoJSON only)")

    # Read new lines
    heard_lines, new_offset = read_new_lines(args.log)
    if not heard_lines:
        print("No new lines to process. Use --reset to reprocess the full log.")
        sys.exit(0)

    # Write raw [HEARD] lines to DB
    if db:
        log_file = Path(args.log).name
        for line in heard_lines:
            db.insert_heard_line({**line, "stream": stream, "log_file": log_file})
        print(f"[db] {len(heard_lines)} [HEARD] lines saved")

    # LLM extraction
    print(f"\n[llm] Extracting incidents via {CLAUDE_MODEL} …")
    incidents = extract_incidents(heard_lines)
    if not incidents:
        print("No incidents extracted. Saving offset.")
        save_offset(args.log, new_offset)
        sys.exit(0)

    # Geocode (Austin Metro bounded)
    print(f"\n[geocode] Resolving {len(incidents)} addresses within Austin Metro, TX …")
    incidents = geocode_incidents(incidents)
    if not incidents:
        print("No incidents resolved within Austin Metro.")
        save_offset(args.log, new_offset)
        sys.exit(0)

    # Write incidents to DB and tag with device name + stream
    db_ids = {}   # index → db row id
    if db:
        for i, inc in enumerate(incidents):
            device = incident_device_name(inc.get("type", ""))
            row_id = db.insert_incident({**inc, "phonetrack_device": device, "stream": stream})
            db_ids[i] = row_id
        print(f"[db] {len(incidents)} incidents saved")

    # GeoJSON
    print(f"\n[export] Writing GeoJSON …")
    save_geojson(incidents, args.out)

    # PhoneTrack
    if args.push:
        print()
        push_to_phonetrack(incidents, verify=args.verify, db=db, db_ids=db_ids)
        print(f"\n✅  View tracks at http://kevcloud.ddns.net/apps/phonetrack\n")

    # DB stats
    if db:
        s = db.stats()
        print(f"[db] Total in DB — incidents: {s['incidents']}  pushed: {s['pushed']}  talkgroups: {s['talkgroups']}")
        db.close()

    # Save offset
    save_offset(args.log, new_offset)
    print(f"[state] Offset saved — next run starts at byte {new_offset}")


if __name__ == "__main__":
    main()
