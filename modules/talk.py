"""modules/talk.py — Nextcloud Talk messaging helpers.

Provides: _bot_reply, _get_or_create_dm_room, send_dm_alert
Moved here from audio_receiver.py and modules/pollers_legacy.py.
No imports from audio_receiver — zero circular deps.
"""
import base64
import json
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from modules.config import (
    TALK_BASE,
    TALK_PASS,
    TALK_USER,
)
from modules.database import get_subscribers

# ---------------------------------------------------------------------------
# Talk DM room cache
# ---------------------------------------------------------------------------

_dm_room_cache: dict[str, str] = {}   # username → 1:1 room token


def _bot_reply(room_token: str, message: str):
    """Post a reply back to a Talk room."""
    url     = f"{TALK_BASE}/chat/{room_token}"
    payload = json.dumps({"message": message}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    req     = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization":  f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Content-Type":   "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[bot] reply sent to {room_token} ({len(message)} chars)", flush=True)
    except Exception as e:
        print(f"[bot] reply FAILED to {room_token}: {e}", flush=True)


def _get_or_create_dm_room(username: str) -> str | None:
    """Return the Talk 1:1 room token for a user, creating it if needed."""
    if username in _dm_room_cache:
        return _dm_room_cache[username]
    creds    = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    room_url = TALK_BASE.replace("/api/v1", "/api/v4") + "/room"
    payload  = urllib.parse.urlencode({"roomType": 1, "invite": username}).encode()
    req = urllib.request.Request(
        room_url,
        data=payload,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        raw   = urllib.request.urlopen(req, timeout=10).read().decode()
        root  = ET.fromstring(raw)
        token = root.findtext(".//token")
        if token:
            _dm_room_cache[username] = token
        return token
    except Exception as e:
        print(f"[dm] failed to get room for {username}: {e}", flush=True)
        return None


def send_dm_alert(itype: str, description: str, location: str | None,
                  agencies: str, category: str):
    """Send a 🔴 DM alert to all subscribed users."""
    subscribers = get_subscribers(itype, category)
    if not subscribers:
        return
    loc_str = f" @ {location}" if location else ""
    message = (
        f"🔴 BREAKING — {itype}{loc_str}\n"
        f"Agencies: {agencies}\n"
        f"{description}"
    )
    for username in subscribers:
        token = _get_or_create_dm_room(username)
        if token:
            threading.Thread(target=_bot_reply, args=(token, message),
                             daemon=True).start()
            print(f"[dm] alerted {username}: {itype}", flush=True)
