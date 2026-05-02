from flask import Blueprint, request, jsonify
from modules.database import recent_calls, active_incidents, calls_for_sitrep
from modules.incident_engine import build_sitrep
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import json

_CDT = ZoneInfo("America/Chicago")

bp = Blueprint('reports', __name__)

@bp.route("/api/calls")
def api_calls():
    return jsonify(recent_calls(200))

@bp.route("/api/sitrep")
def api_sitrep():
    minutes = int(request.args.get("minutes", 60))
    return jsonify({"sitrep": build_sitrep(minutes)})

@bp.route("/api/voice_sitrep")
def api_voice_sitrep():
    """Returns a clean, natural-language spoken sitrep for TTS."""
    minutes = int(request.args.get("minutes", 60))
    calls     = calls_for_sitrep(minutes)
    incidents = [i for i in active_incidents() if not i.get("is_test")]

    now = datetime.now(_CDT).strftime("%-I:%M %p %Z")
    parts = [f"Battle Buddy. Austin Metro situation report as of {now}."]

    if incidents:
        count = len(incidents)
        parts.append(f"{count} active {'incident' if count == 1 else 'incidents'}.")
        for inc in incidents:
            age = int((time.time() - inc["ts_start"]) / 60)
            loc = f" at {inc['location']}" if inc.get("location") else ""
            agencies = json.loads(inc.get("agencies") or "[]")
            agency_str = ", ".join(agencies[:3]) if agencies else "unknown agencies"
            age_str = f"{age} minutes ago" if age < 60 else f"{age // 60} hours ago"
            parts.append(
                f"{inc['itype'].replace('/', ' or ')}{loc}, "
                f"detected {age_str}, {agency_str} responding."
            )
    else:
        parts.append("No active incidents at this time.")

    if calls:
        by_cat: dict[str, int] = {}
        for c in calls:
            cat = c.get("category") or "Unknown"
            by_cat[cat] = by_cat.get(cat, 0) + 1
        top = sorted(by_cat.items(), key=lambda x: -x[1])[:4]
        summary = ", ".join(f"{cat} {n}" for cat, n in top)
        parts.append(
            f"{len(calls)} calls monitored in the past {minutes} minutes "
            f"across {summary}."
        )
    else:
        parts.append(f"No calls received in the past {minutes} minutes.")

    return jsonify({"text": " ".join(parts)})
