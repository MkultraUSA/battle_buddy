#!/usr/bin/env python3
"""
Battle Buddy — IPN (Incident Page Network) Poller  v1.0

Polls the Broadcastify IPN proxy for Travis County (ctid=2749), geocodes
new incidents, and inserts them into the Battle Buddy DB with stream='ipn'.

IPN data is delayed up to 2 hours for the public.  Incidents have city-level
location only (no street address), so they are geocoded to city centroids and
used to confirm / cross-reference radio-parser incidents on the heatmap.

Designed to run from run_parser.sh (every 30 min):
    python3 ipn_poller.py >> logs/ipn_poller.log 2>&1

Standalone:
    python3 ipn_poller.py [--db logs/battle_buddy.db] [--dry-run]
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from battle_buddy_db import BattleBuddyDB

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IPN_URL  = (
    "https://www.broadcastify.com/scripts/ajax/ipnProxy.php"
    "?ctid=2749&uid=0&callback=cb"
)
DB_PATH  = "logs/battle_buddy.db"
LOG      = "[ipn]"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0",
    "Accept":          "*/*",
    "Referer":         "https://www.broadcastify.com/listen/ctid/2749",
    "X-Requested-With": "XMLHttpRequest",
}

# Austin Metro Nominatim bounding box (matches radio_parser.py)
AUSTIN_METRO = {
    "countrycodes": "us",
    "viewbox":      "-98.4000,29.8000,-97.0000,30.9000",
    "bounded":      "1",
}

# Keyword → severity mapping
HIGH_KEYWORDS   = ["shot", "shooting", "robbery", "pursuit", "swat", "officer down",
                   "structure fire", "house fire", "explosion", "hostage", "stabbing"]
MEDIUM_KEYWORDS = ["assault", "disturbance", "fight", "accident", "collision",
                   "medical", "overdose", "welfare", "fire alarm", "burglary"]


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

_geocache: dict[str, tuple[float, float]] = {}


def geocode(city: str) -> tuple[float, float] | None:
    query = f"{city}, Travis County, TX"
    if query in _geocache:
        return _geocache[query]
    params = {"q": query, "format": "json", "limit": 1, **AUSTIN_METRO}
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "BattleBuddy/0.8.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data:
            coords = (float(data[0]["lat"]), float(data[0]["lon"]))
            _geocache[query] = coords
            return coords
    except Exception as e:
        print(f"{LOG} geocode error '{city}': {e}", flush=True)
    return None


# ---------------------------------------------------------------------------
# IPN fetch + parse
# ---------------------------------------------------------------------------

def fetch_ipn() -> list[dict]:
    """Return list of raw incident dicts from the IPN JSONP endpoint."""
    try:
        req = urllib.request.Request(IPN_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as e:
        print(f"{LOG} fetch error: {e}", flush=True)
        return []

    if not raw:
        print(f"{LOG} empty response — no IPN incidents active for Travis County", flush=True)
        return []

    # Strip JSONP wrapper: cb({...}) or cb([...])
    m = re.match(r"^[^(]*\((.*)\)\s*;?\s*$", raw, re.DOTALL)
    if not m:
        print(f"{LOG} unexpected format: {repr(raw[:120])}", flush=True)
        return []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"{LOG} JSON parse error: {e}", flush=True)
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("incidents") or data.get("data") or []
    return []


# ---------------------------------------------------------------------------
# Severity classifier
# ---------------------------------------------------------------------------

def classify_severity(itype: str, desc: str) -> str:
    text = (itype + " " + desc).lower()
    if any(k in text for k in HIGH_KEYWORDS):
        return "high"
    if any(k in text for k in MEDIUM_KEYWORDS):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Battle Buddy IPN Poller")
    ap.add_argument("--db",      default=DB_PATH, help="DB path")
    ap.add_argument("--dry-run", action="store_true", help="Parse but do not write to DB")
    args = ap.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{LOG} {now} — polling IPN ctid=2749", flush=True)

    items = fetch_ipn()
    if not items:
        print(f"{LOG} 0 incidents in feed — nothing to do", flush=True)
        return

    print(f"{LOG} {len(items)} incident(s) in feed", flush=True)

    db = BattleBuddyDB(args.db)
    imported = skipped = 0

    for item in items:
        # Normalise field names — IPN field names are not publicly documented
        # so we check common variants
        ipn_id   = str(item.get("id") or item.get("incidentId") or "").strip()
        itype    = (item.get("type") or item.get("incidentType") or "unknown").strip()
        desc     = (item.get("description") or item.get("desc") or "").strip()
        city     = (item.get("city") or item.get("cityName") or "Austin").strip()
        freq     = (item.get("frequency") or item.get("freq") or "").strip()
        dispatch = (item.get("dispatcher") or item.get("agency") or "IPN").strip()
        ts_raw   = item.get("dateTime") or item.get("timestamp") or item.get("ts") or ""

        # Parse timestamp
        try:
            if str(ts_raw).isdigit():
                ts = datetime.fromtimestamp(int(ts_raw)).strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts = str(ts_raw).strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Deduplicate by IPN id
        if ipn_id:
            row = db.conn.execute(
                "SELECT id FROM incidents WHERE ipn_id=? LIMIT 1", (ipn_id,)
            ).fetchone()
            if row:
                skipped += 1
                continue

        # Build address — IPN gives city only, not street
        address = f"{city}, Travis County, TX"

        # Geocode to city centroid
        coords = geocode(city)
        lat = coords[0] if coords else None
        lon = coords[1] if coords else None
        time.sleep(1.1)  # Nominatim 1 req/sec limit

        severity = classify_severity(itype, desc)

        # Build talkgroup_raw from frequency or dispatcher
        tg_raw = freq if freq else dispatch

        print(
            f"{LOG}  [{severity:6s}] {itype} — {city}  ({ts})"
            + (f"  freq={freq}" if freq else ""),
            flush=True,
        )

        if args.dry_run:
            continue

        inc = {
            "timestamp":     ts,
            "type":          itype,
            "address":       address,
            "severity":      severity,
            "lat":           lat,
            "lon":           lon,
            "talkgroup_raw": tg_raw,
            "stream":        "ipn",
        }
        inc_id = db.insert_incident(inc)

        if ipn_id:
            db.conn.execute(
                "UPDATE incidents SET ipn_id=? WHERE id=?", (ipn_id, inc_id)
            )
            db.conn.commit()

        imported += 1

    print(
        f"{LOG} done — {imported} imported, {skipped} skipped (already in DB)",
        flush=True,
    )
    db.close()


if __name__ == "__main__":
    main()
