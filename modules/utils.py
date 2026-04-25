"""
Shared utility functions for Battle Buddy modules.
"""
import math
import sqlite3
import threading
import time

from modules.config import (
    DB_PATH,
    LOCUTION_CORRECTIONS,
    DPS_ASSET_PATTERNS,
    DPS_MENTION_PATTERNS,
    CAPITOL_KEYWORDS,
    AIR_ASSET_TGIDS,
    AIR_ASSET_PATTERN,
    AIR_ASSET_CONTEXT,
)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_calls_since(since_ts: float) -> list[dict]:
    """Return all calls stored after since_ts as a list of dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM calls WHERE ts > ? ORDER BY ts DESC", (since_ts,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Transcript correction / normalisation
# ---------------------------------------------------------------------------

def apply_locution_corrections(transcript: str) -> str:
    """Apply known Whisper mis-transcription corrections to a locution transcript."""
    for pattern, replacement in LOCUTION_CORRECTIONS:
        transcript = pattern.sub(replacement, transcript)
    return transcript


# ---------------------------------------------------------------------------
# DPS / Capitol detection helpers
# ---------------------------------------------------------------------------

def detect_dps_assets(transcript: str) -> list[str]:
    """Return list of DPS asset types detected in a transcript."""
    if not transcript:
        return []
    found = []
    for pattern, label in DPS_ASSET_PATTERNS:
        if pattern.search(transcript):
            found.append(label)
    return found


def is_capitol_area(transcript: str, location: str | None) -> bool:
    """Return True if the call appears to be in/around the Capitol complex."""
    text = (transcript + " " + (location or "")).lower()
    return any(k in text for k in CAPITOL_KEYWORDS)


def mentions_dps(transcript: str) -> bool:
    """Return True if any agency's transcript references DPS."""
    return bool(DPS_MENTION_PATTERNS.search(transcript or ""))


# ---------------------------------------------------------------------------
# Air asset detection
# ---------------------------------------------------------------------------

def detect_air_asset(tgid: int, transcript: str, category: str) -> str | None:
    """Return a context string if an air asset is active, else None."""
    if tgid in AIR_ASSET_TGIDS or AIR_ASSET_PATTERN.search(transcript or ""):
        context = AIR_ASSET_CONTEXT.get(category, AIR_ASSET_CONTEXT["default"])
        return context
    return None


# ---------------------------------------------------------------------------
# Geospatial helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Geocoding cache — in-memory dict backed by the geocode_cache SQLite table
# ---------------------------------------------------------------------------

# key  : address string (lowercased, stripped)
# value: (lat, lon) tuple on a hit, or None on a recorded miss
_geocode_cache: dict[str, tuple[float, float] | None] = {}
_geocode_lock = threading.Lock()

_GEOCODE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS geocode_cache (
    address_key  TEXT PRIMARY KEY,
    lat          REAL,
    lon          REAL,
    ts_cached    REAL NOT NULL
)
"""

_GEOCODE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_geocode_key ON geocode_cache(address_key)"
)


def _ensure_geocode_table(conn: sqlite3.Connection) -> None:
    """Create the geocode_cache table and index if they do not yet exist."""
    conn.execute(_GEOCODE_TABLE_DDL)
    conn.execute(_GEOCODE_INDEX_DDL)
    conn.commit()


def _geocode_load_db() -> None:
    """Warm the in-memory geocode cache from the persistent DB at startup.

    Reads every row from the geocode_cache table and populates
    _geocode_cache so that subsequent lookups never hit SQLite for
    already-resolved (or already-missed) addresses.

    Creates the table automatically if it is absent (first-run safety).
    Safe to call from any thread; acquires _geocode_lock while writing to
    the shared dict.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_geocode_table(conn)
        rows = conn.execute(
            "SELECT address_key, lat, lon FROM geocode_cache"
        ).fetchall()
        loaded: dict[str, tuple[float, float] | None] = {}
        for address_key, lat, lon in rows:
            loaded[address_key] = (lat, lon) if lat is not None else None
        with _geocode_lock:
            _geocode_cache.update(loaded)
        print(
            f"[geocode] loaded {len(loaded)} cached entries from DB",
            flush=True,
        )
    except Exception as exc:
        print(f"[geocode] DB warm-up failed: {exc}", flush=True)
    finally:
        if conn is not None:
            conn.close()


def _geocode_save_db(key: str, lat: float | None, lon: float | None) -> None:
    """Persist a single geocode result (or a None miss) to the DB.

    Uses INSERT OR REPLACE so re-geocoding an address always refreshes
    both the coordinates and the ts_cached timestamp.

    Args:
        key: The normalised address string (used as the primary key).
        lat: Latitude on a successful geocode; None to record a miss.
        lon: Longitude on a successful geocode; None to record a miss.

    Errors are caught and printed rather than raised so that a transient
    DB problem never silences the caller.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(DB_PATH)
        _ensure_geocode_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO geocode_cache "
            "(address_key, lat, lon, ts_cached) VALUES (?, ?, ?, ?)",
            (key, lat, lon, time.time()),
        )
        conn.commit()
    except Exception as exc:
        print(f"[geocode] DB save failed for '{key}': {exc}", flush=True)
    finally:
        if conn is not None:
            conn.close()
