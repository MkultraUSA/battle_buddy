#!/usr/bin/env python3
"""
Phase 2 — Live incident watcher for Urban Battle Buddy.
Tails a log file, geocodes new incidents, and pushes them to PhoneTrack in real time.

Usage:
    cd ~/battle_buddy
    python incident_watcher_v1.1.py --log logs/incidents.log

PhoneTrack endpoint: https://kevcloud.ddns.net/apps/phonetrack/log/owntracks/23b91519e13f254f4fecb9a6802f6cc5/Urban Battle Buddy
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# Same pattern as incident_to_geojson.py — adjust to match your log format
LOG_PATTERN = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r".*?\[INCIDENT\]"
    r".*?TYPE=['\"](?P<type>[^'\"]+)['\"]"
    r".*?ADDRESS=['\"](?P<address>[^'\"]+)['\"]"
    r"(?:.*?SEVERITY=['\"](?P<severity>[^'\"]+)['\"])?",
    re.IGNORECASE,
)

_geocache: dict[str, tuple[float, float]] = {}


def geocode(address: str) -> tuple[float, float] | None:
    if address in _geocache:
        return _geocache[address]
    url = (
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode({"q": address, "format": "json", "limit": 1})
    )
    req = urllib.request.Request(url, headers={"User-Agent": "IncidentWatcher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data:
            coords = float(data[0]["lon"]), float(data[0]["lat"])
            _geocache[address] = coords
            return coords
    except Exception as e:
        print(f"[geocode] ERROR '{address}': {e}", file=sys.stderr)
    return None


PHONETRACK_URL = (
    "https://kevcloud.ddns.net/apps/phonetrack/log/owntracks"
    "/23b91519e13f254f4fecb9a6802f6cc5/Urban%20Battle%20Buddy"
)


def push_phonetrack(inc: dict, **kwargs) -> None:
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
            print(f"  [push] {inc['type']} | {inc['address']} → HTTP {resp.status}")
    except Exception as e:
        print(f"  [push] ERROR: {e}", file=sys.stderr)


def tail(log_path: str, poll_interval: float = 2.0):
    """Generator that yields new lines appended to a file (like `tail -f`)."""
    with open(log_path, "r", encoding="utf-8") as f:
        f.seek(0, 2)  # seek to end
        print(f"[watcher] Monitoring {log_path} (press Ctrl-C to stop)")
        while True:
            line = f.readline()
            if line:
                yield line.strip()
            else:
                time.sleep(poll_interval)


def watch(log_path: str):
    for line in tail(log_path):
        m = LOG_PATTERN.search(line)
        if not m:
            continue

        inc = {
            "timestamp": m.group("timestamp"),
            "type":      m.group("type"),
            "address":   m.group("address"),
            "severity":  m.group("severity") or "Unknown",
        }
        print(f"\n[new] {inc['timestamp']} — {inc['type']} @ {inc['address']}")

        time.sleep(1.1)  # Nominatim rate limit
        coords = geocode(inc["address"])
        if not coords:
            print("  → geocode failed, skipping")
            continue

        inc["lon"], inc["lat"] = coords
        push_phonetrack(inc)


def main():
    p = argparse.ArgumentParser(description="Live incident watcher → PhoneTrack (Urban Battle Buddy)")
    p.add_argument("--log", default="logs/incidents.log", help="Log file to tail")
    args = p.parse_args()

    try:
        watch(args.log)
    except KeyboardInterrupt:
        print("\n[watcher] Stopped.")


if __name__ == "__main__":
    main()
