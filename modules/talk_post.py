"""modules/talk_post.py — Post high-priority calls to Nextcloud Talk.

Extracted from modules/pollers_legacy.py to break the monolith into
focused, testable units.  No logic changes — pure relocation.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.request
from datetime import datetime

from modules.config import (
    TALK_BASE,
    TALK_ENABLED,
    TALK_PASS,
    TALK_USER,
    _room_for_call,
)
from modules.incident_engine import (
    _active_incidents,
    _incident_lock,
)
from modules.talkgroups import (
    detect_air_asset,
    detect_dps_assets,
    is_capitol_area,
    mentions_dps,
)

# Regex patterns for unit extraction from transcripts
_UNIT_PATTERNS = [
    re.compile(r'\bunit[s]?\s+(\d{1,4})\b', re.I),
]

_HIGH_PRIORITY = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "AIRCRAFT EMERGENCY",
    "MASS CASUALTY", "STRUCTURE FIRE", "HOSTAGE/BARRICADE",
}
_HIGH_KW = [
    "officer down", "shots fired", "shooting", "stabbing",
    "structure fire", "mass casualty", "hostage", "barricade", "10-99",
    "homicide", "body found", "found dead", "death investigation",
    "medical examiner",
]


def _extract_units(transcript: str) -> list[str]:
    """Extract unit identifiers from a call transcript."""
    found, seen = [], set()
    for pat in _UNIT_PATTERNS:
        for m in pat.finditer(transcript):
            unit = m.group(1).strip()
            key = unit.lower()
            if key not in seen:
                seen.add(key)
                found.append(unit)
    return found[:6]


def post_to_talk(call: dict):
    """Post a high-priority call summary to the appropriate Nextcloud Talk room.

    Only genuinely dangerous calls are posted — routine chatter is filtered out.
    Incident-level alerts are handled separately by send_dm_alert.
    """
    if not TALK_ENABLED:
        return

    ts = datetime.fromtimestamp(call["ts"]).strftime("%H:%M")
    tag = call.get("tag") or f"TGID {call.get('tgid')}"
    cat = call.get("category", "Unknown")
    loc = f" @ {call['location']}" if call.get("location") else ""
    transcript = call.get("transcript") or "(no transcript)"
    tgid = call.get("tgid")
    text_lower = transcript.lower()

    # Only post high-danger calls
    llm_pri_early = (call.get("llm") or {}).get("priority", "NONE")
    has_high_kw = any(k in text_lower for k in _HIGH_KW)
    if llm_pri_early != "HIGH" and not has_high_kw:
        return

    # --- Incident linkage ---
    incident_line = ""
    with _incident_lock:
        for inc in _active_incidents.values():
            if tgid in inc.get("tgids", set()) or cat in inc.get("agencies", set()):
                age = int((time.time() - inc["ts_updated"]) / 60)
                incident_line = f"\n⚡ INCIDENT: {inc['itype']} — active {age}m"
                break

    priority = "🔴"

    # --- Unit extraction ---
    units = _extract_units(transcript)
    units_line = f"\nUnits: {', '.join(units)}" if units else ""

    # --- Air asset context ---
    air_line = ""
    air_context = detect_air_asset(tgid, transcript, cat)
    if air_context:
        air_line = f"\n🚁 AIR: {air_context}"

    # --- DPS asset/Capitol context ---
    dps_line = ""
    if cat == "DPS" or mentions_dps(transcript):
        assets = detect_dps_assets(transcript)
        capitol = is_capitol_area(transcript, call.get("location"))
        parts = []
        if assets:
            parts.append(", ".join(assets))
        if capitol:
            parts.append("Capitol area")
        if parts:
            dps_line = f"\n🏛 DPS: {' — '.join(parts)}"

    message = (
        f"{priority} [{ts}] {cat} — {tag}{loc}"
        f"{incident_line}"
        f"{air_line}"
        f"{dps_line}"
        f"{units_line}"
        f"\n\"{transcript}\""
    )

    payload = json.dumps({"message": message}).encode()
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "OCS-APIRequest": "true",
        "Content-Type": "application/json",
    }

    for room_token in _room_for_call(call, priority):
        url = f"{TALK_BASE}/chat/{room_token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f"[talk] posted {priority} {tag} → {room_token}: {transcript[:50]}", flush=True)
        except Exception as e:
            print(f"[talk] post failed → {room_token}: {e}", flush=True)
