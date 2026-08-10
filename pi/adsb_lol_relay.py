#!/usr/bin/env python3
"""Relay ADSB.lol feeder-only data to the Battle Buddy aircraft module."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_SOURCE_URL = "https://re-api.adsb.lol?circle=30.2672,-97.7431,52"
DEFAULT_INTERVAL_SECONDS = 30.0
USER_AGENT = "BattleBuddy-ADSB-Relay/1.0"

logger = logging.getLogger("bb-adsb-relay")


def _request_json(request: urllib.request.Request, timeout: float = 20.0) -> Any:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_snapshot(source_url: str) -> tuple[dict[str, Any], int]:
    """Fetch one feeder-only snapshot and return its relay payload and source count."""
    request = urllib.request.Request(
        source_url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    data = _request_json(request)
    if not isinstance(data, dict):
        raise ValueError("ADSB.lol returned a non-object response")

    source_aircraft = data.get("ac")
    if not isinstance(source_aircraft, list):
        source_aircraft = data.get("aircraft")
    if not isinstance(source_aircraft, list):
        raise ValueError("ADSB.lol response did not include an aircraft list")

    aircraft = [
        item
        for item in source_aircraft
        if isinstance(item, dict) and item.get("lat") is not None and item.get("lon") is not None
    ]
    return {"now": data.get("now", time.time()), "aircraft": aircraft}, len(source_aircraft)


def post_snapshot(ingest_url: str, ingest_token: str, snapshot: dict[str, Any]) -> int:
    """Post one snapshot and return the server-accepted aircraft count."""
    body = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        ingest_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {ingest_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    response = _request_json(request)
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise ValueError("Battle Buddy rejected the ADS-B snapshot")
    return int(response.get("aircraft", 0))


def relay_once(source_url: str, ingest_url: str, ingest_token: str) -> None:
    snapshot, source_count = fetch_snapshot(source_url)
    accepted = post_snapshot(ingest_url, ingest_token, snapshot)
    logger.info("relayed=%d source=%d", accepted, source_count)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    source_url = os.environ.get("BB_ADSB_SOURCE_URL", DEFAULT_SOURCE_URL)
    ingest_url = os.environ.get("BB_ADSB_INGEST_URL", "").strip()
    ingest_token = os.environ.get("BB_ADSB_INGEST_TOKEN", "").strip()
    interval = max(
        10.0,
        float(os.environ.get("BB_ADSB_RELAY_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
    )

    if not ingest_url or not ingest_token:
        raise SystemExit("BB_ADSB_INGEST_URL and BB_ADSB_INGEST_TOKEN are required")

    while True:
        started = time.monotonic()
        try:
            relay_once(source_url, ingest_url, ingest_token)
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            logger.warning("relay failed: %s", exc)
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, interval - elapsed))


if __name__ == "__main__":
    main()
