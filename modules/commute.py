
import json
import math
import sqlite3
import urllib.request

from modules.config import DB_PATH, GOOGLE_ROUTES_KEY
from modules.talk import _bot_reply, _get_or_create_dm_room

_COMMUTE_ALERT_ITYPES = {
    "SHOOTING", "OFFICER DOWN", "PURSUIT", "STRUCTURE FIRE",
    "HAZMAT", "WEAPONS", "CRASH/COLLISION", "STABBING", "MASS CASUALTY",
}
_COMMUTE_CORRIDOR_MILES = 3.0  # incident must be within this distance of route line

def _point_to_segment_distance_miles(px, py, ax, ay, bx, by) -> float:
    """Perpendicular distance (miles) from point P to line segment A→B."""
    
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        dx2 = px - ax; dy2 = py - ay  # noqa: E702
        return math.sqrt(dx2*dx2 + dy2*dy2) * 69.0
    t = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / (dx*dx + dy*dy)))
    cx2 = ax + t*dx; cy2 = ay + t*dy  # noqa: E702
    ddx = px - cx2; ddy = py - cy2  # noqa: E702
    deg = math.sqrt(ddx*ddx + ddy*ddy)
    return deg * 69.0  # rough degrees→miles


def _routes_travel_time(origin_lat, origin_lon, dest_lat, dest_lon, traffic=True) -> int | None:
    """Call Google Routes API; return travel time in minutes or None on error."""
    
    preference = "TRAFFIC_AWARE" if traffic else "TRAFFIC_UNAWARE"
    body = json.dumps({
        "origin":      {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lon}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat,   "longitude": dest_lon}}},
        "travelMode":  "DRIVE",
        "routingPreference": preference,
    }).encode()
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration",
        },
        method="POST",
    )
    try:
        resp  = urllib.request.urlopen(req, timeout=10).read().decode()
        data  = json.loads(resp)
        dur   = data.get("routes", [{}])[0].get("duration", "0s")
        secs  = int(dur.rstrip("s")) if isinstance(dur, str) else 0
        return max(1, round(secs / 60))
    except Exception as e:
        print(f"[commute] Routes API error: {e}", flush=True)
        return None


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


def _check_commute_alerts(inc_id: int, itype: str, inc_lat: float, inc_lon: float, description: str):
    """Check all premium users with saved commutes; alert those whose route passes near the incident."""
    if itype not in _COMMUTE_ALERT_ITYPES:
        return
    if inc_lat is None or inc_lon is None:
        return
    if not GOOGLE_ROUTES_KEY:
        return

    conn = sqlite3.connect(DB_PATH)
    users = conn.execute(
        "SELECT username, commute_origin, commute_origin_lat, commute_origin_lon, "
        "commute_dest, commute_dest_lat, commute_dest_lon, commute_baseline_mins "
        "FROM premium_users WHERE status='active' AND commute_origin_lat IS NOT NULL"
    ).fetchall()
    conn.close()

    for (username, origin, olat, olon, dest, dlat, dlon, baseline) in users:
        dist = _point_to_segment_distance_miles(inc_lat, inc_lon, olat, olon, dlat, dlon)
        if dist > _COMMUTE_CORRIDOR_MILES:
            continue

        # Fetch live travel time
        live_mins = _routes_travel_time(olat, olon, dlat, dlon, traffic=True)
        if live_mins is None:
            continue

        delta = live_mins - baseline if baseline else None
        delta_str = ""
        if delta is not None:
            if delta > 0:
                delta_str = f" (+{delta} min over normal)"
            elif delta < 0:
                delta_str = f" ({abs(delta)} min faster than normal)"

        # Trim description for message
        short_desc = description[:120].rsplit(" ", 1)[0] if len(description) > 120 else description

        msg = (
            f"🚗 [COMMUTE ALERT] {itype} detected {dist:.1f} mi from your route\n"
            f"🕔 Current travel time: {live_mins} min{delta_str}\n"
            f"📍 {short_desc}"
        )

        # Send Talk DM
        try:
            token = _get_or_create_dm_room(username)
            if token:
                _bot_reply(token, msg)
            print(f"[commute] alert sent to {username}: {itype} {dist:.1f}mi, {live_mins}min", flush=True)
        except Exception as e:
            print(f"[commute] DM failed for {username}: {e}", flush=True)
