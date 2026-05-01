"""ATAK / FreeTAKServer (FTS) / Cursor-on-Target (CoT) subsystem.

Extracted from modules/incident_engine.py and audio_receiver.py — pure refactor,
no behavior changes. Persistent SSL connection to FTS for broadcasting CoT
markers when Battle Buddy detects incidents, plus periodic resync to keep
late-joining ATAK/WinTAK clients in sync.
"""
import socket as _sock_mod
import sqlite3
import ssl as _ssl_mod
import threading
import time
from datetime import datetime, timedelta, timezone

from modules.config import DB_PATH, FTS_COT_PORT, FTS_ENABLED, FTS_HOST

_atak_markers: dict[int, str] = {}  # incident_id → FTS uid, for deletion on clear

_fts_lock   = threading.Lock()
_fts_socket = None          # the live SSL socket, or None

_BB_SA_UID  = "BATTLEBUDDY-SERVER"
_BB_SA_XML  = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    "<event version='2.0' uid='{uid}' type='t-x-c-t' "
    "time='{t}' start='{t}' stale='{s}' how='m-g'>"
    "<point lat='0.0' lon='0.0' hae='0.0' ce='9999999.0' le='9999999.0'/>"
    "<detail>"
    "<contact callsign='BattleBuddy'/>"
    "<remarks>Austin P25 AI Monitor</remarks>"
    "</detail>"
    "</event>"
)

def _fts_build_ctx():
    ctx = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations("/opt/battlebuddy/certs/ca.pem")
    ctx.load_cert_chain("/opt/battlebuddy/certs/client.pem",
                        "/opt/battlebuddy/certs/client.key")
    ctx.check_hostname = False
    return ctx

def _fts_connect():
    """Open a fresh persistent SSL connection to FTS. Called under _fts_lock."""
    global _fts_socket
    try:
        if _fts_socket:
            try: _fts_socket.close()  # noqa: E701
            except Exception: pass  # noqa: E701
            _fts_socket = None
        raw = _sock_mod.create_connection((FTS_HOST, FTS_COT_PORT), timeout=10)
        _fts_socket = _fts_build_ctx().wrap_socket(raw)
        # Send our SA announcement so FTS registers us as a proper client
        now_dt  = datetime.now(timezone.utc)
        stale_dt = now_dt + __import__('datetime').timedelta(minutes=10)
        fmt = "%Y-%m-%dT%H:%M:%S.0Z"
        sa = _BB_SA_XML.format(uid=_BB_SA_UID,
                               t=now_dt.strftime(fmt),
                               s=stale_dt.strftime(fmt))
        _fts_socket.sendall(sa.encode("utf-8"))
        print("[atak] persistent connection established to FTS", flush=True)
    except Exception as exc:
        _fts_socket = None
        print(f"[atak] connect failed: {exc}", flush=True)

def _atak_send_cot(xml: str):
    """Send a CoT XML string over the persistent FTS connection, reconnecting if needed."""
    global _fts_socket
    if not FTS_ENABLED:
        return
    for attempt in range(2):
        with _fts_lock:
            if _fts_socket is None:
                _fts_connect()
            if _fts_socket is None:
                raise ConnectionError("FTS unreachable")
            try:
                _fts_socket.sendall(xml.encode("utf-8"))
                return
            except Exception as exc:
                print(f"[atak] send error (attempt {attempt+1}): {exc} — reconnecting", flush=True)
                _fts_socket = None
                if attempt == 0:
                    _fts_connect()

def _fts_keepalive_thread():
    """Send a SA heartbeat every 30 s to keep the FTS connection alive."""
    while True:
        time.sleep(30)
        try:
            now_dt   = datetime.now(timezone.utc)
            stale_dt = now_dt + __import__('datetime').timedelta(minutes=2)
            fmt = "%Y-%m-%dT%H:%M:%S.0Z"
            sa = _BB_SA_XML.format(uid=_BB_SA_UID,
                                   t=now_dt.strftime(fmt),
                                   s=stale_dt.strftime(fmt))
            _atak_send_cot(sa)
        except Exception as exc:
            print(f"[atak] keepalive error: {exc}", flush=True)


def _atak_resync_thread():
    """Re-post all active incident markers every 5 minutes.
    Ensures clients that connect after initial post receive current state."""
    while True:
        time.sleep(300)
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, itype, lat, lon, location, description FROM incidents"
                " WHERE status='active' AND location IS NOT NULL AND location != ''"
                " AND lat IS NOT NULL AND lon IS NOT NULL AND is_test=0"
            ).fetchall()
            conn.close()
            count = 0
            for row in rows:
                threading.Thread(
                    target=_atak_post_marker,
                    args=(row['id'], row['lat'], row['lon'], row['itype'],
                          row['location'], row['description']),
                    daemon=True
                ).start()
                count += 1
            if count:
                print(f"[atak] resync: re-posted {count} active marker(s)", flush=True)
        except Exception as exc:
            print(f"[atak] resync error: {exc}", flush=True)




# CoT type codes and colors per incident type
# type format: a-h-G (attitude-hostility-dimension)
# ARGB color as signed int: 0xFFRRGGBB cast to int32
# iconsetpath constants
_FEMA_FIRE_ICON      = "f8f7f666-8b28-4b57-9fbb-e48e61d33b79/Iron Sites/Fire Incident.png"
_GEOOPS_FIRE_ICON    = "83198b4872a8c34eb9c549da8a4de5a28f07821185b39a2277948f66c24ac17a/WildFire/Fire Location.png"
_RESPONDER_EMS_ICON  = "de450cbf-2ffc-47fb-bd2b-ba2db89b035e/Incident/EMS--Plain.png"
_HELO_EMS_ICON       = "de450cbf-2ffc-47fb-bd2b-ba2db89b035e/Incident/EMS--Plain.png"

_COT_PROFILE = {
    # itype                 cot_type         argb_color    stale_min  iconsetpath (None = default shape)
    "SHOOTING":           ("a-h-G",          -65536,       60,  None),   # red
    "STABBING":           ("a-h-G",          -65536,       60,  None),   # red
    "OFFICER DOWN":       ("a-h-G",          -65536,       60,  None),   # red
    "PURSUIT":            ("a-h-G-V",        -23296,       45,  None),   # orange, hostile vehicle
    "WEAPONS":            ("a-h-G",          -23296,       45,  None),   # orange
    "STRUCTURE FIRE":     ("a-n-G",          -14336,       60,  _FEMA_FIRE_ICON),
    "FIRE DISPATCH":      ("a-n-G",          -14336,       45,  _FEMA_FIRE_ICON),
    "FIRE ALARM":         ("a-n-G",          -256,         30,  _FEMA_FIRE_ICON),
    "FIRE/EMS DISPATCH":  ("a-n-G",          -14336,       45,  _FEMA_FIRE_ICON),
    "GRASS FIRE":         ("a-n-G",          -23296,       60,  _GEOOPS_FIRE_ICON),
    "CRASH/COLLISION":    ("b-m-p-s-p-i",    -23296,       30,  None),   # orange, incident point
    "FATAL CRASH":        ("a-h-G",           -65536,       60,  None),   # red, fatal incident
    "MASS CASUALTY":      ("a-h-G",          -65536,       90,  _RESPONDER_EMS_ICON),
    "EMS DISPATCH":        ("a-n-G",          -16711936,    45,  _RESPONDER_EMS_ICON),  # green, EMS
    "HAZMAT":             ("b-m-p-s-m",      -16711681,    60,  None),   # green, hazmat
    "AIR ASSET ACTIVE":   ("a-f-A-M-H-R",    -16776961,    30,  None),   # blue, rotary wing
    "AIR ASSET EMS":      ("a-f-A-C-H-R",    -16711936,    30,  _HELO_EMS_ICON),  # green civilian rotary
    "AIR ASSET ORBIT":    ("a-h-A-M-H-R",    -65536,       45,  None),            # red hostile rotary
    "DPS CAPITOL ACTIVATION": ("a-h-G",      -65536,       60,  None),   # red
    "MULTI-AGENCY RESPONSE":  ("a-h-G",      -65536,       60,  None),   # red
    # Traffic incident types
    "FLOODING":           ("b-m-p-s-p-i",  -16776961,    45,  None),   # blue
    "ROAD HAZARD":        ("b-m-p-s-p-i",  -23296,       30,  None),   # orange
    "PEDESTRIAN INCIDENT": ("a-h-G",        -65536,       45,  None),   # red, person involved
    "VEHICLE FIRE":       ("a-n-G",         -23296,       45,  _FEMA_FIRE_ICON),  # orange, fire icon
    "TRAFFIC SIGNAL ISSUE": ("b-m-p-s-p-i", -8355712,    20,  None),   # grey, low priority
    "STALLED VEHICLE":    ("b-m-p-s-p-i",  -8355712,     20,  None),   # grey, low priority
    "ABANDONED VEHICLE":  ("b-m-p-s-p-i",  -8355712,     20,  None),   # grey, low priority
    "TRAFFIC INCIDENT":   ("b-m-p-s-p-i",  -8355712,     20,  None),   # grey, catch-all
}
_COT_DEFAULT = ("b-m-p-s-p-i", -8355712, 30, None)  # grey incident point


def _atak_post_marker(incident_id: int, lat: float, lon: float, itype: str,
                      location: str | None, description: str | None = None):
    """Post a CoT marker to FTS port 8087 with proper type, color, and stale time."""
    if not FTS_ENABLED:
        return
    cot_type, argb, stale_min, iconsetpath = _COT_PROFILE.get(itype, _COT_DEFAULT)
    uid      = f"BB-INC-{incident_id}"
    callsign = (f"{itype} @ {location}" if location else itype)[:60]
    remarks  = (description or callsign)[:200]
    now_dt   = datetime.now(timezone.utc)
    stale_dt = now_dt + timedelta(minutes=stale_min)
    fmt      = "%Y-%m-%dT%H:%M:%S.0Z"
    now      = now_dt.strftime(fmt)
    stale    = stale_dt.strftime(fmt)
    # ce/le: use 50m if we have a real address, 500m if approximate
    ce_le    = "50.0" if location else "500.0"
    usericon = (f"<usericon iconsetpath='{iconsetpath}'/>" if iconsetpath else '')
    xml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<event version='2.0' uid='{uid}' type='{cot_type}' "
        f"time='{now}' start='{now}' stale='{stale}' how='m-g'>"
        f"<point lat='{lat}' lon='{lon}' hae='0.0' ce='{ce_le}' le='{ce_le}'/>"
        f"<detail>"
        f"<contact callsign='{callsign}'/>"
        f"<color argb='{argb}'/>"
        f"{usericon}"
        f"<remarks>{remarks}</remarks>"
        f"</detail>"
        f"</event>"
    )
    try:
        _atak_send_cot(xml)
        _atak_markers[incident_id] = uid
        print(f"[atak] marker posted: {itype} id={incident_id} uid={uid}", flush=True)
    except Exception as exc:
        print(f"[atak] post_marker error: {exc}", flush=True)


def _atak_clear_marker(incident_id: int):
    """Delete the ATAK marker via CoT t-x-d-d when an incident is cleared."""
    uid = _atak_markers.pop(incident_id, None)
    if not uid or not FTS_ENABLED:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0Z")
    xml = (
        f"<?xml version='1.0' encoding='UTF-8'?>"
        f"<event version='2.0' uid='{uid}' type='t-x-d-d' "
        f"time='{now}' start='{now}' stale='{now}' how='m-g'>"
        f"<point lat='0.0' lon='0.0' hae='0.0' ce='9999999.0' le='9999999.0'/>"
        f"<detail/></event>"
    )
    try:
        _atak_send_cot(xml)
        print(f"[atak] marker cleared: id={incident_id} uid={uid}", flush=True)
    except Exception as exc:
        print(f"[atak] clear_marker error: {exc}", flush=True)


def get_connection_status() -> dict:
    """Public API: report whether the persistent FTS socket is currently up."""
    with _fts_lock:
        connected = _fts_socket is not None
    return {"connected": connected, "fts_enabled": FTS_ENABLED}


def startup():
    """Public API: connect to FTS and launch keepalive + resync threads if enabled."""
    if not FTS_ENABLED:
        return
    _fts_connect()
    threading.Thread(target=_fts_keepalive_thread, daemon=True).start()
    threading.Thread(target=_atak_resync_thread, daemon=True).start()


def _atak_resync_on_startup():
    """Re-post ATAK markers for any incidents still active in the DB at startup."""
    if not FTS_ENABLED:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - 30 * 60
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status='active' AND ts_updated > ?"
        " AND location IS NOT NULL AND location != ''"
        "AND lat IS NOT NULL AND lon IS NOT NULL AND is_test=0",
        (cutoff,)
    ).fetchall()
    conn.close()
    count = 0
    for row in rows:
        inc = dict(row)
        threading.Thread(
            target=_atak_post_marker,
            args=(inc['id'], inc['lat'], inc['lon'], inc['itype'], inc['location'], inc.get('description')),
            daemon=True
        ).start()
        count += 1
    if count:
        print(f"[atak] startup resync: re-posted {count} active incident marker(s)", flush=True)
