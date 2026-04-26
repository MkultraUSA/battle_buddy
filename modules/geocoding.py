import re
import threading
import urllib.request
from modules.talkgroups import CAT_COORDS

LOCATION_HINTS = [
    ("state capitol",      30.2747, -97.7404),
    ("capitol complex",    30.2747, -97.7404),
    ("governor's mansion", 30.2757, -97.7417),
    ("congress ave",       30.2672, -97.7431),
    ("congress",           30.2672, -97.7431),
    ("6th street",         30.2672, -97.7388),
    ("lamar",              30.2950, -97.7545),
    ("mopac",              30.3500, -97.7690),
    ("i-35",               30.2672, -97.7306),
    ("airport",            30.1975, -97.6664),
    ("abia",               30.1975, -97.6664),
    ("domain",             30.4015, -97.7296),
    ("round rock",         30.5083, -97.6789),
    ("cedar park",         30.5052, -97.8203),
    ("pflugerville",       30.4394, -97.6200),
    ("bastrop",            30.1107, -97.3154),
    ("burnet",             30.7488, -98.2345),
    ("new braunfels",      29.7030, -98.1245),
    ("kerrville",          30.0474, -99.1403),
    ("manor",              30.3424, -97.5564),
    ("buda",               30.0849, -97.8403),
    ("kyle",               29.9891, -97.8772),
    ("bee cave",           30.3077, -97.9461),
    ("lakeway",            30.3577, -97.9772),
    ("cap metro",          30.2672, -97.7431),
    ("capital metro",      30.2672, -97.7431),
    ("bus",                30.2672, -97.7431),
    ("ut campus",          30.2849, -97.7341),
    ("university",         30.2849, -97.7341),
    ("townes terrace",     30.3566, -97.4930),
    ("thomas wheeler",     30.3566, -97.4930),
    ("carillon",           30.3566, -97.4930),
]

_geocode_cache: dict = {}
_geocode_lock        = threading.Lock()
_GEO_BOUNDS          = (29.85, -98.25, 30.70, -97.25)

_ADDR_RE = re.compile(
    r'\b(\d{3,5})[,\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b'
    r'|'
    r'\b(\d{1,2}(?:st|nd|rd|th))\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'
    r'|'
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:and|&)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b'
)


def _geocode_address(address: str):
    key = address.lower().strip()
    with _geocode_lock:
        if key in _geocode_cache:
            return _geocode_cache[key]
    try:
        from geopy.geocoders import Nominatim
        geo = Nominatim(user_agent="battlebuddy/1.0")
        min_lat, min_lon, max_lat, max_lon = _GEO_BOUNDS
        for context in (f"{address}, Austin, TX", f"{address}, Travis County, TX", f"{address}, TX"):
            result = geo.geocode(context, timeout=4)
            if result:
                lat, lon = result.latitude, result.longitude
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    with _geocode_lock:
                        _geocode_cache[key] = (lat, lon)
                    print(f"[geocode] '{address}' → {lat:.4f},{lon:.4f}", flush=True)
                    return lat, lon
        with _geocode_lock:
            _geocode_cache[key] = None
    except Exception as exc:
        print(f"[geocode] error for '{address}': {exc}", flush=True)
        with _geocode_lock:
            _geocode_cache[key] = None
    return None


def extract_location(text: str):
    if not text:
        return None, None, None
    lower = text.lower()
    for keyword, lat, lon in LOCATION_HINTS:
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
