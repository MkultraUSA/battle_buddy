from flask import Blueprint, request, jsonify
import base64
import time
import threading
from modules.config import IGNORE_TGIDS, TGID_META
from modules.audio_dedup import is_duplicate_and_mark
from modules.transcription import transcribe
from modules.geocoding import extract_location
from modules.database import insert_call, calls_since
from modules.llm import groq_analyze, groq_identify_tgid
from modules.incident_engine import analyze_for_incident
from modules.talk import post_to_talk
from modules.config import _state
from modules.transcription import _BROADCASTIFY_MAX, _MAX_PROCESS_THREADS, _broadcastify_sem, _process_sem, _get_fw_model
from modules.llm import _TGID_ID_MIN_LEN


bp = Blueprint('receive', __name__)

@bp.route("/receive", methods=["POST"])
def receive():
    data = request.get_json(force=True)
    if not data or "audio_b64" not in data:
        return jsonify({"error": "missing audio_b64"}), 400

    tgid = int(data.get("tgid", 0))

    # Drop non-public-safety talkgroups — don't waste Whisper on them
    if tgid in IGNORE_TGIDS:
        return jsonify({"status": "ignored"}), 202

    wav_bytes = base64.b64decode(data["audio_b64"])

    # Prefer tag from Pi 1 (already resolved by OP25), fall back to TSV
    tag      = data.get("tag") or TGID_META.get(tgid, {}).get("tag") or f"TGID {tgid}"
    node     = data.get("node", "unknown")
    ts       = time.time()
    meta     = TGID_META.get(tgid, {})
    category = meta.get("cat", "Unknown")
    def_lat  = meta.get("lat")
    def_lon  = meta.get("lon")

    try:
        with __import__('wave').open(__import__('io').BytesIO(wav_bytes)) as wf:
            duration = wf.getnframes() / wf.getframerate()
    except Exception:
        duration = 0.0

    print(f"[recv] {tag} ({duration:.1f}s) from {node}", flush=True)

    # Skip clips too short to contain real speech — saves Whisper CPU
    if duration < 0.5:
        return jsonify({"status": "too_short"}), 202

    # Audio dedup — drop if we have seen this exact clip within 5 minutes
    audio_hash = __import__("hashlib").sha256(wav_bytes).hexdigest()
    if is_duplicate_and_mark(audio_hash):
        print(f"[recv] DEDUP {tag} ({duration:.1f}s) — already seen", flush=True)
        return jsonify({"status": "duplicate"}), 202

    # Bounded backlog with pi5 priority: broadcastify capped at _BROADCASTIFY_MAX
    # so at least (_MAX_PROCESS_THREADS - _BROADCASTIFY_MAX) slots are always
    # available for OP25 audio from the Pi.
    is_broadcastify = node != "pi5"
    if is_broadcastify and not _broadcastify_sem.acquire(blocking=False):
        print(f"[recv] DROP {tag} ({duration:.1f}s) [broadcastify] — broadcastify cap ({_BROADCASTIFY_MAX}) reached", flush=True)
        return jsonify({"status": "backlog_full"}), 202
    if not _process_sem.acquire(blocking=False):
        if is_broadcastify:
            _broadcastify_sem.release()
        src_label = "pi5" if node == "pi5" else "broadcastify"
        print(f"[recv] DROP {tag} ({duration:.1f}s) [{src_label}] — backlog full ({_MAX_PROCESS_THREADS} active)", flush=True)
        return jsonify({"status": "backlog_full"}), 202

    def process():
        try:
            
            _state['last_call_ts'] = time.time()
            transcript = transcribe(wav_bytes)
            lat, lon, location = extract_location(transcript)
            if lat is None:
                lat, lon = def_lat, def_lon
                location = None
                coords_approx = 1
            else:
                coords_approx = 0
            print(f"[recv] {tag}: {transcript[:80]}", flush=True)
            call_id = insert_call(ts, tgid, tag, category, node, duration, transcript, lat, lon, location, coords_approx)
            call = dict(id=call_id, ts=ts, tgid=tgid, tag=tag, category=category,
                        transcript=transcript, lat=lat, lon=lon, location=location)
            recent = calls_since(ts - 15 * 60)
            call["groq"] = groq_analyze(call, recent)
            # If this is an unknown talkgroup, ask Groq to identify it
            if tag.startswith("TGID ") and transcript and len(transcript) >= _TGID_ID_MIN_LEN:
                threading.Thread(target=groq_identify_tgid, args=(tgid, transcript),
                                 daemon=True).start()
            analyze_for_incident(call)
            post_to_talk(call)
        finally:
            _process_sem.release()
            if is_broadcastify:
                _broadcastify_sem.release()

    threading.Thread(target=process, daemon=True).start()
    return jsonify({"status": "queued"}), 202
