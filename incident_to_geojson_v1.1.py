#!/usr/bin/env python3
"""
Incident Log → GeoJSON pipeline for Nextcloud Maps
Phase 1: Static export  |  Phase 2: Live PhoneTrack push

Usage:
    cd ~/battle_buddy

    # Phase 1 — demo
    python incident_to_geojson_v1.1.py --demo

    # Phase 1 — real log
    python incident_to_geojson_v1.1.py --log logs/incidents.log --out logs/incidents.geojson

    # Phase 2 — bulk push to PhoneTrack
    python incident_to_geojson_v1.1.py --log logs/incidents.log --live
"""

import argparse
import json
import re
import time
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. LOG PARSING
# ---------------------------------------------------------------------------

# Adjust this regex to match YOUR actual log format.
# Default pattern matches lines like:
#   2024-03-05 14:32:11 [INCIDENT] TYPE="Fire" ADDRESS="123 Main St, Springfield" SEVERITY="High"
LOG_PATTERN = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r".*?\[INCIDENT\]"
    r".*?TYPE=['\"](?P<type>[^'\"]+)['\"]"
    r".*?ADDRESS=['\"](?P<address>[^'\"]+)['\"]"
    r"(?:.*?SEVERITY=['\"](?P<severity>[^'\"]+)['\"])?",
    re.IGNORECASE,
)


def parse_log(log_path: str) -> list[dict]:
    """Extract incident records from a log file."""
    incidents = []
    with open(log_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            m = LOG_PATTERN.search(line)
            if m:
                incidents.append(
                    {
                        "lineno": lineno,
                        "timestamp": m.group("timestamp"),
                        "type": m.group("type"),
                        "address": m.group("address"),
                        "severity": m.group("severity") or "Unknown",
                        "raw": line.strip(),
                    }
                )
    print(f"[parse] Found {len(incidents)} incidents in {log_path}")
    return incidents


# ---------------------------------------------------------------------------
# 2. GEOCODING  (Nominatim / OpenStreetMap — no API key required)
# ---------------------------------------------------------------------------

def geocode_nominatim(address: str) -> tuple[float, float] | None:
    """
    Geocode an address via Nominatim (OSM).
    Returns (longitude, latitude) or None on failure.
    Free but rate-limited to ~1 req/sec — do NOT hammer it in production.
    Replace with a commercial geocoder for high volume.
    """
    import urllib.request
    import urllib.parse

    url = (
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode({"q": address, "format": "json", "limit": 1})
    )
    req = urllib.request.Request(url, headers={"User-Agent": "IncidentMapper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data:
            return float(data[0]["lon"]), float(data[0]["lat"])
    except Exception as e:
        print(f"  [geocode] ERROR for '{address}': {e}", file=sys.stderr)
    return None


def geocode_incidents(incidents: list[dict], delay: float = 1.1) -> list[dict]:
    """Add lon/lat to each incident. Skips failures gracefully."""
    geocoded = []
    for i, inc in enumerate(incidents, 1):
        print(f"  [{i}/{len(incidents)}] Geocoding: {inc['address']}")
        coords = geocode_nominatim(inc["address"])
        if coords:
            inc["lon"], inc["lat"] = coords
            geocoded.append(inc)
        else:
            print(f"    → SKIPPED (no result)", file=sys.stderr)
        time.sleep(delay)  # respect Nominatim rate limit
    print(f"[geocode] {len(geocoded)}/{len(incidents)} successfully geocoded")
    return geocoded


# ---------------------------------------------------------------------------
# 3. GEOJSON EXPORT
# ---------------------------------------------------------------------------

SEVERITY_COLORS = {
    "high":     "#e63946",
    "medium":   "#f4a261",
    "low":      "#2a9d8f",
    "unknown":  "#adb5bd",
}

INCIDENT_ICONS = {
    "fire":       "🔥",
    "flood":      "💧",
    "accident":   "🚗",
    "crime":      "🚔",
    "medical":    "🏥",
    "hazmat":     "☣️",
}


def build_geojson(incidents: list[dict]) -> dict:
    """Convert geocoded incidents to a GeoJSON FeatureCollection."""
    features = []
    for inc in incidents:
        sev_key = inc["severity"].lower()
        color = SEVERITY_COLORS.get(sev_key, SEVERITY_COLORS["unknown"])
        icon = INCIDENT_ICONS.get(inc["type"].lower(), "⚠️")

        feature = {
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
                "timestamp": inc["timestamp"],
                "type": inc["type"],
                "severity": inc["severity"],
                "address": inc["address"],
                # Nextcloud Maps uses 'marker-color' from simplestyle-spec
                "marker-color": color,
                "marker-size": "medium" if sev_key != "high" else "large",
            },
        }
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "name": f"Incidents — exported {datetime.utcnow().isoformat()}Z",
        "features": features,
    }


def save_geojson(geojson: dict, out_path: str) -> None:
    Path(out_path).write_text(json.dumps(geojson, indent=2, ensure_ascii=False))
    print(f"[export] Saved {len(geojson['features'])} features → {out_path}")


# ---------------------------------------------------------------------------
# 4. PHASE 2 — LIVE PHONETRACK PUSH (OwnTracks endpoint)
# ---------------------------------------------------------------------------
# Endpoint: POST https://kevcloud.ddns.net/apps/phonetrack/log/owntracks/<token>/<device>
# Device name: Urban Battle Buddy
# Token: 23b91519e13f254f4fecb9a6802f6cc5

PHONETRACK_URL = (
    "https://kevcloud.ddns.net/apps/phonetrack/log/owntracks"
    "/23b91519e13f254f4fecb9a6802f6cc5/Urban%20Battle%20Buddy"
)


def push_to_phonetrack(incidents: list[dict], **kwargs) -> None:
    """
    Push geocoded incidents to PhoneTrack via OwnTracks endpoint.
    All incidents post as device 'Urban Battle Buddy'.
    """
    import urllib.request

    for inc in incidents:
        ts = int(datetime.fromisoformat(inc["timestamp"]).timestamp())
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
                print(f"  [phonetrack] {inc['type']} @ {inc['address']} → HTTP {resp.status}")
        except Exception as e:
            print(f"  [phonetrack] ERROR: {e}", file=sys.stderr)

        time.sleep(0.2)


# ---------------------------------------------------------------------------
# 5. DEMO / SAMPLE DATA (no log file needed for quick test)
# ---------------------------------------------------------------------------

SAMPLE_LOG = """\
2024-03-05 08:12:44 [INCIDENT] TYPE="Fire" ADDRESS="Brandenburger Tor, Berlin, Germany" SEVERITY="High"
2024-03-05 09:30:02 [INCIDENT] TYPE="Accident" ADDRESS="Alexanderplatz, Berlin, Germany" SEVERITY="Medium"
2024-03-05 11:05:18 [INCIDENT] TYPE="Medical" ADDRESS="Checkpoint Charlie, Berlin, Germany" SEVERITY="Low"
2024-03-05 13:22:55 [INCIDENT] TYPE="Crime" ADDRESS="Potsdamer Platz, Berlin, Germany" SEVERITY="Medium"
"""


def write_sample_log(path: str = "sample_incidents.log") -> str:
    Path(path).write_text(SAMPLE_LOG)
    print(f"[demo] Wrote sample log → {path}")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Incident log → Nextcloud Maps GeoJSON / PhoneTrack")
    parser.add_argument("--log",        help="Path to incident log file")
    parser.add_argument("--out",        default="logs/incidents.geojson", help="Output GeoJSON path")
    parser.add_argument("--demo",       action="store_true", help="Use built-in sample data")
    parser.add_argument("--live",       action="store_true", help="Push to PhoneTrack (Phase 2)")
    parser.add_argument("--no-geocode", action="store_true", help="Skip geocoding")
    args = parser.parse_args()

    # Resolve log source
    if args.demo:
        log_path = write_sample_log()
    elif args.log:
        log_path = args.log
    else:
        parser.error("Provide --log <path> or --demo")

    # Parse
    incidents = parse_log(log_path)
    if not incidents:
        print("No incidents matched the log pattern. Check LOG_PATTERN in the script.")
        sys.exit(1)

    # Geocode
    if not args.no_geocode:
        incidents = geocode_incidents(incidents)

    if not incidents:
        print("No geocodable incidents found.")
        sys.exit(1)

    # Phase 1: GeoJSON
    geojson = build_geojson(incidents)
    save_geojson(geojson, args.out)
    print(f"\n✅  Import {args.out} into Nextcloud Maps via:")
    print(f"    Maps → ⋮ → Import → select {args.out}\n")

    # Phase 2: PhoneTrack live push
    if args.live:
        print("[live] Pushing to PhoneTrack as 'Urban Battle Buddy' …")
        push_to_phonetrack(incidents)
        print("\n✅  Check PhoneTrack in Nextcloud Maps at http://kevcloud.ddns.net")


if __name__ == "__main__":
    main()
