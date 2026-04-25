import threading
import time
import re
import sqlite3
import urllib.request
import urllib.parse
import base64
from typing import Optional

# These will be imported/accessed from audio_receiver.py
# Assuming imports are set up in audio_receiver.py to handle the module path
# e.g. from modules.llm import groq_analyze, groq_identify_tgid

def groq_analyze(call, recent_calls, GROQ_ENABLED, GROQ_MIN_TRANSCRIPT, GROQ_MIN_DURATION, 
                 _groq_backoff_until, _groq_call_times, _groq_rate_lock, _groq_routine_tracker, 
                 _GROQ_RATE_LIMIT, _GROQ_BACKOFF_SECS, _GROQ_ROUTINE_WINDOW, _GROQ_ROUTINE_COOLDOWN, 
                 _GROQ_SAFETY_RE, _GROQ_SYSTEM, _call_groq_llm, DB_PATH, _GROQ_ROUTINE_STREAK) -> Optional[dict]:
    """Call Groq LLM to analyze a call. Returns parsed JSON dict or None on failure."""
    if not GROQ_ENABLED:
        return None
    
    if call.get("tgid", 0) == 0:
        return None
        
    transcript = call.get("transcript") or ""
    if not transcript or len(transcript) < GROQ_MIN_TRANSCRIPT:
        return None
        
    if call.get("duration", 99.0) < GROQ_MIN_DURATION:
        return None
        
    tgid = call.get("tgid", 0)
    if not _GROQ_SAFETY_RE.search(transcript):
        now_pre = time.time()
        tracker = _groq_routine_tracker.get(tgid)
        if tracker and now_pre < tracker.get("cooldown_until", 0):
            return None
            
    now = time.time()
    with _groq_rate_lock:
        if now < _groq_backoff_until:
            return None
        _groq_call_times[:] = [t for t in _groq_call_times if now - t < 60]
        if len(_groq_call_times) >= _GROQ_RATE_LIMIT:
            return None
        _groq_call_times.append(now)
    
    ctx_lines = []
    for rc in recent_calls[-6:]:
        rc_txt = (rc.get("transcript") or "")[:100]
        if rc_txt:
            ctx_lines.append(
                f"  [{rc.get('category','?')}] {rc.get('tag') or 'TGID '+str(rc.get('tgid','?'))}: {rc_txt}"
            )
    context_block = "\n".join(ctx_lines) if ctx_lines else "  (none)"
    
    user_msg = (
        f"Agency: {call.get('category','Unknown')}\n"
        f"Talkgroup: {call.get('tag') or 'TGID '+str(call.get('tgid',0))}\n"
        f"Location hint: {call.get('location') or 'unknown'}\n"
        f"Transcript: {transcript}\n\n"
        f"Recent calls (last 15 min):\n{context_block}"
    )
    
    try:
        result = _call_groq_llm(_GROQ_SYSTEM, user_msg)
        itype  = result.get("incident_type") or "ROUTINE"
        pri    = result.get("priority", "NONE")
        hold   = result.get("should_hold", False)
        reason = (result.get("reasoning") or "")[:100]
        print(f"[groq] {call.get('tag','?')} → {itype} pri={pri} hold={hold} | {reason}", flush=True)                
        
        now_post = time.time()
        if itype in (None, "ROUTINE"):
            tr = _groq_routine_tracker.setdefault(tgid, {"streak": 0, "cooldown_until": 0.0, "last_ts": 0.0})
            if now_post - tr["last_ts"] < _GROQ_ROUTINE_WINDOW:
                tr["streak"] += 1
            else:
                tr["streak"] = 1
            tr["last_ts"] = now_post
            if tr["streak"] >= _GROQ_ROUTINE_STREAK:
                tr["cooldown_until"] = now_post + _GROQ_ROUTINE_COOLDOWN
                tr["streak"] = 0
        else:
            _groq_routine_tracker.pop(tgid, None)
        return result
    except Exception as exc:
        print(f"[groq] error: {exc}", flush=True)
        if "429" in str(exc):
            return {"error": "RATE_LIMITED", "backoff": _GROQ_BACKOFF_SECS}
        return None

def groq_identify_tgid(tgid: int, transcript: str, GROQ_ENABLED, _TGID_ID_MIN_LEN, 
                       _TGID_ID_SYSTEM, _call_groq_llm, DB_PATH, _TGID_ID_CONFIRM_THRESHOLD,
                       TALK_USER, TALK_PASS, TALK_BASE, TALK_ROOMS) -> Optional[dict]:
    """Ask Groq to guess what agency/role this unknown talkgroup belongs to."""
    if not GROQ_ENABLED or not transcript or len(transcript) < _TGID_ID_MIN_LEN:
        return None

    try:
        user_msg = (
            f"TGID {tgid} on GATRRS Austin/Travis County P25 system.\n"
            f"Radio transcript: {transcript}\n\n"
            f"What Austin/Travis County public safety agency and role does this talkgroup belong to?"
        )
        raw = _call_groq_llm(_TGID_ID_SYSTEM, user_msg)
        guess     = raw.get("guess")
        agency    = raw.get("agency")
        reasoning = raw.get("reasoning", "")
        raw_conf = raw.get("confidence", "LOW")
        if isinstance(raw_conf, (int, float)):
            conf = "HIGH" if raw_conf >= 0.75 else ("MED" if raw_conf >= 0.5 else "LOW")
        else:
            conf = str(raw_conf).upper() if str(raw_conf).upper() in ("HIGH", "MED", "LOW") else "LOW"

        if not guess:
            return None

        ts = time.time()
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO tgid_guesses (tgid, ts, guess, category, confidence, reasoning, transcript) "
            "VALUES (?,?,?,?,?,?,?)",
            (tgid, ts, guess, agency, conf, reasoning, transcript[:200])
        )
        conn.commit()

        if conf in ("HIGH", "MED"):
            rows = conn.execute(
                "SELECT guess FROM tgid_guesses WHERE tgid=? AND confirmed=0 AND confidence IN ('HIGH','MED')",
                (tgid,)
            ).fetchall()
            guesses = [r[0] for r in rows]
            if len(guesses) >= _TGID_ID_CONFIRM_THRESHOLD:
                from collections import Counter
                top_guess, top_count = Counter(guesses).most_common(1)[0]
                if top_count >= _TGID_ID_CONFIRM_THRESHOLD:
                    conn.execute("UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid,))
                    conn.commit()
                    print(f"[tgid-id] AUTO-CONFIRMED tgid={tgid} → {top_guess!r} ({top_count} agreeing guesses)", flush=True)
                    _notify_tgid_confirmed(tgid, top_guess, agency, top_count, TALK_USER, TALK_PASS, TALK_BASE, TALK_ROOMS)

        conn.close()
        print(f"[tgid-id] tgid={tgid} guess={guess!r} conf={conf}", flush=True)
        return raw

    except Exception as exc:
        print(f"[tgid-id] error for tgid={tgid}: {exc}", flush=True)
        return None

def _notify_tgid_confirmed(tgid: int, name: str, agency: str, count: int, 
                           TALK_USER, TALK_PASS, TALK_BASE, TALK_ROOMS):
    """Post a Talk message when a TGID gets auto-confirmed."""
    msg = (
        f"🔍 **Unknown talkgroup identified!**\n"
        f"TGID {tgid} → **{name}** (agency: {agency or 'unknown'})\n"
        f"Auto-confirmed from {count} agreeing Groq guesses.\n"
        f"Review at /api/tgid_guesses — run `/addtag {tgid} {name}` to write to tags file."
    )
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    payload = urllib.parse.urlencode({"message": msg}).encode()
    req = urllib.request.Request(
        f"{TALK_BASE}/chat/{TALK_ROOMS['general']}",
        data=payload,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[tgid-id] notify failed: {e}", flush=True)
