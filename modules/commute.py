import json
import urllib.request

# These lived here and, verbatim, in modules/alerts.py. One definition now, in
# modules/commute_alerts.py, which depends on neither caller. Re-exported rather
# than moved at the call sites so `from modules.commute import
# _routes_travel_time` in audio_receiver.py keeps working: that explicit import
# exists precisely because `from x import *` skips underscore names, and three
# handlers 500'd when such a name was missing.
from modules.commute_alerts import (  # noqa: F401
    _COMMUTE_ALERT_ITYPES,
    _COMMUTE_CORRIDOR_MILES,
    _check_commute_alerts,
    _point_to_segment_distance_miles,
    _routes_travel_time,
)
from modules.config import GOOGLE_ROUTES_KEY


def _commute_route_info(origin_addr: str, dest_addr: str, traffic: bool = False):
    """
    Call Routes API with raw address strings.
    Returns dict with keys: origin_lat, origin_lon, dest_lat, dest_lon, mins
    or None on failure. Google geocodes the addresses natively — no Nominatim needed.
    """
    
    preference = "TRAFFIC_AWARE" if traffic else "TRAFFIC_UNAWARE"
    body = json.dumps({
        "origin":      {"address": origin_addr},
        "destination": {"address": dest_addr},
        "travelMode":  "DRIVE",
        "routingPreference": preference,
    }).encode()
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.legs.startLocation,routes.legs.endLocation",
        },
        method="POST",
    )
    try:
        resp  = urllib.request.urlopen(req, timeout=10).read().decode()
        data  = json.loads(resp)
        route = data.get("routes", [{}])[0]
        leg   = route.get("legs", [{}])[0]
        dur   = route.get("duration", "0s")
        secs  = int(dur.rstrip("s")) if isinstance(dur, str) else 0
        sloc  = leg.get("startLocation", {}).get("latLng", {})
        eloc  = leg.get("endLocation",   {}).get("latLng", {})
        if not sloc or not eloc:
            return None
        return {
            "origin_lat": sloc["latitude"],
            "origin_lon": sloc["longitude"],
            "dest_lat":   eloc["latitude"],
            "dest_lon":   eloc["longitude"],
            "mins":       max(1, round(secs / 60)),
        }
    except Exception as e:
        print(f"[commute] Routes API error: {e}", flush=True)
        return None

