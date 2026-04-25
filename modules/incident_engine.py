"""
Battle Buddy — Incident Engine

Ported from audio_receiver.py: analyze_for_incident and all related helpers.

This module is self-contained with respect to incident detection and state
management.  It intentionally avoids importing from audio_receiver.py so that
audio_receiver.py can import *this* module without a circular dependency.

Notification side-effects (Talk posts, Mailgun emails, Deck cards, ATAK markers,
commute alerts) are injected via callback hooks so that callers can wire up
whichever notification back-ends they need.

Usage in audio_receiver.py:
    from modules.incident_engine import (
        analyze_for_incident,
        incident_cleanup_thread,
        register_callbacks,
        load_active_incidents,
    )

    register_callbacks(
        on_create=lambda inc_id, data: ...,   # called when a new incident is created
        on_update=lambda inc_id, data: ...,   # called when an existing incident is updated
        on_clear=lambda inc_id, itype: ...,   # called when an incident is auto-cleared
        on_escalation=lambda inc_id, stage, msg: ...,  # called on escalation step
    )
"""

import json
import sqlite3
import threading
import time

from modules.config import (
    DB_PATH,
    LOCUTION_TGIDS,
    TRANSIT_TGIDS,
    ABIA_ALERT_TGIDS,
    ABIA_OPS_TGIDS,
    INCIDENT_KEYWORDS,
    MULTIAGENCY_WINDOW_MIN,
    APD_SURGE_WINDOW_MIN,
    APD_SURGE_THRESHOLD,
    HOLD_ENABLED,
    HOLD_RELEASE_MINUTES,
    ESCALATION_STAGES,
    ESCALATION_STAGE_NAMES,
    ITYPE_SEVERITY,
    ITYPE_MERGE_COMPAT,
    INCIDENT_TIMEOUT_MINUTES,
    INCIDENT_TIMEOUT_DEFAULT,
    INCIDENT_LOCATION_RADIUS_KM,
    TGID_TIER,
    ESCALATION_MIN_TIER,
)
from modules.utils import (
    apply_locution_corrections,
    detect_air_asset,
    detect_dps_assets,
    get_calls_since,
    haversine_km,
    is_capitol_area,
    mentions_dps,
)

# ---------------------------------------------------------------------------
# In-memory incident state
#   _active_incidents: {db_id: {itype, ts_updated, agencies, tgids,
#                               lat, lon, location, escalation_stage,
#                               room_tokens}}
# ---------------------------------------------------------------------------
_active_incidents: dict[int, dict] = {}
_incident_lock = threading.Lock()

# ---------------------------------------------------------------------------
# OP25 hold state
# ---------------------------------------------------------------------------
_current_hold_tgid: int | None = None
_last_hold_activity: float = 0.0
_hold_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Notification callbacks — registered by the caller (audio_receiver.py)
# ---------------------------------------------------------------------------
_cb_on_create = None      # (inc_id, inc_data) -> None
_cb_on_update = None      # (inc_id, inc_data) -> None
_cb_on_clear = None       # (inc_id, itype)    -> None
_cb_on_escalation = None  # (inc_id, stage, msg) -> None


def register_callbacks(
    on_create=None,
    on_update=None,
    on_clear=None,
    on_escalation=None,
):
    """
    Register notification callbacks.  All arguments are optional; pass only the
    hooks you need.  Each hook is called in a daemon thread so it never blocks
    the incident engine.

    on_create(inc_id: int, data: dict)
        Called once when a new incident row is created.
        data keys: itype, description, agencies (JSON str), location, ts_start,
                   lat, lon, call (the triggering call dict)

    on_update(inc_id: int, data: dict)
        Called each time an existing incident is updated.
        data keys: itype, description, agencies (JSON str), ts_updated, call

    on_clear(inc_id: int, itype: str)
        Called when an incident is auto-cleared by the timeout watchdog.

    on_escalation(inc_id: int, stage: str, message: str)
        Called when an escalation step is detected and recorded.
    """
    global _cb_on_create, _cb_on_update, _cb_on_clear, _cb_on_escalation
    if on_create is not None:
        _cb_on_create = on_create
    if on_update is not None:
        _cb_on_update = on_update
    if on_clear is not None:
        _cb_on_clear = on_clear
    if on_escalation is not None:
        _cb_on_escalation = on_escalation


def _fire(cb, *args):
    """Run callback in a daemon thread, swallowing any exceptions."""
    if cb is None:
        return
    def _run():
        try:
            cb(*args)
        except Exception as exc:
            print(f"[incident_engine] callback error: {exc}", flush=True)
    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Bootstrap — load active incidents from DB at startup
# ---------------------------------------------------------------------------

def load_active_incidents():
    """
    Populate _active_incidents from the DB.  Call once at application start
    (after init_db()) so that in-flight incidents survive a process restart.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status='active'"
    ).fetchall()
    conn.close()
    with _incident_lock:
        for row in rows:
            inc = dict(row)
            try:
                agencies = set(json.loads(inc.get("agencies") or "[]"))
            except Exception:
                agencies = set()
            try:
                tgids = set(json.loads(inc.get("tgids") or "[]"))
            except Exception:
                tgids = set()
            _active_incidents[inc["id"]] = {
                "itype":            inc["itype"],
                "ts_updated":       inc["ts_updated"],
                "agencies":         agencies,
                "tgids":            tgids,
                "lat":              inc.get("lat"),
                "lon":              inc.get("lon"),
                "location":         inc.get("location"),
                "escalation_stage": None,
                "room_tokens":      [],
            }
    print(f"[incident_engine] loaded {len(_active_incidents)} active incident(s) from DB",
          flush=True)


# ---------------------------------------------------------------------------
# Escalation helpers
# ---------------------------------------------------------------------------

def _detect_escalation_stage(text: str) -> str | None:
    """Return the highest-indexed escalation stage keyword found in transcript."""
    text = text.lower()
    matched = None
    for stage, keywords in ESCALATION_STAGES:
        if any(kw in text for kw in keywords):
            matched = stage
    return matched  # returns the highest match because we iterate in order


def _find_incident_by_location(lat: float, lon: float, ts: float) -> int | None:
    """Return active incident ID if a recent incident is within radius of this call."""
    if lat is None or lon is None:
        return None
    with _incident_lock:
        for inc_id, inc in _active_incidents.items():
            if inc.get("lat") is None or inc.get("lon") is None:
                continue
            if (ts - inc["ts_updated"]) > MULTIAGENCY_WINDOW_MIN * 60:
                continue
            dist = haversine_km(lat, lon, inc["lat"], inc["lon"])
            if dist <= INCIDENT_LOCATION_RADIUS_KM:
                return inc_id
    return None


def _record_escalation(incident_id: int, stage: str, description: str, ts: float):
    """Store an escalation step and fire the on_escalation callback if stage rises."""
    inc = _active_incidents.get(incident_id)
    if inc is None:
        return

    last_stage = inc.get("escalation_stage")
    last_idx   = (ESCALATION_STAGE_NAMES.index(last_stage)
                  if last_stage in ESCALATION_STAGE_NAMES else -1)
    new_idx    = (ESCALATION_STAGE_NAMES.index(stage)
                  if stage in ESCALATION_STAGE_NAMES else -1)

    if new_idx <= last_idx:
        return  # not an escalation

    inc["escalation_stage"] = stage

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO incident_escalations (incident_id, ts, stage, description) "
        "VALUES (?,?,?,?)",
        (incident_id, ts, stage, description),
    )
    conn.commit()

    # Build escalation chain narrative
    rows = conn.execute(
        "SELECT ts, stage FROM incident_escalations WHERE incident_id=? ORDER BY ts",
        (incident_id,),
    ).fetchall()
    conn.close()

    chain    = " -> ".join(r[1].upper() for r in rows)
    itype    = inc["itype"]
    location = inc.get("location", "unknown location")
    msg = (
        f"ESCALATION: {itype} @ {location}\n"
        f"Chain: {chain}\n"
        f"Latest: {description}"
    )
    print(f"[incident_engine] {msg}", flush=True)

    _fire(_cb_on_escalation, incident_id, stage, msg)


# ---------------------------------------------------------------------------
# Incident create / update
# ---------------------------------------------------------------------------

def _create_incident(itype: str, desc: str, call: dict, ts: float):
    cat      = call.get("category", "Unknown")
    tgid     = call.get("tgid")
    agencies = json.dumps([cat])
    tgids    = json.dumps([tgid])

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.execute(
        "INSERT INTO incidents "
        "(ts_start, ts_updated, itype, description, agencies, tgids, "
        " location, lat, lon, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,'active')",
        (ts, ts, itype, desc, agencies, tgids,
         call.get("location"), call.get("lat"), call.get("lon")),
    )
    inc_id = cur.lastrowid
    conn.commit()
    conn.close()

    _active_incidents[inc_id] = {
        "itype":            itype,
        "ts_updated":       ts,
        "agencies":         {cat},
        "tgids":            {tgid},
        "lat":              call.get("lat"),
        "lon":              call.get("lon"),
        "location":         call.get("location"),
        "escalation_stage": None,
        "room_tokens":      [],
    }

    print(f"[incident_engine] NEW  {itype}: {desc}", flush=True)

    _fire(
        _cb_on_create,
        inc_id,
        dict(
            itype=itype,
            description=desc,
            agencies=agencies,
            location=call.get("location"),
            ts_start=ts,
            lat=call.get("lat"),
            lon=call.get("lon"),
            call=call,
        ),
    )
    return inc_id


def _update_incident(inc_id: int, call: dict, ts: float, desc: str,
                     new_itype: str | None = None):
    inc = _active_incidents[inc_id]
    inc["ts_updated"] = ts
    inc["agencies"].add(call.get("category"))
    inc["tgids"].add(call.get("tgid"))
    agencies = json.dumps(sorted(x for x in inc["agencies"] if x))
    tgids    = json.dumps(sorted(x for x in inc["tgids"]    if x is not None))

    # Upgrade itype if the incoming event is more severe than the current type
    stored_itype = inc["itype"]
    if new_itype and ITYPE_SEVERITY.get(new_itype, 0) > ITYPE_SEVERITY.get(stored_itype, 0):
        print(f"[incident_engine] UPGRADE id={inc_id}: {stored_itype} -> {new_itype}",
              flush=True)
        inc["itype"] = new_itype
        stored_itype = new_itype

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE incidents "
        "SET ts_updated=?, agencies=?, tgids=?, description=?, itype=? "
        "WHERE id=?",
        (ts, agencies, tgids, desc, stored_itype, inc_id),
    )
    conn.commit()
    conn.close()

    print(f"[incident_engine] UPD  {stored_itype} (id={inc_id}): {desc}", flush=True)

    _fire(
        _cb_on_update,
        inc_id,
        dict(
            itype=stored_itype,
            description=desc,
            agencies=agencies,
            ts_updated=ts,
            call=call,
        ),
    )


# ---------------------------------------------------------------------------
# OP25 hold / skip control
# ---------------------------------------------------------------------------

def _send_hold(tgid: int):
    """Issue a hold command to the Pi 1 OP25 endpoint."""
    import json as _json
    import urllib.request as _urllib
    global _current_hold_tgid, _last_hold_activity

    # Import PI1_OP25_URL at call time to avoid a hard dependency on
    # audio_receiver.py at import time.  Fall back gracefully if unavailable.
    try:
        from audio_receiver import PI1_OP25_URL
    except ImportError:
        PI1_OP25_URL = ""

    payload = _json.dumps([{"command": "hold", "arg1": tgid, "arg2": 0}]).encode()
    try:
        req = _urllib.Request(
            PI1_OP25_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        _urllib.urlopen(req, timeout=5)
        _current_hold_tgid  = tgid
        _last_hold_activity = time.time()
        print(f"[hold] HOLD  TGID {tgid}", flush=True)
    except Exception as e:
        print(f"[hold] FAILED to hold TGID {tgid}: {e}", flush=True)


def _send_skip():
    """Issue a skip (release hold) command to the Pi 1 OP25 endpoint."""
    import json as _json
    import urllib.request as _urllib
    global _current_hold_tgid

    try:
        from audio_receiver import PI1_OP25_URL
    except ImportError:
        PI1_OP25_URL = ""

    payload = _json.dumps([{"command": "skip", "arg1": 0, "arg2": 0}]).encode()
    try:
        req = _urllib.Request(
            PI1_OP25_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        _urllib.urlopen(req, timeout=5)
        prev = _current_hold_tgid
        _current_hold_tgid = None
        print(f"[hold] SKIP  (released TGID {prev})", flush=True)
    except Exception as e:
        print(f"[hold] FAILED to release: {e}", flush=True)


def _consider_hold(tgid: int, itype: str, escalation_stage: str | None = None):
    """
    Decide whether to hold or switch hold to tgid, using tier-based escalation.

    Tier rules:
    - No current hold  -> hold tgid immediately.
    - Same tgid already held -> refresh activity timestamp only.
    - Different tgid   -> switch only if new tier >= escalation minimum
                          AND strictly higher than current hold's tier.
    - tgid == 0 (Broadcastify) -> skip, no TGID data.
    """
    global _current_hold_tgid, _last_hold_activity
    if tgid == 0:
        return
    with _hold_lock:
        if _current_hold_tgid is None:
            _send_hold(tgid)
            return
        if _current_hold_tgid == tgid:
            _last_hold_activity = time.time()
            return
        if escalation_stage:
            min_tier = ESCALATION_MIN_TIER.get(escalation_stage, 1)
            new_tier = TGID_TIER.get(tgid, 0)
            cur_tier = TGID_TIER.get(_current_hold_tgid, 0)
            if new_tier >= min_tier and new_tier > cur_tier:
                print(
                    f"[hold] ESCALATION {escalation_stage}: "
                    f"tier {cur_tier} TGID {_current_hold_tgid} "
                    f"-> tier {new_tier} TGID {tgid}",
                    flush=True,
                )
                _send_hold(tgid)
                return
        # No upgrade warranted — keep current hold but refresh activity
        _last_hold_activity = time.time()


def hold_watchdog_thread():
    """Release hold automatically when the held channel goes quiet."""
    while True:
        time.sleep(30)
        with _hold_lock:
            if (_current_hold_tgid is not None
                    and time.time() - _last_hold_activity > HOLD_RELEASE_MINUTES * 60):
                print(
                    f"[hold] watchdog: releasing TGID {_current_hold_tgid} (timeout)",
                    flush=True,
                )
                if HOLD_ENABLED:
                    _send_skip()


# ---------------------------------------------------------------------------
# Incident auto-clear watchdog
# ---------------------------------------------------------------------------

def incident_cleanup_thread():
    """Mark incidents as cleared when they have had no updates for their timeout."""
    while True:
        time.sleep(60)
        now = time.time()
        with _incident_lock:
            to_clear = [
                iid for iid, inc in _active_incidents.items()
                if now - inc["ts_updated"]
                   > INCIDENT_TIMEOUT_MINUTES.get(inc["itype"], INCIDENT_TIMEOUT_DEFAULT) * 60
            ]
            for iid in to_clear:
                itype = _active_incidents[iid]["itype"]
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "UPDATE incidents SET status='cleared', ts_cleared=? WHERE id=?",
                    (time.time(), iid),
                )
                conn.commit()
                conn.close()
                del _active_incidents[iid]
                print(f"[incident_engine] CLEAR {itype} (id={iid}) — no activity",
                      flush=True)
                _fire(_cb_on_clear, iid, itype)


# ---------------------------------------------------------------------------
# Main entry point — called after each call is stored
# ---------------------------------------------------------------------------

def analyze_for_incident(call: dict):
    """
    Run after each call is stored.  Detect and record incidents.

    This is the authoritative port of the same function from audio_receiver.py.
    All detection rules, escalation logic, and state management live here;
    notifications are delegated to registered callbacks.
    """
    tgid = call.get("tgid", 0)
    cat  = call.get("category", "Unknown")
    _raw = call.get("transcript") or ""
    if tgid in LOCUTION_TGIDS:
        _raw = apply_locution_corrections(_raw)
    text = _raw.lower()
    ts   = call.get("ts", time.time())

    flags = []   # list of (priority_score, itype, description)

    # --- Groq LLM result (primary signal — overrides keyword rules if present) ---
    groq       = call.get("groq") or {}
    groq_itype = groq.get("incident_type")
    groq_pri   = groq.get("priority", "NONE")
    if groq_itype and groq_itype not in (None, "ROUTINE"):
        pri_score = {"HIGH": 5, "MED": 15, "NONE": 30}.get(groq_pri, 20)
        flags.append((
            pri_score,
            groq_itype,
            groq.get("description") or f"{groq_itype} detected by LLM",
        ))

    # --- Rule 1: Transit channels active (APD Metro 1-10) ---
    if tgid in TRANSIT_TGIDS:
        flags.append((
            10, "TRANSIT INCIDENT",
            f"APD transit channel active: {call.get('tag', tgid)} — "
            f"Cap Metro bus/rail event likely",
        ))

    # --- Rule 2: Airport alert ---
    if tgid in ABIA_ALERT_TGIDS:
        flags.append((5, "AIRPORT EMERGENCY", "ABIA Alert channel activated"))

    # --- Rule 3: Keyword in transcript ---
    # Skip ABIA operational channels — airport security/ops uses alarming words
    # ("barricade", "hostage", "weapons") in routine daily context.
    # Skip Unknown agency — APD radio is P25 encrypted; Whisper hallucinates
    # words like "shooting" from carrier noise.
    if tgid not in ABIA_OPS_TGIDS and cat != "Unknown":
        for kw, itype in INCIDENT_KEYWORDS:
            if kw in text:
                flags.append((
                    20, itype,
                    f"'{kw}' detected on {call.get('tag', tgid)}",
                ))
                break

    # --- Rule 4: Locution dispatch ---
    # Skip pure EMS calls — cardiac arrests, medical assists, seizures etc.
    # are routine EMS responses, not newsworthy fire incidents.
    EMS_ONLY_KEYWORDS = (
        "cardiac arrest", "medical assist", "seizure", "sick person",
        "respiratory", "difficulty breathing", "chest pain", "diabetic",
        "unconscious", "fall victim", "overdose", "stroke",
    )
    FIRE_KEYWORDS = (
        "fire", "smoke", "explosion", "hazmat", "brush", "structure",
        "vehicle fire", "trash fire", "alarm", "rescue",
    )
    if tgid in LOCUTION_TGIDS and len(text) > 8:
        tl = text.lower()
        is_ems_only = (
            any(kw in tl for kw in EMS_ONLY_KEYWORDS)
            and not any(kw in tl for kw in FIRE_KEYWORDS)
        )
        if not is_ems_only:
            flags.append((
                15, "FIRE DISPATCH",
                f"Locution active ({call.get('tag', tgid)}): {text[:80]}",
            ))

    # --- Rule 5: Multi-agency convergence ---
    # Only fires when another rule has already detected something real.
    # ABIA excluded — it runs 24/7 and co-responds rarely with ground units.
    # Requires 3+ ground agencies to filter out normal paired APD+AFD dispatches.
    if flags:
        window = get_calls_since(ts - MULTIAGENCY_WINDOW_MIN * 60)
        active_cats = {
            c["category"] for c in window
            if c["category"] not in (None, "Unknown", "TXDOT", "Interop", "ABIA")
        }
        ps_cats = active_cats & {"APD", "AFD", "TCEMS", "TCSO", "TCFD"}
        if len(ps_cats) >= 3:
            flags.append((
                30, "MULTI-AGENCY RESPONSE",
                f"Agencies active in last {MULTIAGENCY_WINDOW_MIN}m: "
                f"{', '.join(sorted(ps_cats))}",
            ))

    # --- Rule 6a: Air asset active ---
    # A police/fire helicopter in the air is one of the strongest early
    # indicators of a newsworthy event.
    air_context = detect_air_asset(tgid, call.get("transcript") or "", cat)
    if air_context:
        flags.append((
            8, "AIR ASSET ACTIVE",
            f"{cat} air asset aloft — likely: {air_context} "
            f"({call.get('tag', tgid)})",
        ))

    # --- Rule 6b: DPS Capitol activation ---
    # DPS protects the Capitol complex with unique assets (mounted, bicycle,
    # ATV, helicopter, sniper overwatch).  Any DPS channel activity or
    # cross-agency DPS mention near downtown signals a potential dignitary,
    # protest, or Capitol security event.
    if cat == "DPS" or mentions_dps(text):
        assets     = detect_dps_assets(call.get("transcript") or "")
        asset_note = f" — assets: {', '.join(assets)}" if assets else ""
        capitol    = is_capitol_area(call.get("transcript") or "", call.get("location"))
        if capitol or assets:
            flags.append((
                25, "DPS CAPITOL ACTIVATION",
                f"DPS activity detected{asset_note}"
                + (" near Capitol complex" if capitol else ""),
            ))

    # --- Rule 7: APD surge (APD-only major events like bus stabbings) ---
    apd_calls = [
        c for c in get_calls_since(ts - APD_SURGE_WINDOW_MIN * 60)
        if c["category"] == "APD"
    ]
    if len(apd_calls) >= APD_SURGE_THRESHOLD:
        # Exclude pure dispatch / metro-only surges from noise.
        # 967 = APD Dispatch (high volume normal traffic)
        ops_calls = [c for c in apd_calls if c["tgid"] not in (967,)]
        if len(ops_calls) >= APD_SURGE_THRESHOLD:
            flags.append((
                35, "APD SURGE",
                f"{len(ops_calls)} APD operational calls in "
                f"{APD_SURGE_WINDOW_MIN} min — possible major incident",
            ))

    # --- Escalation check on ALL calls (even routine ones) ---
    # A welfare check that evolves into a SWAT standoff must be linked to
    # the incident that was opened for the earlier call.
    call_id = call.get("id")
    stage   = (_detect_escalation_stage(call.get("transcript") or "")
               or groq.get("escalation_stage"))
    loc_match = _find_incident_by_location(call.get("lat"), call.get("lon"), ts)

    if loc_match is not None:
        # Link this call to the nearby incident
        if call_id:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT OR IGNORE INTO incident_calls (incident_id, call_id) "
                "VALUES (?,?)",
                (loc_match, call_id),
            )
            conn.commit()
            conn.close()
        if stage:
            _record_escalation(
                loc_match, stage,
                f"{call.get('tag', '?')}: {(call.get('transcript') or '')[:80]}",
                ts,
            )

    if not flags:
        if HOLD_ENABLED and loc_match:
            _consider_hold(
                tgid,
                _active_incidents[loc_match]["itype"],
                escalation_stage=(
                    stage or _active_incidents[loc_match].get("escalation_stage")
                ),
            )
        return

    # Use the lowest priority-score (= highest urgency) flag
    flags.sort(key=lambda x: x[0])
    _, itype, desc = flags[0]

    with _incident_lock:
        # Match by location first, then fall back to same itype within window
        matched_id = loc_match
        if matched_id is None:
            for inc_id, inc in _active_incidents.items():
                if (ts - inc["ts_updated"]) >= MULTIAGENCY_WINDOW_MIN * 60:
                    continue
                cur = inc["itype"]
                if cur == itype or itype in ITYPE_MERGE_COMPAT.get(cur, set()):
                    matched_id = inc_id
                    break

        if matched_id is not None:
            _update_incident(matched_id, call, ts, desc, new_itype=itype)
        else:
            _create_incident(itype, desc, call, ts)
            matched_id = max(_active_incidents.keys())  # just created

    if stage:
        _record_escalation(
            matched_id, stage,
            f"{call.get('tag', '?')}: {(call.get('transcript') or '')[:80]}",
            ts,
        )
    if call_id:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO incident_calls (incident_id, call_id) "
            "VALUES (?,?)",
            (matched_id, call_id),
        )
        conn.commit()
        conn.close()

    if HOLD_ENABLED:
        _consider_hold(
            tgid, itype,
            escalation_stage=(stage or groq.get("escalation_stage")),
        )
