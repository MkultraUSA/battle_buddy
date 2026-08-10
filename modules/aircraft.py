"""Aircraft API and public map routes.

This module keeps ADS-B ingestion, validation, snapshot state, local helicopter
trails, and the aircraft map out of the audio/transcription entrypoint.
"""

from __future__ import annotations

import hmac
import os
import sqlite3
import threading
import time
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from modules.config import DB_PATH
from modules.pollers.impl.adsb_air_asset import ADSB_TRAIL_SECS, KNOWN_AIR_ASSETS

aircraft_bp = Blueprint("aircraft", __name__)

_MAX_AIRCRAFT = 300
_MAX_AGE_SECONDS = 120
_MAX_SNAPSHOT_BYTES = 1_000_000
_AUSTIN_LAT_BOUNDS = (29.85, 30.70)
_AUSTIN_LON_BOUNDS = (-98.25, -97.25)

_snapshot_lock = threading.Lock()
_snapshot: dict[str, Any] = {
    "now": 0.0,
    "received_at": 0.0,
    "aircraft": [],
}


def _number(value: Any, minimum: float | None = None, maximum: float | None = None):
    """Return a bounded float for an ADS-B value, or ``None``."""
    if value is None or value == "ground":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _text(value: Any, max_length: int = 32) -> str:
    """Normalize untrusted ADS-B text before returning it to browsers."""
    return str(value or "").strip()[:max_length]


def _sanitize_aircraft(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    lat = _number(raw.get("lat"), *_AUSTIN_LAT_BOUNDS)
    lon = _number(raw.get("lon"), *_AUSTIN_LON_BOUNDS)
    icao24 = _text(raw.get("hex"), 16).lower()
    if lat is None or lon is None or not icao24:
        return None

    db_flags = int(_number(raw.get("dbFlags"), 0, 65535) or 0)
    known_label, known_leo = KNOWN_AIR_ASSETS.get(icao24, ("", False))
    squawk = _text(raw.get("squawk"), 8)
    emergency = _text(raw.get("emergency"), 24)
    aircraft_type = _text(raw.get("t"), 12).upper()
    category = _text(raw.get("category"), 8).upper()

    return {
        "hex": icao24,
        "flight": _text(raw.get("flight"), 16),
        "registration": _text(raw.get("r"), 16),
        "aircraft_type": aircraft_type,
        "category": category,
        "lat": lat,
        "lon": lon,
        "alt_baro": _number(raw.get("alt_baro"), -2000, 100000),
        "alt_geom": _number(raw.get("alt_geom"), -2000, 100000),
        "gs": _number(raw.get("gs"), 0, 2000),
        "track": _number(raw.get("track"), 0, 360),
        "baro_rate": _number(raw.get("baro_rate"), -20000, 20000),
        "squawk": squawk,
        "emergency": emergency,
        "seen": _number(raw.get("seen"), 0, 600),
        "seen_pos": _number(raw.get("seen_pos"), 0, 600),
        "db_flags": db_flags,
        "is_military": bool(db_flags & 1),
        "is_interesting": bool(db_flags & 2),
        "is_pia": bool(db_flags & 4),
        "is_ladd": bool(db_flags & 8),
        "is_helicopter": aircraft_type.startswith("H") or category == "A7",
        "is_known_public_safety": bool(known_label),
        "is_known_leo": bool(known_leo),
        "known_label": known_label,
        "is_emergency": bool(
            emergency and emergency.lower() not in {"none", "no emergency"}
        )
        or squawk in {"7500", "7600", "7700"},
    }


@aircraft_bp.route("/api/adsb")
def local_aircraft():
    """Return locally tracked helicopters with their 30-minute trails."""
    cutoff = time.time() - ADSB_TRAIL_SECS
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM aircraft_positions WHERE ts > ? ORDER BY icao24, ts",
        (cutoff,),
    ).fetchall()
    conn.close()

    aircraft: dict[str, dict[str, Any]] = {}
    for row in rows:
        icao24 = row["icao24"]
        current = aircraft.setdefault(
            icao24,
            {
                "icao24": icao24,
                "label": row["label"],
                "callsign": row["callsign"],
                "is_leo": bool(row["is_leo"]),
                "trail": [],
            },
        )
        current.update(
            {
                "lat": row["lat"],
                "lon": row["lon"],
                "alt_ft": row["alt_ft"],
                "heading": row["heading"],
                "speed_kts": row["speed_kts"],
                "ts": row["ts"],
            }
        )
        current["trail"].append([row["lat"], row["lon"], row["ts"]])

    return jsonify(list(aircraft.values()))


@aircraft_bp.route("/api/adsb/ingest", methods=["POST"])
def ingest_aircraft():
    """Accept a bounded network-wide snapshot from the authorized feeder Pi."""
    ingest_token = os.environ.get("BB_ADSB_INGEST_TOKEN", "")
    if not ingest_token:
        return jsonify({"error": "ADSB ingest is not configured"}), 503

    supplied = request.headers.get("Authorization", "")
    if supplied.startswith("Bearer "):
        supplied = supplied[7:].strip()
    if not supplied or not hmac.compare_digest(supplied, ingest_token):
        return jsonify({"error": "unauthorized"}), 401

    if request.content_length and request.content_length > _MAX_SNAPSHOT_BYTES:
        return jsonify({"error": "snapshot too large"}), 413

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("aircraft"), list):
        return jsonify({"error": "aircraft list required"}), 400

    sanitized = []
    for raw in payload["aircraft"][:_MAX_AIRCRAFT]:
        aircraft = _sanitize_aircraft(raw)
        if aircraft is not None:
            sanitized.append(aircraft)

    received_at = time.time()
    source_now = _number(payload.get("now"), 0) or received_at
    with _snapshot_lock:
        _snapshot.update(
            now=source_now,
            received_at=received_at,
            aircraft=sanitized,
        )

    return jsonify({"status": "ok", "aircraft": len(sanitized)})


@aircraft_bp.route("/api/adsb/live")
def live_aircraft():
    """Return the latest network-wide ADSB.lol snapshot for map clients."""
    with _snapshot_lock:
        snapshot = {
            "now": _snapshot["now"],
            "received_at": _snapshot["received_at"],
            "aircraft": list(_snapshot["aircraft"]),
        }

    age = (
        max(0.0, time.time() - snapshot["received_at"])
        if snapshot["received_at"]
        else None
    )
    snapshot.update(
        age_seconds=age,
        stale=age is None or age > _MAX_AGE_SECONDS,
        attribution="ADSB.lol — ODbL 1.0",
    )
    return jsonify(snapshot)


@aircraft_bp.route("/public/aircraft")
def public_aircraft():
    """Serve the dedicated aircraft map."""
    return render_template("aircraft.html")
