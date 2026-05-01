import os
import uuid
from datetime import datetime

from modules.config import TIPS_UPLOAD_DIR
from modules.talk import _bot_reply, _get_or_create_dm_room

_ALLOWED_TIP_EXT = {"jpg", "jpeg", "png", "gif", "webp"}


def _save_tip_photo(file) -> str | None:
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _ALLOWED_TIP_EXT:
        return None
    filename = uuid.uuid4().hex + "." + ext
    file.save(os.path.join(TIPS_UPLOAD_DIR, filename))
    return filename


def _notify_new_tip(tip_id: int, location_text: str, description: str,
                    photo_path: str | None, lat, lon, ts: float):
    """DM kevin when a new tip arrives. Runs in a thread."""
    token = _get_or_create_dm_room("kevin")
    if not token:
        print("[tip] could not get DM room for kevin", flush=True)
        return

    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %I:%M %p")
    coords_str = f"{lat:.5f}, {lon:.5f}" if (lat and lon) else "not geocoded"

    lines = [
        f"📍 NEW TIP #{tip_id} — review needed",
        "",
        f"Location: {location_text}",
        f"Coords: {coords_str}",
        f"Time: {time_str}",
    ]
    if description:
        lines += ["", "What they saw:", description]
    if photo_path:
        lines += ["", f"📷 https://battlebuddy.news/static/tips/{photo_path}"]
    lines += [
        "",
        "Review: https://battlebuddy.news/admin/tips",
        f"To investigate: ask me to look into tip #{tip_id}",
    ]

    _bot_reply(token, chr(10).join(lines))
    print(f"[tip] DM sent to kevin for tip #{tip_id}", flush=True)
