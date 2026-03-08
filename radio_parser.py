#!/usr/bin/env python3
"""
Battle Buddy — Radio Log → Incident Parser
Uses local Ollama (llama3) to extract incidents from raw radio transcription.
Incremental: only processes new lines since last run via logs/.processed_offset

Usage:
    cd ~/battle_buddy
    python3 radio_parser.py --log logs/radio_20260304.log
    python3 radio_parser.py --log logs/radio_20260304.log --push   # also push to PhoneTrack
    python3 radio_parser.py --log logs/radio_20260304.log --out logs/incidents.geojson

Pipeline:
    [HEARD] lines → llama3 (Ollama) → structured incidents
                  → Nominatim geocoder (fully resolvable addresses only)
                  → GeoJSON file  +  optional PhoneTrack push
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

OLLAMA_URL      = "http://localhost:11434/api/generate"
OLLAMA_MODEL    = "llama3"
BATCH_SIZE      = 10          # [HEARD] lines per LLM call
BATCH_OVERLAP   = 2           # lines of overlap between batches (context continuity)
GEOCODE_DELAY   = 1.1         # seconds between Nominatim requests

PHONETRACK_URL  = (
    "https://kevcloud.ddns.net/apps/phonetrack/log/owntracks"
    "/23b91519e13f254f4fecb9a6802f6cc5/Urban%20Battle%20Buddy"
)

SEVERITY_COLORS = {
    "high":    "#e63946",
    "medium":  "#f4a261",
    "low":     "#2a9d8f",
    "unknown": "#adb5bd",
}

INCIDENT_ICONS = {
    "welfare check":      "🏥",
    "collision":          "🚗",
    "accident":           "🚗",
    "suspicious person":  "🚔",
    "suspicious vehicle": "🚔",
    "arrest":             "🚔",
    "warrant":            "🚔",
    "disturbance":        "⚠️",
    "fight":              "⚠️",
    "fire":               "🔥",
    "medical":            "🏥",
    "mental health":      "🏥",
    "theft":              "🚔",
    "burglary":           "🚔",
    "trespass":           "🚔",
}

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

SYSTEM_PROMPT = """You are a police dispatch analyst. You will receive a batch of raw radio transcription lines from a police scanner.

Your job is to identify ONLY lines that contain a dispatchable incident with a COMPLETE street address (must include a street number AND a street name, e.g. "1500 West 71st Street" or "601 East Main Street").

For each incident found, return a JSON array. Each element must have exactly these fields:
- "timestamp": copy from the line (format: YYYY-MM-DD HH:MM:SS)
- "type": incident type in plain English (e.g. "welfare check", "collision", "suspicious person", "arrest", "disturbance", "mental health", "theft")
- "address": the most complete street address you can assemble from context (must have number + street)
- "severity": "high", "medium", or "low" based on urgency

Rules:
- SKIP lines with no actionable address (e.g. "Copy", "10-4", unit chatter, partial addresses like "29th Street" with no number)
- SKIP lines that are clearly just acknowledgements or radio noise
- Piece together addresses that span multiple consecutive lines
- Return ONLY valid JSON array, no explanation, no markdown, no commentary
- If no incidents found, return exactly: []
"""


def call_ollama(batch: list[dict]) -> str:
    """Send a batch of [HEARD] lines to local llama3 via Ollama."""
    lines_text = "\n".join(
        f"[{l['timestamp']}] {l['text']}" for l in batch
    )
    prompt = f"Analyze these radio transcription lines:\n\n{lines_text}"

    payload = json.dumps({
        "model":  OLLAMA_MODEL,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "options": {"temperature": 0.1},  # low temp for consistent structured output
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data.get("response", "[]").strip()
    except Exception as e:
        print(f"  [ollama] ERROR: {e}", file=sys.stderr)
        return "[]"


def parse_llm_response(raw: str) -> list[dict]:
    """Safely parse JSON array from LLM response."""
    # Strip markdown fences if model added them anyway
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    # Find first [ ... ] block
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group())
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

        raw = call_ollama(batch)
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
# 4. GEOCODING
# ---------------------------------------------------------------------------

_geocache: dict[str, tuple[float, float]] = {}


def geocode(address: str) -> tuple[float, float] | None:
    if address in _geocache:
        return _geocache[address]
    url = (
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode({"q": address, "format": "json", "limit": 1})
    )
    req = urllib.request.Request(url, headers={"User-Agent": "BattleBuddy/1.0"})
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
        else:
            print(f"    → SKIPPED (no geocode result)")
        time.sleep(GEOCODE_DELAY)
    print(f"[geocode] {len(geocoded)}/{len(incidents)} resolved")
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
                    f"<b>Severity:</b> {inc['severity']}"
                ),
                "timestamp":    inc["timestamp"],
                "type":         inc["type"],
                "severity":     inc["severity"],
                "address":      inc["address"],
                "marker-color": color,
                "marker-size":  "large" if sev == "high" else "medium",
            },
        })

    return {
        "type": "FeatureCollection",
        "name": f"Battle Buddy Incidents — {datetime.utcnow().isoformat()}Z",
        "features": features,
    }


def load_existing_geojson(path: str) -> list[dict]:
    """Load existing GeoJSON features so we can append rather than overwrite."""
    try:
        data = json.loads(Path(path).read_text())
        return data.get("features", [])
    except Exception:
        return []


def save_geojson(incidents: list[dict], out_path: str) -> None:
    existing = load_existing_geojson(out_path)
    geojson  = build_geojson(incidents)

    # Merge — existing features first, new ones appended
    geojson["features"] = existing + geojson["features"]
    Path(out_path).write_text(json.dumps(geojson, indent=2, ensure_ascii=False))
    print(f"[export] {len(geojson['features'])} total features → {out_path}")
    print(f"         ({len(existing)} existing + {len(incidents)} new)")


# ---------------------------------------------------------------------------
# 6. PHONETRACK PUSH
# ---------------------------------------------------------------------------

def push_to_phonetrack(incidents: list[dict]) -> None:
    print(f"[phonetrack] Pushing {len(incidents)} incidents as 'Urban Battle Buddy' …")
    for inc in incidents:
        try:
            ts = int(datetime.fromisoformat(inc["timestamp"]).timestamp())
        except Exception:
            ts = int(time.time())

        payload = json.dumps({
            "_type": "location",
            "lat":   inc["lat"],
            "lon":   inc["lon"],
            "tst":   ts,
            "desc":  f"{inc['type']} — {inc['severity']}",
        }).encode()

        req = urllib.request.Request(
            PHONETRACK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"  [push] {inc['type']} @ {inc['address']} → HTTP {resp.status}")
        except Exception as e:
            print(f"  [push] ERROR: {e}", file=sys.stderr)

        time.sleep(0.2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Battle Buddy radio log → incidents (Ollama llama3 + Nominatim)"
    )
    p.add_argument("--log",  default="logs/radio_20260304.log", help="Radio log file")
    p.add_argument("--out",  default="logs/incidents.geojson",  help="GeoJSON output")
    p.add_argument("--push", action="store_true",               help="Push to PhoneTrack")
    p.add_argument("--reset", action="store_true",              help="Reprocess entire log (ignore offset)")
    args = p.parse_args()

    if args.reset:
        offset_file = get_offset_path(args.log)
        if offset_file.exists():
            offset_file.unlink()
        print("[reset] Cleared offset — will reprocess full log")

    # Read new lines
    heard_lines, new_offset = read_new_lines(args.log)
    if not heard_lines:
        print("No new lines to process. Use --reset to reprocess the full log.")
        sys.exit(0)

    # LLM extraction
    print(f"\n[llm] Extracting incidents via {OLLAMA_MODEL} …")
    incidents = extract_incidents(heard_lines)
    if not incidents:
        print("No incidents extracted. Saving offset.")
        save_offset(args.log, new_offset)
        sys.exit(0)

    # Geocode
    print(f"\n[geocode] Resolving {len(incidents)} addresses …")
    incidents = geocode_incidents(incidents)
    if not incidents:
        print("No geocodable incidents found.")
        save_offset(args.log, new_offset)
        sys.exit(0)

    # GeoJSON
    print(f"\n[export] Writing GeoJSON …")
    save_geojson(incidents, args.out)
    print(f"\n✅  Import {args.out} into Nextcloud Maps:")
    print(f"    http://kevcloud.ddns.net → Maps → ⋮ → Import\n")

    # PhoneTrack
    if args.push:
        print()
        push_to_phonetrack(incidents)
        print(f"\n✅  Check PhoneTrack at http://kevcloud.ddns.net\n")

    # Save offset so next run only processes new lines
    save_offset(args.log, new_offset)
    print(f"[state] Offset saved — next run will start from byte {new_offset}")


if __name__ == "__main__":
    main()
