
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
