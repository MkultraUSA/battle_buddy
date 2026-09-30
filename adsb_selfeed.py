#!/usr/bin/env python3
"""Self-feed ADSB.lol radius snapshot into the local ingest endpoint.

Keeps /api/adsb/live fresh without feeder hardware. Token stays in
/opt/battlebuddy/.env; the ingest response is printed, and nothing secret is.

TARGETING. `/api/adsb/live` does not read a database. It returns
`modules.aircraft._snapshot`, a module-level dict that lives in ONE process's
memory, and `POST /api/adsb/ingest` is the only thing that writes it. So the map
renders only when the feeder posts to the same process nginx proxies the live
endpoint to. Both sides must be pointed at the main app: this feeder here, and
`location /api/adsb/` in nginx (which is not in this repo -- it is VPS-only, at
/etc/nginx/sites-enabled/battlebuddy).

This used to point at port 5000, a second Flask app (`app.py`) that registered
`aircraft_bp` as well, so the feeder filled THAT process's snapshot while the
main app reported `stale: true, aircraft: []` forever.

Precedence: `load_env` uses `setdefault`, so an already-exported environment
variable beats /opt/battlebuddy/.env. Under cron the environment is near-empty,
so .env normally wins, but a stale export in the crontab would silently take
priority over the file.
"""
import json
import os
import urllib.request

ENV_FILE = "/opt/battlebuddy/.env"

# Read at call time, not import time: main() calls load_env() first, so a
# BB_ADSB_INGEST_URL set in .env actually takes effect. A module constant
# evaluated at import would silently ignore it.
_DEFAULT_INGEST_URL = "http://127.0.0.1:9001/api/adsb/ingest"


def ingest_url() -> str:
    return os.environ.get("BB_ADSB_INGEST_URL", _DEFAULT_INGEST_URL)


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
        ingest_url(),
        data=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode()[:120])


if __name__ == "__main__":
    main()
