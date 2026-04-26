import base64
import json
import sqlite3
import threading
import time
import urllib.request
import urllib.parse
from collections import Counter

from modules.config import (
    GROQ_API_KEY, GROQ_MODEL, GROQ_ENABLED, GROQ_API_BASE,
    ANTHROPIC_API_KEY, ANTHROPIC_ENABLED,
    TALK_BASE, TALK_USER, TALK_PASS, TALK_ROOMS, DB_PATH,
)

try:
    import anthropic as _anthropic_mod
except ImportError:
    _anthropic_mod = None


def _call_groq_llm(system_prompt: str, user_msg: str) -> dict:
    req = urllib.request.Request(
        f"{GROQ_API_BASE}/chat/completions",
        data=json.dumps({
            "model":           GROQ_MODEL,
            "messages":        [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            "max_tokens":      300,
            "temperature":     0.1,
            "response_format": {"type": "json_object"},
        }).encode(),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type":  "application/json",
            "User-Agent":    "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read())
    return json.loads(data["choices"][0]["message"]["content"])


_GROQ_SYSTEM = """You are the incident detection brain for Battle Buddy, a real-time P25 radio monitoring system covering Austin/Travis County emergency services on the GATRRS trunked system.

Austin agencies: APD (police), AFD (Austin Fire Dept), TCEMS (Travis County EMS), TCFD (Travis County Fire), ABIA (Austin-Bergstrom Airport), TCSO (Travis County Sheriff), DPS (Texas Dept of Public Safety), UTPD (UT Police).

Analyze the radio call transcript and recent context. Respond ONLY with a JSON object — no prose, no markdown.

Required JSON fields:
{
  "incident_type": <one of the values below, or null for routine>,
  "priority": <"HIGH" | "MED" | "NONE">,
  "should_hold": <true | false>,
  "description": <one concise sentence describing the event>,
  "escalation_stage": <"welfare"|"disturbance"|"pursuit"|"weapons"|"backup"|"tactical"|"k9"|"air" or null>,
  "reasoning": <one sentence explaining your decision>
}

Valid incident_type values:
  "OFFICER DOWN", "SHOOTING", "STABBING", "AIRCRAFT EMERGENCY", "MASS CASUALTY",
  "STRUCTURE FIRE", "HAZMAT", "HOSTAGE/BARRICADE", "CRASH/COLLISION", "FATAL CRASH",
  "FIRE DISPATCH", "TRANSIT INCIDENT", "AIRPORT EMERGENCY",
  "MULTI-AGENCY RESPONSE", "APD SURGE", "AIR ASSET ACTIVE", "DPS CAPITOL ACTIVATION",
  "DEATH INVESTIGATION"

Priority rules:
  HIGH — active life threat: officer down, shooting, structure fire, mass casualty, hostage/barricade, aircraft emergency
  NOTE: EMS/AFD reporting "GSW", "gunshot wound", or "gunshot victim" = SHOOTING even without APD confirmation.
  MED  — significant incident: crash, hazmat, fire dispatch, multi-agency, surge, transit, airport alert, death investigation
  NONE — routine: traffic stop, medical assist, disturbance, welfare check, normal patrol, minor fender bender

should_hold: true only if the event is actively unfolding and worth continuous monitoring.

Be conservative. Most radio traffic is routine. Only flag genuine emergencies."""

_groq_call_times: list     = []
_groq_rate_lock            = threading.Lock()
_groq_backoff_until        = 0.0
_GROQ_RATE_LIMIT           = 4
_GROQ_MIN_TRANSCRIPT       = 50
_GROQ_MIN_DURATION         = 2.5
_GROQ_BACKOFF_SECS         = 600
_GROQ_ROUTINE_STREAK       = 3
_GROQ_ROUTINE_WINDOW       = 600
_GROQ_ROUTINE_COOLDOWN     = 300
_GROQ_SAFETY_RE            = __import__('re').compile(
    r"shoot|shot|weapon|gun|stab|assault|pursuit|chase|crash|fire|smoke|explosion|"
    r"officer down|man down|unconscious|not breathing|overdose|hostage|threat|bomb",
    __import__('re').IGNORECASE
)
_groq_routine_tracker: dict = {}


def groq_analyze(call: dict, recent_calls_list: list):
    global _groq_backoff_until, _groq_call_times
    if not GROQ_ENABLED:
        return None
    if call.get("tgid", 0) == 0:
        return None
    transcript = call.get("transcript") or ""
    if not transcript or len(transcript) < _GROQ_MIN_TRANSCRIPT:
        return None
    if call.get("duration", 99.0) < _GROQ_MIN_DURATION:
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
    for rc in recent_calls_list[-6:]:
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
            _groq_backoff_until = time.time() + _GROQ_BACKOFF_SECS
            print(f"[groq] rate limited — backing off {_GROQ_BACKOFF_SECS}s", flush=True)
        return None


_TGID_ID_SYSTEM = """You are a P25 radio talkgroup analyst for the GATRRS trunked system covering Austin, TX and Travis County.

You will receive a radio transcript from an UNKNOWN talkgroup and your job is to guess which Austin/Travis County public safety agency this talkgroup belongs to, and suggest a short name for it.

Known agencies on GATRRS:
- APD: Austin Police Department (patrol, ops, dispatch, detective, SWAT)
- AFD: Austin Fire Department (fire suppression, EMS first response, Locution dispatch)
- TCEMS: Travis County EMS (paramedic units, ambulances)
- TCFD: Travis County Fire/EMS (suburban fire districts)
- TCSO: Travis County Sheriff's Office (patrol, jail, civil)
- UTPD: UT Austin Police
- DPS: Texas Dept of Public Safety (troopers, Capitol Police)
- ABIA: Austin-Bergstrom International Airport operations
- Cap Metro: Capital Metro transit police/operations
- Williamson/Hays: neighboring county agencies
- City utilities: Austin Water, Austin Energy (not public safety)

Respond ONLY with a JSON object:
{
  "guess": <short name for this talkgroup, e.g. "APD South Patrol" or "TCEMS Medic Ops">,
  "agency": <top-level agency abbreviation, e.g. "APD">,
  "confidence": <"HIGH" | "MED" | "LOW">,
  "reasoning": <one sentence explaining your guess based on the radio chatter>
}

If the transcript is too short or garbled to make any guess, set confidence to "LOW" and guess to null."""

_TGID_ID_MIN_LEN           = 15
_TGID_ID_CONFIRM_THRESHOLD = 3


def groq_identify_tgid(tgid: int, transcript: str):
    if not GROQ_ENABLED or not transcript or len(transcript) < _TGID_ID_MIN_LEN:
        return None
    try:
        user_msg = (
            f"TGID {tgid} on GATRRS Austin/Travis County P25 system.\n"
            f"Radio transcript: {transcript}\n\n"
            f"What Austin/Travis County public safety agency and role does this talkgroup belong to?"
        )
        raw       = _call_groq_llm(_TGID_ID_SYSTEM, user_msg)
        guess     = raw.get("guess")
        agency    = raw.get("agency")
        reasoning = raw.get("reasoning", "")
        raw_conf  = raw.get("confidence", "LOW")
        if isinstance(raw_conf, (int, float)):
            conf = "HIGH" if raw_conf >= 0.75 else ("MED" if raw_conf >= 0.5 else "LOW")
        else:
            conf = str(raw_conf).upper() if str(raw_conf).upper() in ("HIGH", "MED", "LOW") else "LOW"
        if not guess:
            return None
        ts   = time.time()
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
                top_guess, top_count = Counter(guesses).most_common(1)[0]
                if top_count >= _TGID_ID_CONFIRM_THRESHOLD:
                    conn.execute("UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid,))
                    conn.commit()
                    print(f"[tgid-id] AUTO-CONFIRMED tgid={tgid} → {top_guess!r}", flush=True)
                    _notify_tgid_confirmed(tgid, top_guess, agency, top_count)
        conn.close()
        print(f"[tgid-id] tgid={tgid} guess={guess!r} conf={conf}", flush=True)
        return raw
    except Exception as exc:
        print(f"[tgid-id] error for tgid={tgid}: {exc}", flush=True)
        return None


def _notify_tgid_confirmed(tgid: int, name: str, agency: str, count: int):
    msg = (
        f"🔍 **Unknown talkgroup identified!**\n"
        f"TGID {tgid} → **{name}** (agency: {agency or 'unknown'})\n"
        f"Auto-confirmed from {count} agreeing Groq guesses.\n"
        f"Review at /api/tgid_guesses — run `/addtag {tgid} {name}` to write to tags file."
    )
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
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
