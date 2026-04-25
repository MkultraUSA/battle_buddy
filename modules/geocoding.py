import re
import sqlite3
import time
import threading
from geopy.geocoders import Nominatim

# Assuming these exist in audio_receiver.py, we might need to pass them or load them differently.
# For now, defined locally based on reading.
_GEO_BOUNDS = (30.0, -98.2, 30.6, -97.5) # Approximate Travis County bounds
_geocode_cache = {}
_geocode_lock = threading.Lock()

_ADDR_RE = re.compile(
    r'\b(\d{3,5})[,\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b'
    r'|'
    r'\b(\d{1,2}(?:st|nd|rd|th))\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'
    r'|'
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'
)

def _geocode_address(address: str) -> tuple[float, float] | None:
    key = address.lower().strip()
    with _geocode_lock:
        if key in _geocode_cache:
            return _geocode_cache[key]
    try:
        geo = Nominatim(user_agent="battlebuddy/1.0")
        min_lat, min_lon, max_lat, max_lon = _GEO_BOUNDS
        for context in (f"{address}, Austin, TX", f"{address}, Travis County, TX", f"{address}, TX"):
            result = geo.geocode(context, timeout=4)
            if result:
                lat, lon = result.latitude, result.longitude
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    with _geocode_lock:
                        _geocode_cache[key] = (lat, lon)
                    return lat, lon
        with _geocode_lock:
            _geocode_cache[key] = None
    except Exception as exc:
        with _geocode_lock:
            _geocode_cache[key] = None
    return None

def extract_location(text: str, location_hints: list) -> tuple[float | None, float | None, str | None]:
    if not text:
        return None, None, None
    lower = text.lower()
    for keyword, lat, lon in location_hints:
        if keyword in lower:
            return lat, lon, keyword.title()
    best_candidate = None
    for m in _ADDR_RE.finditer(text):
        candidate = m.group(0).strip()
        if len(candidate) < 8:
            continue
        result = _geocode_address(candidate)
        if result:
            return result[0], result[1], candidate
        if best_candidate is None:
            best_candidate = candidate
    if best_candidate:
        return None, None, best_candidate
    return None, None, None
