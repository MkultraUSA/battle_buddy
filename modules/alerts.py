"""modules/alerts.py — Banners, Deck cards, and commute alerts.

Moved here from modules/pollers_legacy.py and audio_receiver.py.
No imports from audio_receiver — zero circular deps.
"""
import base64
import json
import math
import sqlite3
import threading
import urllib.request

from modules.config import (
    DB_PATH, TALK_USER, TALK_PASS,
    DECK_BASE, DECK_BOARD_ID, DECK_STACK_NEW, DECK_LABELS,
    GOOGLE_ROUTES_KEY,
)
from modules.talk import _bot_reply, _get_or_create_dm_room

# ---------------------------------------------------------------------------
# Announcement banner — site-wide breaking alert
# ---------------------------------------------------------------------------

BANNER_BASE = "https://kevcloud.ddns.net/index.php/apps/announcementbanner/banners"

BANNER_ITYPES = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "MASS CASUALTY",
    "STRUCTURE FIRE", "HOSTAGE/BARRICADE", "AIRCRAFT EMERGENCY",
    "AIR ASSET ACTIVE",
}

_active_banner_id: str | None = None
_banner_lock = threading.Lock()


def _banner_api(path: str = "", data: dict | None = None, method: str | None = None):
    if method is None:
        method = "POST" if data is not None else "GET"
    url   = BANNER_BASE + (f"/{path}" if path else "")
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    req   = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/json"},
        method=method,
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def post_banner(itype: str, location: str | None, agencies: str):
    """Post a site-wide breaking banner for serious incidents."""
    global _active_banner_id
    if itype not in BANNER_ITYPES:
        return
    loc_str = f" @ {location}" if location else ""
    message = f"🔴 BREAKING: {itype}{loc_str} — {agencies} responding"
    with _banner_lock:
        try:
            if _active_banner_id:
                _banner_api(_active_banner_id, method="DELETE")
            result = _banner_api(data={
                "enabled": True, "message": message, "variant": "danger",
                "dismissible": False, "readMoreText": "", "readMoreUrl": "",
                "scheduleStart": "", "scheduleEnd": "",
                "audienceTarget": "all", "audienceGroups": [],
                "targetAppMode": "all", "targetApps": [],
            })
            _active_banner_id = result.get("id")
            print(f"[banner] posted: {message}", flush=True)
        except Exception as e:
            print(f"[banner] failed: {e}", flush=True)


def clear_banner(itype: str):
    """Remove the site-wide banner when an incident clears."""
    global _active_banner_id
    if itype not in BANNER_ITYPES:
        return
    with _banner_lock:
        if _active_banner_id:
            try:
                _banner_api(_active_banner_id, method="DELETE")
                print(f"[banner] cleared for {itype}", flush=True)
                _active_banner_id = None
            except Exception as e:
                print(f"[banner] clear failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Deck integration — auto-create incident cards
# ---------------------------------------------------------------------------

def create_deck_card(incident: dict):
    """Create a Deck card in the New column when a new incident is detected."""
    import time
    from datetime import datetime
    itype    = incident.get("itype", "INCIDENT")
    desc     = incident.get("description", "")
    location = incident.get("location")
    agencies = ", ".join(json.loads(incident.get("agencies") or "[]"))
    ts       = datetime.fromtimestamp(incident.get("ts_start", time.time())).strftime("%H:%M")

    title = f"{itype}"
    if location:
        title += f" @ {location}"

    body = (
        f"**Time:** {ts}\n"
        f"**Agencies:** {agencies or 'unknown'}\n"
        f"**Details:** {desc}\n"
    )

    label_id = DECK_LABELS.get(itype, DECK_LABELS.get("SHOOTING"))
    creds    = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers  = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    card_url  = f"{DECK_BASE}/boards/{DECK_BOARD_ID}/stacks/{DECK_STACK_NEW}/cards"
    card_data = json.dumps({"title": title, "type": "plain", "order": 0,
                            "description": body}).encode()
    try:
        req     = urllib.request.Request(card_url, data=card_data, headers=headers, method="POST")
        resp    = json.loads(urllib.request.urlopen(req, timeout=10).read())
        card_id = resp.get("id")
        print(f"[deck] card created: {title} (id={card_id})", flush=True)
        if label_id and card_id:
            label_url  = f"{DECK_BASE}/boards/{DECK_BOARD_ID}/stacks/{DECK_STACK_NEW}/cards/{card_id}/assignLabel"
            label_data = json.dumps({"labelId": label_id}).encode()
            req = urllib.request.Request(label_url, data=label_data, headers=headers, method="PUT")
            urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[deck] card creation failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Commute alerts — notify premium users when incidents hit their route
# ---------------------------------------------------------------------------

_COMMUTE_ALERT_ITYPES = {
    "SHOOTING", "OFFICER DOWN", "PURSUIT", "STRUCTURE FIRE",
    "HAZMAT", "WEAPONS", "CRASH/COLLISION", "STABBING", "MASS CASUALTY",
}
_COMMUTE_CORRIDOR_MILES = 3.0  # incident must be within this distance of route line


def _point_to_segment_distance_miles(px, py, ax, ay, bx, by) -> float:
    """Perpendicular distance (miles) from point P to line segment A→B."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        dx2 = px - ax; dy2 = py - ay
        return math.sqrt(dx2*dx2 + dy2*dy2) * 69.0
    t   = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / (dx*dx + dy*dy)))
    cx2 = ax + t*dx; cy2 = ay + t*dy
    ddx = px - cx2;  ddy = py - cy2
    return math.sqrt(ddx*ddx + ddy*ddy) * 69.0


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
            "Content-Type":    "application/json",
            "X-Goog-Api-Key":  GOOGLE_ROUTES_KEY,
            "X-Goog-FieldMask": "routes.duration,routes.staticDuration",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10).read().decode()
        data = json.loads(resp)
        dur  = data.get("routes", [{}])[0].get("duration", "0s")
        secs = int(dur.rstrip("s")) if isinstance(dur, str) else 0
        return max(1, round(secs / 60))
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

    conn  = sqlite3.connect(DB_PATH)
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

        live_mins = _routes_travel_time(olat, olon, dlat, dlon, traffic=True)
        if live_mins is None:
            continue

        delta     = live_mins - baseline if baseline else None
        delta_str = ""
        if delta is not None:
            if delta > 0:
                delta_str = f" (+{delta} min over normal)"
            elif delta < 0:
                delta_str = f" ({abs(delta)} min faster than normal)"

        short_desc = description[:120].rsplit(" ", 1)[0] if len(description) > 120 else description
        msg = (
            f"\U0001f697 [COMMUTE ALERT] {itype} detected {dist:.1f} mi from your route\n"
            f"\U0001f552 Current travel time: {live_mins} min{delta_str}\n"
            f"\U0001f4cd {short_desc}"
        )

        try:
            token = _get_or_create_dm_room(username)
            if token:
                _bot_reply(token, msg)
            print(f"[commute] alert sent to {username}: {itype} {dist:.1f}mi, {live_mins}min", flush=True)
        except Exception as e:
            print(f"[commute] DM failed for {username}: {e}", flush=True)
