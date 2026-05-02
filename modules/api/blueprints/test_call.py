from flask import Blueprint, request, jsonify
import time
from modules.config import TGID_META
from modules.database import insert_call, calls_since
from modules.llm import groq_analyze
from modules.incident_engine import analyze_for_incident
from modules.talk import post_to_talk

bp = Blueprint('test_call', __name__)

@bp.route("/test_call", methods=["POST"])
def test_call():
    """Inject a synthetic call for pipeline testing — bypasses Whisper."""
    data = request.get_json(force=True)
    tgid       = int(data.get("tgid", 1315))
    transcript = data.get("transcript", "")
    tag        = data.get("tag") or TGID_META.get(tgid, {}).get("tag") or f"TGID {tgid}"
    meta       = TGID_META.get(tgid, {})
    category   = data.get("category") or meta.get("cat", "Unknown")
    ts         = time.time()
    lat        = data.get("lat") or meta.get("lat")
    lon        = data.get("lon") or meta.get("lon")
    location   = data.get("location")
    call_id = insert_call(ts, tgid, tag, category, "test", 5.0, transcript, lat, lon, location)
    call = dict(id=call_id, ts=ts, tgid=tgid, tag=tag, category=category,
                transcript=transcript, lat=lat, lon=lon, location=location)
    recent = calls_since(ts - 15 * 60)
    call["groq"] = groq_analyze(call, recent)
    analyze_for_incident(call)
    post_to_talk(call)
    return jsonify({"status": "ok", "tag": tag, "category": category, "transcript": transcript}), 200
