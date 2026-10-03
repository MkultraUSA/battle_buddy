"""modules/alerts.py — Site-wide banners, Deck cards, and Talk DM alerts.

Moved here from modules/pollers_legacy.py and audio_receiver.py.
No imports from audio_receiver — zero circular deps.

The commute-alert half used to live here too, duplicated verbatim in
modules/commute.py. It is now modules/commute_alerts.py, which depends on
neither caller; the names are re-exported at the bottom of this file so existing
import sites keep working.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import urllib.request

from modules.config import (
    DECK_BASE,
    DECK_BOARD_ID,
    DECK_LABELS,
    DECK_STACK_NEW,
    TALK_PASS,
    TALK_USER,
)
from modules.database import get_subscribers
from modules.talk import _bot_reply, _get_or_create_dm_room

# ---------------------------------------------------------------------------
# Announcement banner — site-wide breaking alert
# ---------------------------------------------------------------------------

BANNER_BASE = os.environ.get("NEXTCLOUD_BANNER_BASE", "https://nextcloud.example.com/index.php/apps/announcementbanner/banners")

BANNER_ITYPES = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "MASS CASUALTY",
    "STRUCTURE FIRE", "HOSTAGE/BARRICADE", "AIRCRAFT EMERGENCY",
    "AIR ASSET ACTIVE",
}

_active_banner_id: str | None = None
# Which incident posted the current banner, so retiring one incident cannot
# tear down a banner that a different, still-live incident owns.
_active_banner_incident_id: int | None = None
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


def post_banner(itype: str, location: str | None, agencies: str, incident_id: int | None = None):
    """Post a site-wide breaking banner for serious incidents."""
    global _active_banner_id, _active_banner_incident_id
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
            _active_banner_incident_id = incident_id
            print(f"[banner] posted: {message}", flush=True)
        except Exception as e:
            print(f"[banner] failed: {e}", flush=True)


def clear_banner(itype: str, incident_id: int | None = None):
    """Remove the site-wide banner when the incident that posted it clears.

    There is exactly one site-wide banner, so ownership has to be tracked or a
    newly created incident can take the banner down when an unrelated one is
    retired. Pass the clearing incident's id to retract only that incident's own
    banner; omit it to force the banner down regardless (used when there is no
    banner owner recorded).
    """
    global _active_banner_id, _active_banner_incident_id
    if itype not in BANNER_ITYPES:
        return
    with _banner_lock:
        if _active_banner_id and (
            incident_id is None or _active_banner_incident_id == incident_id
        ):
            try:
                _banner_api(_active_banner_id, method="DELETE")
                print(f"[banner] cleared for {itype}", flush=True)
                _active_banner_id = None
                _active_banner_incident_id = None
            except Exception as e:
                print(f"[banner] clear failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# DM alerts — push breaking incidents directly to subscribed users
# ---------------------------------------------------------------------------

def send_dm_alert(
    itype: str,
    description: str,
    location: str | None,
    agencies: str,
    category: str,
    subscribers_provider=get_subscribers,
    room_provider=_get_or_create_dm_room,
    reply_func=_bot_reply,
    thread_factory=threading.Thread,
) -> int:
    """Send a breaking incident DM alert to subscribed users."""
    subscribers = subscribers_provider(itype, category)
    if not subscribers:
        return 0

    loc_str = f" @ {location}" if location else ""
    message = (
        f"🔴 BREAKING — {itype}{loc_str}\n"
        f"Agencies: {agencies}\n"
        f"{description}"
    )
    sent = 0
    for username in subscribers:
        token = room_provider(username)
        if token:
            thread_factory(target=reply_func, args=(token, message), daemon=True).start()
            print(f"[dm] alerted {username}: {itype}", flush=True)
            sent += 1
    return sent


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

# --- commute alerts ---------------------------------------------------------
# These lived here and, verbatim, in modules/commute.py. One definition now, in
# modules/commute_alerts.py, which depends on neither caller. Re-exported rather
# than moved at the call sites so that `from modules.alerts import
# _point_to_segment_distance_miles` in audio_receiver.py keeps working: that
# explicit import exists precisely because `from x import *` skips underscore
# names, and three handlers 500'd when it was missing.
from modules.commute_alerts import (  # noqa: E402,F401
    _COMMUTE_ALERT_ITYPES,
    _COMMUTE_CORRIDOR_MILES,
    _check_commute_alerts,
    _point_to_segment_distance_miles,
    _routes_travel_time,
)
