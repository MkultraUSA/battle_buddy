import threading
import json
import sqlite3
import time
import urllib.request
import ssl as _ssl_mod
import socket as _sock_mod
from datetime import datetime, timezone

from modules.config import *

_active_incidents = {}
_atak_markers = {}
_incident_lock = threading.Lock()

_fts_lock   = threading.Lock()
_fts_socket = None

def _fts_build_ctx():
    ctx = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations("/opt/battlebuddy/certs/ca.pem")
    ctx.load_cert_chain("/opt/battlebuddy/certs/client.pem",
                        "/opt/battlebuddy/certs/client.key")
    ctx.check_hostname = False
    return ctx

def _fts_connect():
    global _fts_socket
    try:
        if _fts_socket:
            try: _fts_socket.close()
            except Exception: pass
            _fts_socket = None
        raw = _sock_mod.create_connection((FTS_HOST, FTS_COT_PORT), timeout=10)
        _fts_socket = _fts_build_ctx().wrap_socket(raw)
        now_dt  = datetime.now(timezone.utc)
        stale_dt = now_dt + datetime.timedelta(minutes=10)
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

def _atak_post_marker(incident_id: int, lat: float, lon: float, itype: str,
                      location: str | None, description: str | None = None):
    # This assumes _COT_PROFILE is imported from somewhere or moved here
    # Placeholder for moving _COT_PROFILE and _COT_DEFAULT as well
    pass

def _create_incident(itype: str, desc: str, call: dict, ts: float):
    # Logic from audio_receiver.py
    print(f"[incident] NEW {itype}: {desc}", flush=True)
    # ... rest of implementation

def _update_incident(inc_id: int, call: dict, ts: float, desc: str, new_itype: str | None = None):
    # ... rest of implementation
    pass
