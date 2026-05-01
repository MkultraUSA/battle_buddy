
import base64
import json
import urllib.request
from .config import TALK_BASE, TALK_USER, TALK_PASS

def _bot_reply(room_token: str, message: str):
    """Post a reply back to the Talk room that triggered the command."""
    url     = f"{TALK_BASE}/chat/{room_token}"
    payload = json.dumps({"message": message}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    req     = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {creds}",
            "Accept": "application/json",
            "OCS-APIRequest": "true",
        },
    )
    urllib.request.urlopen(req)

_dm_room_cache: dict[str, str] = {}   # username → 1:1 room token

def _get_or_create_dm_room(username: str) -> str | None:
    """Return the Talk 1:1 room token for a user, creating it if needed."""
    if username in _dm_room_cache:
        return _dm_room_cache[username]
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    # Room creation requires API v4; chat posting uses v1 (TALK_BASE)
    room_url = TALK_BASE.replace("/api/v1", "/api/v4") + "/room"
    payload = urllib.parse.urlencode({"roomType": 1, "invite": username}).encode()
    req = urllib.request.Request(
        room_url,
        data=payload,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    try:
        raw = urllib.request.urlopen(req, timeout=10).read().decode()
        # Response is XML — extract <token> element
        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw)
        token = root.findtext(".//token")
        if token:
            _dm_room_cache[username] = token
        return token
    except Exception as e:
        print(f"[dm] failed to get room for {username}: {e}", flush=True)
        return None
