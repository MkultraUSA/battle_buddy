#!/usr/bin/env python3
"""Self-feed ADSB.lol radius snapshot into the local ingest endpoint.

Keeps /api/adsb/live fresh without feeder hardware. Token stays in
/opt/battlebuddy/.env; nothing is printed.
"""
import json
import os
import urllib.request

ENV_FILE = "/opt/battlebuddy/.env"


def load_env(path: str) -> None:
    try:
        with open(path) as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)
    except OSError:
        pass


def main() -> None:
    load_env(ENV_FILE)
    token = os.environ.get("BB_ADSB_INGEST_TOKEN", "")
    if not token:
        raise SystemExit("no ingest token")
    with urllib.request.urlopen(
        "https://api.adsb.lol/v2/lat/30.2672/lon/-97.7431/dist/100", timeout=25
    ) as resp:
        snap = json.load(resp)
    payload = json.dumps(
        {"now": (snap.get("now") or 0) / 1000, "aircraft": snap.get("ac", [])}
    ).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:5000/api/adsb/ingest",
        data=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode()[:120])


if __name__ == "__main__":
    main()
