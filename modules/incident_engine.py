import base64
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from modules.config import (
    _INCIDENT_TIMEOUT_DEFAULT,
    APD_SURGE_THRESHOLD,
    APD_SURGE_WINDOW_MIN,
    DB_PATH,
    FTS_COT_PORT,
    FTS_ENABLED,
    FTS_HOST,
    HOLD_ENABLED,
    HOLD_RELEASE_MINUTES,
    INCIDENT_TIMEOUT_MINUTES,
    MULTIAGENCY_WINDOW_MIN,
    PI1_OP25_URL,
    TALK_BASE,
    TALK_PASS,
    TALK_ROOMS,
    TALK_USER,
)
from modules.database import calls_since
from modules.alerts import _check_commute_alerts
from modules.talkgroups import (
    ABIA_OPS_TGIDS,
    AIR_ASSET_TGIDS,
    LOCUTION_TGIDS,
    TRANSIT_TGIDS,
    _apply_locution_corrections,
    detect_air_asset,
    detect_dps_assets,
    is_capitol_area,
    mentions_dps,
)

# ---------------------------------------------------------------------------

# Ordered by priority — first match wins for a given call
INCIDENT_KEYWORDS = [
    ("officer down",   "OFFICER DOWN"),
    ("10-99",          "OFFICER DOWN"),
    ("shots fired",    "SHOOTING"),
    ("shooting",       "SHOOTING"),
    ("person shot",    "SHOOTING"),
    ("subject shot",   "SHOOTING"),
    ("victim shot",    "SHOOTING"),
    ("shot victim",    "SHOOTING"),
    ("homicide",       "SHOOTING"),
    ("found dead",     "SHOOTING"),
    ("body found",     "SHOOTING"),
    ("gsw",            "SHOOTING"),   # EMS: gunshot wound
    ("gunshot",        "SHOOTING"),   # EMS: gunshot wound
    ("gun shot",       "SHOOTING"),
    ("stabbing",       "STABBING"),
    (" stab",          "STABBING"),
    ("assault",         "STABBING"),   # locution CAD code for stabbing/cutting victim
    ("aircraft",       "AIRCRAFT EMERGENCY"),
    ("mass casualty",  "MASS CASUALTY"),
    ("mci",            "MASS CASUALTY"),
    ("cardiac arrest", "EMS DISPATCH"),
    ("multiple patients", "EMS DISPATCH"),
    ("trauma",         "EMS DISPATCH"),
    ("structure fire", "STRUCTURE FIRE"),
    ("working fire",   "STRUCTURE FIRE"),
    ("fully involved", "STRUCTURE FIRE"),
    ("hazmat",         "HAZMAT"),
    ("chemical spill", "HAZMAT"),
    ("hostage",        "HOSTAGE/BARRICADE"),
    ("barricade",      "HOSTAGE/BARRICADE"),
    # Fatal crash — longer phrases before generic "crash" so they match first
    ("fatal crash",    "FATAL CRASH"),
    ("fatal accident", "FATAL CRASH"),
    ("fatality",       "FATAL CRASH"),
    ("start a dts",    "FATAL CRASH"),   # Austin APD Deceased Traffic Scene protocol
    ("crash",          "CRASH/COLLISION"),
    ("collision",      "CRASH/COLLISION"),
    ("rollover",       "CRASH/COLLISION"),
    # Medical Examiner / death scene indicators
    ("medical examiner", "DEATH INVESTIGATION"),
    ("jp responding",  "DEATH INVESTIGATION"),
    ("justice of the peace", "DEATH INVESTIGATION"),
    ("pronounce",      "DEATH INVESTIGATION"),
    ("pronounced at",  "DEATH INVESTIGATION"),
    ("death investigation", "DEATH INVESTIGATION"),
    ("signal 48",      "DEATH INVESTIGATION"),   # Texas LE code for death
]

# ---------------------------------------------------------------------------
# Escalation chain detection
# ---------------------------------------------------------------------------

# Ordered escalation stages — higher index = more serious
ESCALATION_STAGES = [
    ("welfare",      ["welfare check", "well-being check", "wbc", "check on subject"]),
    ("disturbance",  ["disturbance", "domestic", "fight", "altercation", "argument"]),
    ("pursuit",      ["pursuit", "foot chase", "fleeing", "chase"]),
    ("weapons",      ["weapon", "armed", "firearm", "gun", "knife", "rifle"]),
    ("backup",       ["need backup", "requesting backup", "all units", "code 3", "lights and sirens"]),
    ("tactical",     ["swat", "tac team", "tactical", "negotiat", "standoff", "barricaded"]),
    ("k9",           ["k-9", "k9", "canine", "dog track", "dog unit"]),
    ("air",          ["air1", "air 1", "helicopter", "aviation", "bird in the air"]),
]

ESCALATION_STAGE_NAMES = [s[0] for s in ESCALATION_STAGES]

# Severity ordering for itype upgrade decisions.
# Higher value = more urgent. _update_incident upgrades stored itype when new > current.
ITYPE_SEVERITY: dict[str, int] = {
    "CRASH/COLLISION":         1,
    "PEDESTRIAN INCIDENT":     2,
    "FIRE DISPATCH":           2,
    "TRANSIT INCIDENT":        2,
    "DEATH INVESTIGATION":     3,
    "FATAL CRASH":             4,
    "SHOOTING":                5,
    "STABBING":                5,
    "WEAPONS":                 5,
    "STRUCTURE FIRE":          5,
    "HAZMAT":                  5,
    "OFFICER DOWN":            6,
    "MASS CASUALTY":           6,
    "HOSTAGE/BARRICADE":       6,
    "AIRCRAFT EMERGENCY":      6,
}

# Compatible itype groups — incidents of these types merge even if itype differs.
# Key: new itype being detected. Value: set of existing itypes it can merge into.
ITYPE_MERGE_COMPAT: dict[str, set] = {
    "FATAL CRASH":         {"CRASH/COLLISION", "PEDESTRIAN INCIDENT", "DEATH INVESTIGATION"},
    "CRASH/COLLISION":     {"FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "PEDESTRIAN INCIDENT": {"CRASH/COLLISION", "FATAL CRASH"},
    "DEATH INVESTIGATION": {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
}

# ---------------------------------------------------------------------------
# TGID tier map — higher tier = more tactically specific channel.
# When escalation stage rises, _consider_hold switches to higher-tier TGIDs.
# Tier 0 = non-public-safety (water, parking, energy) — never hold for crime.
# Tier 1 = dispatch (initial report + coordinator traffic).
# Tier 2 = metro/field (unit coordination, pursuits).
# Tier 3 = tactical (SWAT, K9, air operations).
# ---------------------------------------------------------------------------
TGID_TIER: dict[int, int] = {
    **{tgid: 1 for tgid in range(960, 970)},   # APD Dispatch 1-10
    **{tgid: 2 for tgid in range(972, 988)},   # APD Metro 1-16
    **{tgid: 3 for tgid in [1000, 1001, 1002]}, # APD TAC 1-3
    1121: 1, 1122: 1,                            # AFD Dispatch 1-2
    1155: 2,                                     # AFD TAC
    1162: 1,                                     # TCFD Locution
    1371: 1, 1377: 1, 1378: 1,                  # AFD zonal (East/North/South)
    1471: 1, 1472: 2, 1473: 2,                  # ABIA Ops / Security / Fire
    1474: 2, 1480: 3, 1481: 3,                  # ABIA Police / Emerg / Alert
    989: 2,                                      # APD Air/K9
    **{tgid: 2 for tgid in range(1020, 1027)},  # APD Narc 1-7
    1274: 2,                                     # TCEMS SWAT
    2409: 3, 2410: 3,                            # TCSO SWAT 1-2
    5291: 2, 5292: 2,                            # Austin/Travis Interop 1-2
}

# Minimum tier required for each escalation stage.
# When a new clip arrives on a tgid whose tier >= this AND > current hold tier,
# _consider_hold switches the hold to follow the incident.
ESCALATION_MIN_TIER: dict[str, int] = {
    "welfare":     1,  # any dispatch is fine
    "disturbance": 1,
    "weapons":     1,
    "pursuit":     2,  # need metro — units are moving
    "backup":      2,
    "k9":          2,
    "tactical":    3,  # need TAC — SWAT/negotiators on scene
    "air":         3,
}

# Location match radius — calls within this many km link to an existing incident
INCIDENT_LOCATION_RADIUS_KM = 0.5


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km between two lat/lon points."""
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def _detect_escalation_stage(text: str) -> str | None:
    """Return the highest escalation stage keyword found in transcript."""
    text = text.lower()
    matched = None
    for stage, keywords in ESCALATION_STAGES:
        if any(kw in text for kw in keywords):
            matched = stage
    return matched  # returns highest-indexed match since we iterate in order


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
            dist = _haversine_km(lat, lon, inc["lat"], inc["lon"])
            if dist <= INCIDENT_LOCATION_RADIUS_KM:
                return inc_id
    return None


def _record_escalation(incident_id: int, stage: str, description: str, ts: float):
    """Store an escalation step and alert if the stage is higher than last recorded."""
    from modules.pollers_legacy import send_dm_alert
    inc = _active_incidents.get(incident_id)
    if inc is None:
        return
    last_stage = inc.get("escalation_stage")
    last_idx   = ESCALATION_STAGE_NAMES.index(last_stage) if last_stage in ESCALATION_STAGE_NAMES else -1
    new_idx    = ESCALATION_STAGE_NAMES.index(stage)      if stage in ESCALATION_STAGE_NAMES else -1

    if new_idx <= last_idx:
        return  # not an escalation

    inc["escalation_stage"] = stage
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO incident_escalations (incident_id, ts, stage, description) VALUES (?,?,?,?)",
        (incident_id, ts, stage, description)
    )
    conn.commit()
    conn.close()

    # Build escalation chain narrative from DB
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ts, stage FROM incident_escalations WHERE incident_id=? ORDER BY ts",
        (incident_id,)
    ).fetchall()
    conn.close()
    chain = " → ".join(r[1].upper() for r in rows)

    itype    = inc["itype"]
    location = inc.get("location", "unknown location")
    msg = (f"🔺 ESCALATION: {itype} @ {location}\n"
           f"Chain: {chain}\n"
           f"Latest: {description}")
    print(f"[escalation] {msg}", flush=True)

    agencies_str = ", ".join(sorted(x for x in inc["agencies"] if x))
    cat = next(iter(inc["agencies"]), "APD")
    threading.Thread(target=send_dm_alert,
                     args=(itype, msg, location, agencies_str, cat), daemon=True).start()
    # Post escalation to incidents room
    threading.Thread(target=_post_escalation_to_talk,
                     args=(itype, location, chain, description, inc.get("room_tokens", [])),
                     daemon=True).start()


def _post_escalation_to_talk(itype: str, location: str, chain: str, latest: str, extra_rooms: list):
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    msg = (f"🔺 **ESCALATION — {itype}** @ {location}\n"
           f"**Chain:** {chain}\n"
           f"**Latest:** {latest}")
    for room in set([TALK_ROOMS["incidents"]] + extra_rooms):
        payload = urllib.parse.urlencode({"message": msg}).encode()
        req = urllib.request.Request(
            f"{TALK_BASE}/chat/{room}", data=payload,
            headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[escalation] post failed → {room}: {e}", flush=True)


# In-memory active incident state: {db_id: {itype, ts_updated, agencies, tgids, lat, lon, escalation_stage}}
_active_incidents: dict[int, dict] = {}
_incident_lock = threading.Lock()


def analyze_for_incident(call: dict):
    """Run after each call is stored. Detect and record incidents."""
    tgid  = call.get("tgid", 0)
    cat   = call.get("category", "Unknown")
    _raw = (call.get("transcript") or "")
    if tgid in LOCUTION_TGIDS:
        _raw = _apply_locution_corrections(_raw)
    text  = _raw.lower()
    ts    = call.get("ts", time.time())

    flags = []   # list of (priority, itype, description)

    # --- Groq LLM result (primary signal — overrides keyword rules if present) ---
    groq = call.get("groq") or {}
    groq_itype = groq.get("incident_type")
    groq_pri   = groq.get("priority", "NONE")
    if groq_itype and groq_itype not in (None, "ROUTINE"):
        pri_score = {"HIGH": 5, "MED": 15, "NONE": 30}.get(groq_pri, 20)
        flags.append((pri_score, groq_itype,
                      groq.get("description") or f"{groq_itype} detected by LLM"))

    # --- Rule 1: Transit channels active (APD Metro 1-10) ---
    if tgid in TRANSIT_TGIDS:
        flags.append((10, "TRANSIT INCIDENT",
                      f"APD transit channel active: {call.get('tag', tgid)} — "
                      f"Cap Metro bus/rail event likely"))

    # --- Rule 2: Airport alert ---
    if tgid in AIR_ASSET_TGIDS:
        flags.append((5, "AIRPORT EMERGENCY",
                      "ABIA Alert channel activated"))

    # --- Rule 3: Keyword in transcript ---
    # Skip ABIA operational channels — airport security/ops uses words like
    # "barricade", "hostage", "weapons" in routine daily context.
    # Skip Unknown agency — APD radio is P25 encrypted; Whisper hallucinates
    # words like "shooting", "assault", "shots fired" from carrier noise.
    if tgid not in ABIA_OPS_TGIDS and cat != "Unknown":
        for kw, itype in INCIDENT_KEYWORDS:
            if kw in text:
                flags.append((20, itype,
                              f"'{kw}' detected on {call.get('tag', tgid)}"))
                break

    # --- Rule 4: Locution dispatch ---
    # Skip pure EMS calls — cardiac arrests, medical assists, seizures, etc.
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
            flags.append((15, "FIRE DISPATCH",
                          f"Locution active ({call.get('tag', tgid)}): {text[:80]}"))

    # --- Rule 5: Multi-agency convergence ---
    # Only fires when another rule has already detected something real.
    # ABIA is excluded because it runs 24/7 and is rarely co-responding with
    # ground units on the same incident — including it caused constant false positives.
    # Requires 3+ ground agencies to filter out normal paired APD+AFD dispatches.
    if flags:
        window = calls_since(ts - MULTIAGENCY_WINDOW_MIN * 60)
        active_cats = {c["category"] for c in window
                       if c["category"] not in (None, "Unknown", "TXDOT", "Interop", "ABIA")}
        ps_cats = active_cats & {"APD", "AFD", "TCEMS", "TCSO", "TCFD"}
        if len(ps_cats) >= 3:
            flags.append((30, "MULTI-AGENCY RESPONSE",
                          f"Agencies active in last {MULTIAGENCY_WINDOW_MIN}m: "
                          f"{', '.join(sorted(ps_cats))}"))

    # --- Rule 6a: Air asset active ---
    # A police/fire helicopter in the air is one of the strongest early
    # indicators of a newsworthy event — pursuit, active shooter perimeter,
    # search, crowd overwatch, or dignitary movement. Flag it immediately.
    air_context = detect_air_asset(tgid, call.get("transcript") or "", cat)
    if air_context:
        flags.append((8, "AIR ASSET ACTIVE",
                      f"{cat} air asset aloft — likely: {air_context} "
                      f"({call.get('tag', tgid)})"))

    # --- Rule 6b: DPS Capitol activation ---
    # DPS protects the Capitol complex with assets most agencies don't have.
    # Any DPS talkgroup activity OR cross-agency DPS mention near downtown
    # signals a potential dignitary, protest, or Capitol security event.
    if cat == "DPS" or mentions_dps(text):
        assets = detect_dps_assets(call.get("transcript") or "")
        asset_note = f" — assets: {', '.join(assets)}" if assets else ""
        capitol = is_capitol_area(call.get("transcript") or "", call.get("location"))
        if capitol or assets:
            flags.append((25, "DPS CAPITOL ACTIVATION",
                          f"DPS activity detected{asset_note}"
                          + (" near Capitol complex" if capitol else "")))

    # --- Rule 7: APD surge (APD-only events like the bus stabbing) ---
    apd_calls = [c for c in calls_since(ts - APD_SURGE_WINDOW_MIN * 60)
                 if c["category"] == "APD"]
    if len(apd_calls) >= APD_SURGE_THRESHOLD:
        # Exclude pure dispatch/metro-only surges from noise
        ops_calls = [c for c in apd_calls
                     if c["tgid"] not in (967,)]  # 967 = APD Dispatch (high volume normal)
        if len(ops_calls) >= APD_SURGE_THRESHOLD:
            flags.append((35, "APD SURGE",
                          f"{len(ops_calls)} APD operational calls in "
                          f"{APD_SURGE_WINDOW_MIN} min — possible major incident"))

    # --- Escalation check on ALL calls (even routine ones) ---
    # A welfare check that turns into a SWAT standoff must be linked.
    call_id = call.get("id")
    stage = _detect_escalation_stage(call.get("transcript") or "") or groq.get("escalation_stage")
    loc_match = _find_incident_by_location(call.get("lat"), call.get("lon"), ts)
    if loc_match is not None:
        # Link this call to the nearby incident
        if call_id:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT OR IGNORE INTO incident_calls (incident_id, call_id) VALUES (?,?)",
                         (loc_match, call_id))
            conn.commit()
            conn.close()
        if stage:
            _record_escalation(loc_match, stage,
                               f"{call.get('tag','?')}: {(call.get('transcript') or '')[:80]}", ts)

    if not flags:
        if HOLD_ENABLED and loc_match:
            _consider_hold(tgid, _active_incidents[loc_match]["itype"],
                           escalation_stage=stage or _active_incidents[loc_match].get("escalation_stage"))
        return

    # Use lowest priority number (highest urgency)
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
        _record_escalation(matched_id, stage,
                           f"{call.get('tag','?')}: {(call.get('transcript') or '')[:80]}", ts)
    if call_id:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO incident_calls (incident_id, call_id) VALUES (?,?)",
                     (matched_id, call_id))
        conn.commit()
        conn.close()

    if HOLD_ENABLED:
        _consider_hold(tgid, itype, escalation_stage=stage or groq.get("escalation_stage"))


# ---------------------------------------------------------------------------
# ATAK / FTS / CoT subsystem moved to modules/atak.py — re-exported here so
# existing callers (modules.pollers_legacy, etc.) keep working unchanged.
# ---------------------------------------------------------------------------
from modules.atak import _atak_clear_marker, _atak_post_marker  # noqa: E402, F401


def _create_incident(itype: str, desc: str, call: dict, ts: float):
    from modules.pollers_legacy import create_deck_card, post_banner, send_dm_alert
    cat    = call.get("category", "Unknown")
    tgid   = call.get("tgid")
    agencies = json.dumps([cat])
    tgids    = json.dumps([tgid])
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.execute(
        "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, tgids, "
        "location, lat, lon, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
        (ts, ts, itype, desc, agencies, tgids,
         call.get("location"), call.get("lat"), call.get("lon"))
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
    print(f"[incident] NEW  {itype}: {desc}", flush=True)
    agencies_str = ", ".join(json.loads(agencies))
    location     = call.get("location")
    inc_data = dict(itype=itype, description=desc, agencies=agencies,
                    location=location, ts_start=ts)
    threading.Thread(target=create_deck_card, args=(inc_data,), daemon=True).start()
    threading.Thread(target=send_dm_alert,    args=(itype, desc, location, agencies_str, cat), daemon=True).start()
    threading.Thread(target=post_banner,      args=(itype, location, agencies_str), daemon=True).start()
    if call.get("location") and call.get("lat") is not None and call.get("lon") is not None:
        threading.Thread(target=_atak_post_marker,
                         args=(inc_id, call["lat"], call["lon"], itype, call.get("location"), desc),
                         daemon=True).start()
    if call.get("lat") is not None and call.get("lon") is not None:
        threading.Thread(target=_check_commute_alerts,
                         args=(inc_id, itype, call["lat"], call["lon"], desc),
                         daemon=True).start()


def _update_incident(inc_id: int, call: dict, ts: float, desc: str, new_itype: str | None = None):
    inc = _active_incidents[inc_id]
    inc["ts_updated"] = ts
    inc["agencies"].add(call.get("category"))
    inc["tgids"].add(call.get("tgid"))
    agencies = json.dumps(sorted(x for x in inc["agencies"] if x))
    tgids    = json.dumps(sorted(x for x in inc["tgids"]    if x is not None))
    # Upgrade itype if incoming event is more severe than current
    stored_itype = inc["itype"]
    if new_itype and ITYPE_SEVERITY.get(new_itype, 0) > ITYPE_SEVERITY.get(stored_itype, 0):
        print(f"[incident] UPGRADE id={inc_id}: {stored_itype} → {new_itype}", flush=True)
        inc["itype"] = new_itype
        stored_itype = new_itype
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE incidents SET ts_updated=?, agencies=?, tgids=?, description=?, itype=? WHERE id=?",
        (ts, agencies, tgids, desc, stored_itype, inc_id)
    )
    conn.commit()
    conn.close()
    print(f"[incident] UPD  {stored_itype} (id={inc_id}): {desc}", flush=True)


def incident_cleanup_thread():
    """Mark incidents as cleared when they've had no updates for their type's timeout."""
    from modules.pollers_legacy import clear_banner
    while True:
        time.sleep(60)
        now = time.time()
        with _incident_lock:
            to_clear = [
                iid for iid, inc in _active_incidents.items()
                if now - inc["ts_updated"] >
                   INCIDENT_TIMEOUT_MINUTES.get(inc["itype"], _INCIDENT_TIMEOUT_DEFAULT) * 60
            ]
            for iid in to_clear:
                itype = _active_incidents[iid]["itype"]
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "UPDATE incidents SET status='cleared', ts_cleared=? WHERE id=?",
                    (time.time(), iid)
                )
                conn.commit()
                conn.close()
                del _active_incidents[iid]
                print(f"[incident] CLEAR {itype} (id={iid}) — no activity", flush=True)
                threading.Thread(target=clear_banner,        args=(itype,),    daemon=True).start()
                threading.Thread(target=_atak_clear_marker,  args=(iid,),      daemon=True).start()


# ---------------------------------------------------------------------------
# OP25 hold / skip control
# ---------------------------------------------------------------------------

_current_hold_tgid: int | None = None
_last_hold_activity: float = 0.0
_hold_lock = threading.Lock()


def _consider_hold(tgid: int, itype: str, escalation_stage: str | None = None):
    """Decide whether to hold or switch hold to tgid, using tier-based escalation logic.

    Tier rules:
    - No current hold → hold tgid immediately.
    - Same tgid already held → refresh activity timestamp only.
    - Different tgid → switch only if new tgid's tier is >= the escalation minimum
      AND strictly higher than the current hold's tier (follow the incident up the
      chain: dispatch → metro → tactical).
    - Broadcastify clips (tgid == 0) never drive a hold switch.
    """
    global _current_hold_tgid, _last_hold_activity
    if tgid == 0:
        return  # Broadcastify mixed stream — no TGID data, skip
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
                print(f"[hold] ESCALATION {escalation_stage}: tier {cur_tier} TGID {_current_hold_tgid}"
                      f" → tier {new_tier} TGID {tgid}", flush=True)
                _send_hold(tgid)
                return
        # No upgrade warranted — keep current hold but refresh activity
        _last_hold_activity = time.time()


def _send_hold(tgid: int):
    global _current_hold_tgid, _last_hold_activity
    payload = json.dumps([{"command": "hold", "arg1": tgid, "arg2": 0}]).encode()
    try:
        req = urllib.request.Request(
            PI1_OP25_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        _current_hold_tgid = tgid
        _last_hold_activity = time.time()
        print(f"[hold] HOLD  TGID {tgid}", flush=True)
    except Exception as e:
        print(f"[hold] FAILED to hold TGID {tgid}: {e}", flush=True)


def _send_skip():
    global _current_hold_tgid
    payload = json.dumps([{"command": "skip", "arg1": 0, "arg2": 0}]).encode()
    try:
        req = urllib.request.Request(
            PI1_OP25_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=5)
        prev = _current_hold_tgid
        _current_hold_tgid = None
        print(f"[hold] SKIP  (released TGID {prev})", flush=True)
    except Exception as e:
        print(f"[hold] FAILED to release: {e}", flush=True)




def hold_watchdog_thread():
    while True:
        time.sleep(30)
        with _hold_lock:
            if (_current_hold_tgid is not None and
                    time.time() - _last_hold_activity > HOLD_RELEASE_MINUTES * 60):
                print(f'[hold] watchdog: releasing TGID {_current_hold_tgid} (timeout)', flush=True)
                if HOLD_ENABLED:
                    _send_skip()
