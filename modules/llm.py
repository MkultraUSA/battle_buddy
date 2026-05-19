import base64
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter

from modules.config import (
    DB_PATH,
    OPENROUTER_API_BASE,
    OPENROUTER_API_KEY,
    OPENROUTER_ENABLED,
    OPENROUTER_MODEL_CACHE_SECS,
    OPENROUTER_RECOMMENDATIONS_URL,
    TALK_BASE,
    TALK_PASS,
    TALK_ROOMS,
    TALK_USER,
)

try:
    import anthropic as _anthropic_mod
except ImportError:
    _anthropic_mod = None


# ---------------------------------------------------------------------------
# OpenRouter model auto-switching via recommendations.json
# ---------------------------------------------------------------------------
# The recommendations endpoint is updated every 15 minutes.  We cache the
# result locally to avoid hammering it on every LLM call.
# ---------------------------------------------------------------------------

_recommendations_cache: dict = {}
_recommendations_cache_ts: float = 0.0
_recommendations_lock = threading.Lock()

# Models that are known to be unreliable even if they appear in the list.
# Hard-deny only models PROVEN broken through repeated observation.
# Everything else: let the runtime ban + health system handle it adaptively.
_MODEL_DENYLIST = {
    "openrouter/free",                # meta-router, unpredictable routing
    "minimax/minimax-m2.5:free",      # returns null content for JSON (proven repeatedly)
}

# ── Adaptive runtime bans with escalating backoff ───────────────────────────
# Instead of a flat 5-min ban, each repeat offense DOUBLES the ban duration
# (base 2 min, capped at 1 hour).  Offense counts reset after the model
# works successfully for _HEALTH_GRACE_WINDOW seconds.
# ────────────────────────────────────────────────────────────────────────────
_runtime_bans: dict[str, float] = {}          # model_id → ban_until_ts
_ban_offenses: dict[str, int] = {}            # model_id → consecutive offense count
_BASE_BAN_SECS = 120                           # base: 2 minutes
_MAX_BAN_SECS = 3600                           # cap: 1 hour
_HEALTH_GRACE_WINDOW = 900                     # 15 min of good behavior resets offense count

# ── Per-model health tracking ───────────────────────────────────────────────
# Track recent success/fail to prefer models with better track records.
# ────────────────────────────────────────────────────────────────────────────
_model_health: dict[str, dict] = {}            # model_id → {"success": N, "fail": N, "last_success_ts": t}
_HEALTH_MAX_SAMPLES = 20                       # sliding window of recent outcomes


def _record_model_success(model_id: str) -> None:
    """Record a successful call — resets offense count after grace window."""
    h = _model_health.setdefault(model_id, {"success": 0, "fail": 0, "last_success_ts": 0.0})
    h["success"] = min(h["success"] + 1, _HEALTH_MAX_SAMPLES)
    h["last_success_ts"] = time.time()
    # Reset offense count after sustained good behavior
    if model_id in _ban_offenses:
        if time.time() - (_runtime_bans.get(model_id, 0) + _HEALTH_GRACE_WINDOW) > 0:
            del _ban_offenses[model_id]


def _record_model_failure(model_id: str) -> None:
    """Record a failed call."""
    h = _model_health.setdefault(model_id, {"success": 0, "fail": 0, "last_success_ts": 0.0})
    h["fail"] = min(h["fail"] + 1, _HEALTH_MAX_SAMPLES)


def _model_success_ratio(model_id: str) -> float:
    """Return 0.0–1.0 success ratio from recent health window, or 0.5 for unknown."""
    h = _model_health.get(model_id)
    if not h:
        return 0.5
    total = h["success"] + h["fail"]
    return h["success"] / total if total > 0 else 0.5


def _is_runtime_banned(model_id: str) -> bool:
    """True if *model_id* is currently serving a cool-down."""
    now = time.time()
    stale = [mid for mid, until in _runtime_bans.items() if now >= until]
    for mid in stale:
        del _runtime_bans[mid]
        # Don't clear offense count on natural expiry — only on success
    return model_id in _runtime_bans


def _runtime_ban_model(model_id: str, reason: str = "rate limited") -> None:
    """Ban *model_id* with escalating duration.  Each repeat offense doubles the ban."""
    offenses = _ban_offenses.get(model_id, 0) + 1
    _ban_offenses[model_id] = offenses
    duration = min(_BASE_BAN_SECS * (2 ** (offenses - 1)), _MAX_BAN_SECS)
    if duration >= 600:
        duration_str = f"{duration}s ({duration//60}min)"
    else:
        duration_str = f"{duration}s"
    _runtime_bans[model_id] = time.time() + duration
    print(f"[llm] runtime-ban {model_id} for {duration_str} (offense #{offenses}, reason: {reason})",
          flush=True)


def _fetch_recommendations() -> dict:
    """Fetch and cache the OpenRouter free-model recommendations JSON.
    Returns the parsed JSON dict (the full response, including 'recommendations' list).
    On failure, returns the stale cache or an empty dict.
    """
    global _recommendations_cache, _recommendations_cache_ts
    now = time.time()
    with _recommendations_lock:
        if now - _recommendations_cache_ts < OPENROUTER_MODEL_CACHE_SECS and _recommendations_cache:
            return _recommendations_cache
    try:
        req = urllib.request.Request(
            OPENROUTER_RECOMMENDATIONS_URL,
            headers={"User-Agent": "BattleBuddy/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        with _recommendations_lock:
            _recommendations_cache = data
            _recommendations_cache_ts = time.time()
        return data
    except Exception as exc:
        print(f"[llm] WARN failed to fetch recommendations: {exc}", flush=True)
        with _recommendations_lock:
            return _recommendations_cache


def _pick_best_model(recommendations: dict) -> str | None:
    """Pick the best available free model from the recommendations list.
    Filters out denylisted models, runtime-banned models, and those with
    status != 'online'.  Among eligible models, prefers higher probe score
    and better recent health (success ratio).
    Returns the model_id string or None if nothing suitable found.
    """
    recs = recommendations.get("recommendations") or []

    # Gather all eligible models with their scores
    eligible: list[tuple[float, str]] = []  # (composite_score, model_id)
    for rec in recs:
        model_id = rec.get("model_id") or ""
        if model_id in _MODEL_DENYLIST:
            continue
        if _is_runtime_banned(model_id):
            continue
        if rec.get("status") != "online" and rec.get("status") != "catalog_only":
            continue
        if not rec.get("supports_json"):
            continue
        # Composite score: probe score (0-100) * health ratio + health bonus
        probe_score = rec.get("score", 50)
        health_ratio = _model_success_ratio(model_id)
        # Weight: 70% probe score, 30% health (favors models that actually work)
        composite = (probe_score * 0.7) + (health_ratio * 100 * 0.3)
        eligible.append((composite, model_id))

    if eligible:
        eligible.sort(reverse=True)
        best_score, best_model = eligible[0]
        if len(eligible) > 1:
            runners = ", ".join(f"{m}({s:.0f})" for s, m in eligible[:3])
            print(f"[llm] model candidates: {runners}", flush=True)
        return best_model

    # Pass 2 — all eligible models are runtime-banned; clear bans and retry
    if _runtime_bans:
        print("[llm] all eligible models runtime-banned — clearing cooldowns",
              flush=True)
        _runtime_bans.clear()
        for rec in recs:
            model_id = rec.get("model_id") or ""
            if model_id in _MODEL_DENYLIST:
                continue
            if rec.get("status") not in ("online", "catalog_only"):
                continue
            if not rec.get("supports_json"):
                continue
            return model_id

    return None


def _get_effective_model() -> str:
    """Pick the best available free model from the recommendations endpoint.
    Falls back to a hardcoded safe model if no recommendation is available.
    Now includes catalog_only models (not just online) since many valid
    free models appear as catalog_only in the probe data.
    """
    recs = _fetch_recommendations()
    model = _pick_best_model(recs)
    if model:
        print(f"[llm] auto-selected model: {model}", flush=True)
        return model
    # No model from recommendations — try to find any model that supports JSON
    for rec in recs.get("recommendations") or []:
        if rec.get("status") in ("online", "catalog_only") and rec.get("supports_json"):
            mid = rec.get("model_id", "")
            if mid not in _MODEL_DENYLIST and not _is_runtime_banned(mid):
                print(f"[llm] fallback to JSON model: {mid}", flush=True)
                return mid
    # Absolute last resort: use a known free model even if it may not support JSON.
    SAFE_FALLBACK = "openai/gpt-oss-20b:free"
    print(f"[llm] no suitable JSON model found — falling back to {SAFE_FALLBACK}", flush=True)
    return SAFE_FALLBACK


# ── Inter-call throttling for free models ───────────────────────────────────
# Free models get rate-limited aggressively.  Track per-model call timing
# and enforce a minimum interval that adapts based on recent 429s.
# ────────────────────────────────────────────────────────────────────────────
_last_call_time: dict[str, float] = {}          # model_id → timestamp of last call
_inter_call_delay: dict[str, float] = {}        # model_id → current minimum delay (seconds)
_BASE_FREE_DELAY = 2.0                           # minimum 2s between free-model calls
_PENALTY_DELAY = 5.0                             # increased delay after 429
_DELAY_DECAY = 0.9                               # decay factor per successful call
_throttle_lock = threading.Lock()


def _throttle_free_model(model_id: str) -> None:
    """Sleep if needed to respect per-model inter-call delay for free models.
    Only applies to models with ':free' in the name.
    """
    if ":free" not in model_id:
        return
    with _throttle_lock:
        delay = _inter_call_delay.get(model_id, _BASE_FREE_DELAY)
        last = _last_call_time.get(model_id, 0)
        elapsed = time.time() - last
        if elapsed < delay:
            sleep_for = delay - elapsed
            time.sleep(sleep_for)
        _last_call_time[model_id] = time.time()


def _adjust_throttle(model_id: str, success: bool) -> None:
    """Adjust inter-call delay: increase on failure, decay on success."""
    if ":free" not in model_id:
        return
    with _throttle_lock:
        current = _inter_call_delay.get(model_id, _BASE_FREE_DELAY)
        if success:
            # Decay toward base delay
            new_delay = max(_BASE_FREE_DELAY, current * _DELAY_DECAY)
        else:
            # Increase delay after 429 / failure
            new_delay = min(current + _PENALTY_DELAY, 15.0)
        _inter_call_delay[model_id] = new_delay



# ---------------------------------------------------------------------------
# Core LLM call with retry
# ---------------------------------------------------------------------------

def _call_openrouter_llm(system_prompt: str, user_msg: str, max_retries: int = 3) -> dict:
    """Call OpenRouter chat/completions with adaptive backoff retry.
    Applies inter-call throttling for free models, records health outcomes
    for model selection, and uses escalating runtime bans on failure.
    """
    last_exc = None
    model = None
    for attempt in range(max_retries):
        model = _get_effective_model()
        # Throttle before calling — prevents hammering free models
        _throttle_free_model(model)
        success = False
        try:
            payload = json.dumps({
                "model":           model,
                "messages":        [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_msg},
                ],
                "max_tokens":      300,
                "temperature":     0.1,
                "response_format": {"type": "json_object"},
            }).encode()
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
                "User-Agent":    "Mozilla/5.0",
                "HTTP-Referer":  "https://battlebuddy.news",
            }
            req = urllib.request.Request(
                f"{OPENROUTER_API_BASE}/chat/completions",
                data=payload,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            if "error" in data:
                err = data["error"]
                err_msg = err.get("message", str(err))
                err_code = err.get("code", 0)
                if err_code == 429 or "429" in str(err_msg) or "rate" in str(err_msg).lower():
                    _runtime_ban_model(model, reason="HTTP 429")
                    _record_model_failure(model)
                    _adjust_throttle(model, False)
                    raise Exception(f"429 rate limit: {err_msg}")
                raise Exception(f"API error ({err_code}): {err_msg}")
            content = data["choices"][0]["message"]["content"]
            if content is None:
                _runtime_ban_model(model, reason="null content")
                _record_model_failure(model)
                _adjust_throttle(model, False)
                raise Exception(f"model returned null content (does not support JSON output): {model}")
            result = json.loads(content)
            success = True
            _record_model_success(model)
            _adjust_throttle(model, True)
            return result
        except Exception as exc:
            last_exc = exc
            if not success:  # only record failure if we didn't already above
                if "429" in str(exc) or "null content" in str(exc):
                    pass  # already recorded in the specific handler
                else:
                    _record_model_failure(model)
                    _adjust_throttle(model, False)
            if "429" in str(exc):
                wait = (2 ** attempt) * 5
                print(f"[llm] 429 rate limited (attempt {attempt+1}/{max_retries}), waiting {wait}s", flush=True)
                time.sleep(wait)
            elif attempt < max_retries - 1:
                wait = (2 ** attempt) * 2
                print(f"[llm] error (attempt {attempt+1}/{max_retries}): {exc}, retrying in {wait}s", flush=True)
                time.sleep(wait)
            else:
                raise
    raise last_exc


# ---------------------------------------------------------------------------
# Incident detection system prompt
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """You are the incident detection brain for Battle Buddy, a real-time P25 radio monitoring system covering Austin/Travis County emergency services on the GATRRS trunked system.

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

_llm_call_times: list      = []
_llm_rate_lock             = threading.Lock()
_llm_backoff_until         = 0.0
_LLM_MIN_TRANSCRIPT        = 50
_LLM_MIN_DURATION          = 2.5
_LLM_BACKOFF_SECS          = 600
_LLM_ROUTINE_STREAK        = 3
_LLM_ROUTINE_WINDOW        = 600
_LLM_ROUTINE_COOLDOWN      = 300
_LLM_SAFETY_RE             = re.compile(
    r"shoot|shot|weapon|gun|stab|assault|pursuit|chase|crash|fire|smoke|explosion|"
    r"officer down|man down|unconscious|not breathing|overdose|hostage|threat|bomb",
    re.IGNORECASE,
)
_llm_routine_tracker: dict = {}


# --- Classification config (self-improving constraints) ---------------------

_CLASSIFICATION_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "classification_config.json"
)
try:
    with open(_CLASSIFICATION_CONFIG_PATH, "r") as _f:
        CLASSIFICATION_CONFIG = json.load(_f)
except Exception as _exc:
    print(f"[llm] WARN could not load classification_config.json: {_exc}", flush=True)
    CLASSIFICATION_CONFIG = {}


def build_classification_rules_text() -> str:
    """Render CLASSIFICATION_CONFIG into a constraints block appended to the
    base system prompt at call time. Never mutates _LLM_SYSTEM."""
    if not CLASSIFICATION_CONFIG:
        return ""
    lines = ["", "ADDITIONAL CLASSIFICATION CONSTRAINTS (per-incident-type rules):"]
    for itype, rules in CLASSIFICATION_CONFIG.items():
        parts = [f"- {itype}:"]
        tgids = rules.get("trusted_tgids") or []
        names = rules.get("trusted_tgid_names") or []
        if tgids:
            tg_label = ", ".join(
                f"{t} ({n})" for t, n in zip(tgids, names)
            ) if names and len(names) == len(tgids) else ", ".join(str(t) for t in tgids)
            parts.append(f"trusted talkgroups: {tg_label}")
        min_calls = rules.get("required_min_calls", 1)
        if min_calls and min_calls > 1:
            parts.append(f"requires >= {min_calls} corroborating calls")
        kw_req = rules.get("keywords_required") or []
        if kw_req:
            parts.append(f"required keywords: {kw_req}")
        kw_ex = rules.get("keywords_excluded") or []
        if kw_ex:
            parts.append(f"excluded jargon (do NOT classify if transcript contains any): {kw_ex}")
        notes = rules.get("notes") or ""
        if notes:
            parts.append(f"notes: {notes}")
        lines.append("  " + " | ".join(parts))
    lines.append(
        "If a call's talkgroup is not in an incident type's trusted_tgids list "
        "(when that list is non-empty), you MUST NOT classify the call as that "
        "type. Prefer ROUTINE when constraints are not met."
    )
    return "\n".join(lines)


def _per_call_tgid_restrictions(tgid: int) -> str:
    """Return an explicit per-call restriction string for the user message
    listing incident types this tgid is NOT allowed to be classified as."""
    blocked = []
    for itype, rules in CLASSIFICATION_CONFIG.items():
        trusted = rules.get("trusted_tgids") or []
        if trusted and tgid not in trusted:
            blocked.append(itype)
    if not blocked:
        return ""
    return (
        "\nHARD RESTRICTION: this call's talkgroup is NOT in the trusted list "
        "for the following incident types. Do NOT classify as any of these: "
        + ", ".join(blocked)
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def llm_analyze(call: dict, recent_calls_list: list):
    """Analyze a radio call for incident detection using OpenRouter LLM.
    Returns a dict with incident_type, priority, should_hold, description,
    escalation_stage, reasoning — or None if skipped/error.
    """
    global _llm_backoff_until, _llm_call_times
    if not OPENROUTER_ENABLED:
        return None
    if call.get("tgid", 0) == 0:
        return None
    transcript = call.get("transcript") or ""
    if not transcript or len(transcript) < _LLM_MIN_TRANSCRIPT:
        return None
    if call.get("duration", 99.0) < _LLM_MIN_DURATION:
        return None
    tgid = call.get("tgid", 0)
    if not _LLM_SAFETY_RE.search(transcript):
        now_pre = time.time()
        tracker = _llm_routine_tracker.get(tgid)
        if tracker and now_pre < tracker.get("cooldown_until", 0):
            return None
    now = time.time()
    with _llm_rate_lock:
        if now < _llm_backoff_until:
            return None
        _llm_call_times[:] = [t for t in _llm_call_times if now - t < 60]
        _llm_call_times.append(now)

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
    rules_text = build_classification_rules_text()
    system_prompt = _LLM_SYSTEM + ("\n" + rules_text if rules_text else "")
    user_msg = user_msg + _per_call_tgid_restrictions(tgid)
    try:
        result = _call_openrouter_llm(system_prompt, user_msg)
        itype  = result.get("incident_type") or "ROUTINE"
        pri    = result.get("priority", "NONE")
        hold   = result.get("should_hold", False)
        reason = (result.get("reasoning") or "")[:100]
        print(f"[llm] {call.get('tag','?')} → {itype} pri={pri} hold={hold} | {reason}", flush=True)
        now_post = time.time()
        if itype in (None, "ROUTINE"):
            tr = _llm_routine_tracker.setdefault(tgid, {"streak": 0, "cooldown_until": 0.0, "last_ts": 0.0})
            if now_post - tr["last_ts"] < _LLM_ROUTINE_WINDOW:
                tr["streak"] += 1
            else:
                tr["streak"] = 1
            tr["last_ts"] = now_post
            if tr["streak"] >= _LLM_ROUTINE_STREAK:
                tr["cooldown_until"] = now_post + _LLM_ROUTINE_COOLDOWN
                tr["streak"] = 0
        else:
            _llm_routine_tracker.pop(tgid, None)
        return result
    except Exception as exc:
        print(f"[llm] error: {exc}", flush=True)
        if "429" in str(exc):
            _llm_backoff_until = time.time() + _LLM_BACKOFF_SECS
            print(f"[llm] rate limited — backing off {_LLM_BACKOFF_SECS}s", flush=True)
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


def llm_identify_tgid(tgid: int, transcript: str):
    """Identify an unknown talkgroup using OpenRouter LLM.
    Returns the raw LLM response dict or None.
    """
    if not OPENROUTER_ENABLED or not transcript or len(transcript) < _TGID_ID_MIN_LEN:
        return None
    try:
        user_msg = (
            f"TGID {tgid} on GATRRS Austin/Travis County P25 system.\n"
            f"Radio transcript: {transcript}\n\n"
            f"What Austin/Travis County public safety agency and role does this talkgroup belong to?"
        )
        raw       = _call_openrouter_llm(_TGID_ID_SYSTEM, user_msg)
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
        f"Auto-confirmed from {count} agreeing LLM guesses.\n"
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
