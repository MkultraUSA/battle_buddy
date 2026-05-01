#!/usr/bin/env python3
"""
Battle Buddy — Audio Receiver / Brain  v2.0

Receives call audio from call_recorder.py on Pi 1,
transcribes with Whisper, categorizes by talkgroup,
detects incidents, controls OP25 hold/skip on Pi 1,
stores in SQLite, and serves a map + sitrep web UI.

Usage:
    python3 audio_receiver.py [--port 9001] [--model base] [--enable-hold]
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import pwd
import re
import shutil
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.request
import uuid

import modules.weather as weather_mod

try:
    import anthropic
except ImportError:
    anthropic = None
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_CDT = ZoneInfo("America/Chicago")

# Bypass SSL cert verification for all urllib calls (Nextcloud snap cert not in system store)
_ssl_ctx = ssl._create_unverified_context()
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_ctx))
)

from flask import Flask, jsonify, render_template_string, request  # noqa: E402

from modules import atak as _atak_mod  # noqa: E402
from modules.atak import (  # noqa: E402
    _atak_resync_on_startup,
)
from modules.audio_dedup import is_duplicate_and_mark  # noqa: E402
from modules.commute import *  #noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------
from modules.config import *  # noqa: E402
from modules.config import _state  # noqa: E402
from modules.database import *  # noqa: E402
from modules.geocoding import *  # noqa: E402
from modules.incident_engine import *  # noqa: E402
from modules.incident_engine import (  # noqa: E402
    _active_incidents,
    _incident_lock,
)
from modules.llm import *  # noqa: E402
from modules.llm import _TGID_ID_MIN_LEN  # noqa: E402
from modules.pollers import *  # noqa: E402
from modules.pollers import _pi_command_queue  # noqa: E402
from modules.talk import _bot_reply, _get_or_create_dm_room  # noqa: E402
from modules.talkgroups import *  # noqa: E402
from modules.transcription import *  # noqa: E402
from modules.transcription import (  # noqa: E402
    _BROADCASTIFY_MAX,
    _MAX_PROCESS_THREADS,
    _broadcastify_sem,
    _get_fw_model,
    _process_sem,
)

app = Flask(__name__, static_folder="/opt/battlebuddy/static", static_url_path="/static")

from modules.public import public_bp  # noqa: E402, I001
app.register_blueprint(public_bp)


@app.route("/receive", methods=["POST"])
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


@app.route("/watchdog_event", methods=["POST"])
def watchdog_event():
    """Receive Pi watchdog events and forward as Talk DM alerts."""
    data  = request.get_json(force=True)
    event = data.get("event", "unknown")
    msg   = data.get("msg", "")
    icons = {"op25_down": "⚠️", "op25_recovered": "✅", "op25_failed": "🚨"}
    icon  = icons.get(event, "⚠️")
    full  = f"{icon} PI WATCHDOG: {msg}"
    print(f"[pi-watchdog] {full}", flush=True)
    threading.Thread(target=_pi_watchdog_alert, args=(full,), daemon=True).start()
    return jsonify({"status": "ok"}), 200


@app.route("/pi/commands", methods=["GET"])
def pi_commands():
    """Pi polls this endpoint for pending commands (restart_op25, etc.)."""
    cmds = list(_pi_command_queue)
    _pi_command_queue.clear()
    return jsonify({"commands": cmds}), 200


@app.route("/test_call", methods=["POST"])
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


@app.route("/api/calls")
def api_calls():
    return jsonify(recent_calls(200))


# --- Prometheus metrics endpoint (added 2026-04-18 / bak39) ---
try:
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
    from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

    _BB_METRICS_REGISTRY = CollectorRegistry()

    class _BBMetricsCollector:
        def collect(self):
            try:
                c = sqlite3.connect(DB_PATH, timeout=5.0)
                cur = c.cursor()

                cur.execute(
                    "SELECT COALESCE(itype,'unknown'), COUNT(*) "
                    "FROM incidents WHERE (is_test IS NULL OR is_test=0) "
                    "GROUP BY itype"
                )
                m = CounterMetricFamily(
                    "battlebuddy_incidents",
                    "Total non-test incidents detected by Battle Buddy, by itype",
                    labels=["itype"],
                )
                for itype, count in cur.fetchall():
                    m.add_metric([str(itype)], float(count))
                yield m

                cur.execute("SELECT COUNT(*) FROM calls")
                (call_count,) = cur.fetchone()
                m2 = CounterMetricFamily(
                    "battlebuddy_calls",
                    "Total transcribed radio calls across all talkgroups",
                )
                m2.add_metric([], float(call_count))
                yield m2

                cur.execute(
                    "SELECT "
                    "  CASE "
                    "    WHEN tag LIKE 'APD%' THEN 'APD' "
                    "    WHEN tag LIKE 'AFD%' THEN 'AFD' "
                    "    WHEN tag LIKE 'TCEMS%' THEN 'TCEMS' "
                    "    WHEN tag LIKE 'TCSO%' THEN 'TCSO' "
                    "    WHEN tag LIKE 'LE %' OR tag LIKE 'LE/%' OR tag LIKE 'Lago%' THEN 'LE_other' "
                    "    WHEN tag LIKE '%Scanner%' THEN 'scanner_gateway' "
                    "    ELSE 'other' "
                    "  END AS agency, "
                    "  COUNT(*) "
                    "FROM calls GROUP BY agency"
                )
                m3 = CounterMetricFamily(
                    "battlebuddy_calls_by_agency",
                    "Total transcribed radio calls grouped by agency prefix",
                    labels=["agency"],
                )
                for agency, count in cur.fetchall():
                    m3.add_metric([str(agency)], float(count))
                yield m3

                # --- homicide YTD gauge — sourced from curated homicides_2026.json ---
                try:
                    import json as _json
                    import os as _os
                    _hf = _os.path.join(_os.path.dirname(__file__), "homicides_2026.json")
                    _hdata = _json.load(open(_hf))
                    _homicide_victims = sum(h.get("count", 1) for h in _hdata)
                    _homicide_incidents = len(_hdata)
                except Exception:
                    _homicide_victims = 0
                    _homicide_incidents = 0
                g_hom_v = GaugeMetricFamily(
                    "battlebuddy_homicides_ytd_victims",
                    "Austin homicide victims tracked by Battle Buddy, year-to-date 2026",
                )
                g_hom_v.add_metric([], float(_homicide_victims))
                yield g_hom_v
                g_hom_i = GaugeMetricFamily(
                    "battlebuddy_homicides_ytd",
                    "Austin homicide incidents tracked by Battle Buddy, year-to-date 2026",
                )
                g_hom_i.add_metric([], float(_homicide_incidents))
                yield g_hom_i

                # --- shooting intelligence tiers (30-day window) ---
                import time as _time
                _now = _time.time()
                _30d = _now - (30 * 86400)
                CORROBORATING_AGENCIES = {"AFD", "TCEMS", "TCSO", "TCFD"}

                cur.execute(
                    "SELECT agencies FROM incidents "
                    "WHERE itype='SHOOTING' AND ts_start >= ? "
                    "AND (is_test IS NULL OR is_test=0) "
                    "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')",
                    (_30d,),
                )
                import json as _json2
                _s_confirmed = 0
                _s_signal = 0
                for (_ag,) in cur.fetchall():
                    try:
                        _ags = set(_json2.loads(_ag or "[]"))
                    except Exception:
                        _ags = set()
                    if _ags & CORROBORATING_AGENCIES:
                        _s_confirmed += 1
                    elif _ags - {"Unknown", "scanner_gateway", None, ""}:
                        _s_signal += 1

                cur.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE itype='SHOOTING' AND ts_start >= ? "
                    "AND (is_test IS NULL OR is_test=0) "
                    "AND description LIKE '%[APD Press Release]%'",
                    (_30d,),
                )
                (_s_press,) = cur.fetchone()

                for _name, _help, _val in [
                    ("battlebuddy_shootings_confirmed_30d",
                     "Shooting incidents corroborated by AFD/TCEMS/TCSO radio in last 30 days",
                     _s_confirmed),
                    ("battlebuddy_shootings_signal_30d",
                     "Shooting incidents on known agency talkgroup, unverified, last 30 days",
                     _s_signal),
                    ("battlebuddy_shootings_press_release_30d",
                     "Shooting incidents from APD press releases in last 30 days",
                     _s_press),
                ]:
                    _g = GaugeMetricFamily(_name, _help)
                    _g.add_metric([], float(_val))
                    yield _g

                # --- live gauges ---
                _window = _now - 86400

                cur.execute(
                    "SELECT COUNT(*) FROM incidents "
                    "WHERE status='active' AND (is_test IS NULL OR is_test=0) "
                    "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')"
                )
                (active_count,) = cur.fetchone()
                g_active = GaugeMetricFamily(
                    "battlebuddy_active_incidents",
                    "Currently active (non-cleared) Battle Buddy incidents",
                )
                g_active.add_metric([], float(active_count))
                yield g_active

                cur.execute(
                    "SELECT COALESCE(itype,'unknown'), COUNT(*) FROM incidents "
                    "WHERE ts_start >= ? AND (is_test IS NULL OR is_test=0) "
                    "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%') "
                    "GROUP BY itype",
                    (_window,),
                )
                g24 = GaugeMetricFamily(
                    "battlebuddy_incidents_24h",
                    "Incidents detected in the last 24 hours by itype",
                    labels=["itype"],
                )
                for itype, count in cur.fetchall():
                    g24.add_metric([str(itype)], float(count))
                yield g24

                cur.execute(
                    "SELECT COUNT(*) FROM calls WHERE ts >= ?",
                    (_window,),
                )
                (calls_24h,) = cur.fetchone()
                g_calls = GaugeMetricFamily(
                    "battlebuddy_calls_24h",
                    "Radio calls received in the last 24 hours",
                )
                g_calls.add_metric([], float(calls_24h))
                yield g_calls

                # --- process memory / thread metrics (leak detection) ---
                try:
                    import psutil as _psutil
                    _proc = _psutil.Process()
                    _mi = _proc.memory_info()
                    g_rss = GaugeMetricFamily(
                        'battlebuddy_process_rss_bytes',
                        'Battle Buddy Flask process RSS memory in bytes',
                    )
                    g_rss.add_metric([], float(_mi.rss))
                    yield g_rss
                    g_vms = GaugeMetricFamily(
                        'battlebuddy_process_vms_bytes',
                        'Battle Buddy Flask process VMS (virtual) memory in bytes',
                    )
                    g_vms.add_metric([], float(_mi.vms))
                    yield g_vms
                    g_thr = GaugeMetricFamily(
                        'battlebuddy_process_threads',
                        'Battle Buddy Flask process thread count',
                    )
                    g_thr.add_metric([], float(_proc.num_threads()))
                    yield g_thr
                except Exception as _pe:
                    print(f'[metrics] psutil error: {_pe}', flush=True)

                c.close()
            except Exception as _e:
                print(f"[metrics] collector error: {_e}", flush=True)

    _BB_METRICS_REGISTRY.register(_BBMetricsCollector())

    @app.route("/metrics")
    def prometheus_metrics():
        return (
            generate_latest(_BB_METRICS_REGISTRY),
            200,
            {"Content-Type": CONTENT_TYPE_LATEST},
        )
except Exception as _metrics_init_err:
    print(f"[metrics] disabled \u2014 init failed: {_metrics_init_err}", flush=True)
# --- end Prometheus metrics ---



@app.route("/api/sitrep")
def api_sitrep():
    minutes = int(request.args.get("minutes", 60))
    return jsonify({"sitrep": build_sitrep(minutes)})


@app.route("/api/voice_sitrep")
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


@app.route("/api/incidents")
def api_incidents():
    return jsonify(get_all_incidents(50))


@app.route("/api/incidents/active")
def api_incidents_active():
    return jsonify(active_incidents())


@app.route("/api/stats")
def api_stats():
    """24-hour summary stats for the splash page."""
    since = time.time() - 86400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    calls_24h = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE ts > ?", (since,)
    ).fetchone()[0]
    inc_24h = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE ts_start > ? AND is_test=0"
        " AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')", (since,)
    ).fetchone()[0]
    no_loc = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE ts_start > ? AND is_test=0"
        " AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')"
        " AND (location IS NULL OR location='')",
        (since,)
    ).fetchone()[0]
    agencies_24h = conn.execute(
        "SELECT COUNT(DISTINCT category) FROM calls WHERE ts > ? AND category IS NOT NULL AND category != '' AND category != 'Unknown'",
        (since,)
    ).fetchone()[0]
    conn.close()
    return jsonify({"calls_24h": calls_24h, "incidents_24h": inc_24h, "no_location": no_loc, "agencies_24h": agencies_24h})


@app.route("/api/shooting_intel")
def api_shooting_intel():
    """
    Public endpoint: shooting incidents with transcript evidence, last 30 days.
    Returns only incidents corroborated by known (non-encrypted) agencies.
    Excludes APD press releases (those are in the verified homicide/press tracker).
    """
    import json as _json
    since = time.time() - (30 * 86400)
    CORROBORATING = {"AFD", "TCEMS", "TCSO", "TCFD"}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT i.id, i.itype, i.agencies, i.description, i.location, i.lat, i.lon, "
        "       i.ts_start, i.status "
        "FROM incidents i "
        "WHERE i.itype IN ('SHOOTING','STABBING') "
        "AND i.ts_start >= ? "
        "AND (i.is_test IS NULL OR i.is_test=0) "
        "AND (i.description IS NULL OR i.description NOT LIKE '%[APD Press Release]%') "
        "ORDER BY i.ts_start DESC",
        (since,)
    ).fetchall()

    results = []
    for row in rows:
        try:
            agencies = set(_json.loads(row["agencies"] or "[]"))
        except Exception:
            agencies = set()

        # Only include incidents from known, non-encrypted agencies
        known = agencies - {"Unknown", "scanner_gateway", None, ""}
        if not known:
            continue

        if agencies & CORROBORATING:
            tier = "confirmed"
            tier_label = "Corroborated — EMS/Fire radio"
        else:
            tier = "radio_signal"
            tier_label = "Radio signal — known agency, under investigation"

        # Pull corroborating transcripts
        transcripts = conn.execute(
            "SELECT c.tag, c.category, c.transcript, datetime(c.ts,'unixepoch','localtime') as ts "
            "FROM calls c JOIN incident_calls ic ON ic.call_id = c.id "
            "WHERE ic.incident_id = ? "
            "AND c.category NOT IN ('Unknown') "
            "AND length(c.transcript) > 10 "
            "ORDER BY c.ts ASC LIMIT 5",
            (row["id"],)
        ).fetchall()

        results.append({
            "incident_id": row["id"],
            "itype": row["itype"],
            "ts": row["ts_start"],
            "ts_local": __import__("datetime").datetime.fromtimestamp(
                row["ts_start"],
                tz=__import__("zoneinfo").ZoneInfo("America/Chicago")
            ).strftime("%Y-%m-%d %H:%M CDT"),
            "location": row["location"],
            "lat": row["lat"],
            "lon": row["lon"],
            "agencies": list(agencies),
            "confidence_tier": tier,
            "confidence_label": tier_label,
            "evidence": [
                {
                    "agency": t["category"],
                    "talkgroup": t["tag"],
                    "time": t["ts"],
                    "transcript": t["transcript"][:300],
                }
                for t in transcripts
            ],
        })

    conn.close()
    return jsonify({
        "window": "last_30_days",
        "methodology": (
            "Only incidents detected on known, non-encrypted agency talkgroups are included. "
            "'Confirmed' = corroborated by AFD, TCEMS, TCSO, or TCFD radio traffic. "
            "'Radio signal' = detected on a known agency talkgroup but not yet corroborated by a second agency. "
            "APD radio is P25 encrypted — APD transcripts are not available. "
            "All evidence is verbatim radio transcript text."
        ),
        "count": len(results),
        "confirmed": sum(1 for r in results if r["confidence_tier"] == "confirmed"),
        "radio_signal": sum(1 for r in results if r["confidence_tier"] == "radio_signal"),
        "incidents": results,
    })


@app.route("/api/daily_summary")
def api_daily_summary():
    """
    Public daily summary of incidents bucketed by confidence tier.

    Confidence tiers:
      confirmed     — corroborated by AFD, TCEMS, TCSO, or TCFD radio traffic
      radio_signal  — detected on known (non-Unknown) agency talkgroup, single source
      press_release — APD press release (verified, but lagging and APD-selected)
      unconfirmed   — Unknown agency / scanner gateway only (low confidence)
    """
    import json as _json
    since = time.time() - 86400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT itype, description, agencies FROM incidents "
        "WHERE ts_start > ? AND (is_test IS NULL OR is_test=0) "
        "ORDER BY ts_start DESC",
        (since,)
    ).fetchall()
    conn.close()

    CORROBORATING = {"AFD", "TCEMS", "TCSO", "TCFD"}

    confirmed = []
    radio_signal = []
    press_release = []
    unconfirmed = []

    for row in rows:
        desc = row["description"] or ""
        itype = row["itype"] or "UNKNOWN"
        try:
            agencies = set(_json.loads(row["agencies"] or "[]"))
        except Exception:
            agencies = set()

        if desc.startswith("[APD Press Release]"):
            press_release.append(itype)
        elif agencies & CORROBORATING:
            confirmed.append(itype)
        elif agencies - {"Unknown", "scanner_gateway", None, ""}:
            radio_signal.append(itype)
        else:
            unconfirmed.append(itype)

    def summarize(lst):
        from collections import Counter
        return dict(Counter(lst))

    return jsonify({
        "window": "last_24h",
        "note": (
            "Confirmed = corroborated by AFD/TCEMS/TCSO/TCFD radio. "
            "Radio signal = single known agency, unverified. "
            "Press release = APD-published, verified but lagging. "
            "Unconfirmed = encrypted scanner noise, treat as rumor only."
        ),
        "confirmed":     {"count": len(confirmed),     "by_type": summarize(confirmed)},
        "radio_signal":  {"count": len(radio_signal),  "by_type": summarize(radio_signal)},
        "press_release": {"count": len(press_release), "by_type": summarize(press_release)},
        "unconfirmed":   {"count": len(unconfirmed),   "by_type": summarize(unconfirmed)},
    })


@app.route("/api/tgid_guesses")
def api_tgid_guesses():
    """Return all TGID identification guesses, grouped by tgid."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM tgid_guesses ORDER BY ts DESC LIMIT 500"
    ).fetchall()
    conn.close()

    # Group by tgid — include count of HIGH/MED guesses and top guess
    from collections import Counter, defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r["tgid"]].append(dict(r))

    result = []
    for tgid, guesses in sorted(groups.items()):
        hm = [g["guess"] for g in guesses if g["confidence"] in ("HIGH", "MED") and g["guess"]]
        top = Counter(hm).most_common(1)[0] if hm else (None, 0)
        result.append({
            "tgid":          tgid,
            "guess_count":   len(guesses),
            "top_guess":     top[0],
            "top_count":     top[1],
            "confirmed":     any(g["confirmed"] for g in guesses),
            "guesses":       guesses[:10],   # most recent 10
        })

    return jsonify(result)


@app.route("/api/tgid_guesses/confirm", methods=["POST"])
def api_tgid_confirm():
    """Manually confirm a TGID name and write it to the tags TSV."""
    data  = request.get_json(force=True)
    tgid  = int(data.get("tgid", 0))
    name  = (data.get("name") or "").strip()
    if not tgid or not name:
        return jsonify({"error": "tgid and name required"}), 400

    # Write to tags file
    try:
        with open(TGID_TSV, "a") as f:
            f.write(f"{tgid}\t{name}\n")
        # Mark confirmed in DB
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid,))
        conn.commit()
        conn.close()
        # Reload talkgroup table
        load_talkgroups()
        return jsonify({"status": "ok", "tgid": tgid, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/drone_sighting", methods=["POST"])
def api_drone_sighting():
    """Receive a drone sighting from the DroneRID Android app."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "no JSON body"}), 400
    serial = data.get("serial", "").strip()
    lat    = data.get("lat")
    lon    = data.get("lon")
    if not serial or lat is None or lon is None:
        return jsonify({"error": "serial, lat, lon required"}), 400
    ts = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO drone_sightings (ts, serial, ua_type, lat, lon, alt_geo, alt_agl, speed_ms, heading) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            serial,
            int(data.get("ua_type", 0)),
            float(lat),
            float(lon),
            float(data.get("alt_geo", 0)),
            float(data.get("alt_agl", 0)),
            float(data.get("speed_ms", 0)),
            int(data.get("heading", 0)),
        )
    )
    conn.commit()
    conn.close()
    app.logger.info(f"Drone sighting: serial={serial} lat={lat} lon={lon} agl={data.get('alt_agl')}m")
    return jsonify({"status": "ok", "serial": serial, "ts": ts})


@app.route("/api/drone_sightings")
def api_drone_sightings():
    """Return recent drone sightings (last 24h by default, ?hours=N to override)."""
    hours = min(int(request.args.get("hours", 24)), 168)
    since = time.time() - (hours * 3600)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM drone_sightings WHERE ts > ? ORDER BY ts DESC LIMIT 500",
        (since,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/adsb")
def api_adsb():
    """Return current aircraft positions + 30-min trails grouped by icao24."""
    cutoff = time.time() - ADSB_TRAIL_SECS
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM aircraft_positions WHERE ts > ? ORDER BY icao24, ts",
        (cutoff,)
    ).fetchall()
    conn.close()

    aircraft: dict = {}
    for r in rows:
        icao = r["icao24"]
        if icao not in aircraft:
            aircraft[icao] = {
                "icao24":   icao,
                "label":    r["label"],
                "callsign": r["callsign"],
                "is_leo":   bool(r["is_leo"]),
                "lat":      r["lat"],
                "lon":      r["lon"],
                "alt_ft":   r["alt_ft"],
                "heading":  r["heading"],
                "ts":       r["ts"],
                "trail":    [],
            }
        else:
            # Keep latest position as current
            aircraft[icao].update({
                "lat":     r["lat"],
                "lon":     r["lon"],
                "alt_ft":  r["alt_ft"],
                "heading": r["heading"],
                "ts":      r["ts"],
            })
        aircraft[icao]["trail"].append([r["lat"], r["lon"], r["ts"]])

    return jsonify(list(aircraft.values()))


@app.route("/")
def index():
    return render_template_string(HTML)


# ---------------------------------------------------------------------------
# Talk bot webhook — handles slash commands from Nextcloud Talk users
# Register with: occ talk:bot:install "Battle Buddy" <secret> http://127.0.0.1:9001/bot/talk
# ---------------------------------------------------------------------------

def _verify_bot_signature(raw_body: bytes, random_header: str, sig_header: str) -> bool:
    """Verify Nextcloud Talk bot HMAC-SHA256 signature."""
    expected = hmac.new(
        TALK_BOT_SECRET.encode(),
        (random_header + raw_body.decode()).encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header.lower())





@app.route("/bot/talk", methods=["POST"])
def bot_talk():
    raw_body      = request.get_data()
    random_header = request.headers.get("X-Nextcloud-Talk-Random", "")
    sig_header    = request.headers.get("X-Nextcloud-Talk-Signature", "")

    if not _verify_bot_signature(raw_body, random_header, sig_header):
        return jsonify({"error": "invalid signature"}), 401

    data    = json.loads(raw_body)
    raw_content = (data.get("object", {}).get("content") or "")
    # Content may be a JSON-encoded message object or plain text
    try:
        parsed = json.loads(raw_content)
        content = (parsed.get("message") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        content = raw_content.strip()
    token   = data.get("target", {}).get("id", TALK_ROOM)
    actor   = data.get("actor", {}).get("name", "someone")

    # Ignore messages posted by the bot itself to avoid loops
    if actor in ("Battle Buddy", TALK_USER):
        return jsonify({"status": "ignored"}), 200

    print(f"[bot] received from {actor}: '{content[:80]}'", flush=True)

    # Only respond to !commands
    if not content.startswith("!"):
        return jsonify({"status": "ignored"}), 200

    parts   = content.split()
    command = parts[0].lower()
    print(f"[bot] executing command: {command}", flush=True)

    def respond(msg):
        threading.Thread(target=_bot_reply, args=(token, msg), daemon=True).start()

    if command == "!sitrep":
        try:
            minutes = int(parts[1]) if len(parts) > 1 else 60
            minutes = max(5, min(minutes, 360))
        except ValueError:
            minutes = 60
        sitrep = build_sitrep(minutes)
        respond(f"📋 Sitrep requested by {actor} (last {minutes} min)\n\n{sitrep}")

    elif command == "!incidents":
        incs = active_incidents()
        if not incs:
            respond("✅ No active incidents at this time.")
        else:
            lines = [f"⚡ {len(incs)} active incident(s):"]
            for inc in incs:
                age     = int((time.time() - inc["ts_start"]) / 60)
                updated = int((time.time() - inc["ts_updated"]) / 60)
                loc     = f" @ {inc['location']}" if inc.get("location") else ""
                agencies = ", ".join(json.loads(inc.get("agencies") or "[]"))
                lines.append(
                    f"• {inc['itype']}{loc} — started {age}m ago, "
                    f"last update {updated}m ago — {agencies}"
                )
                if inc.get("description"):
                    lines.append(f"  {inc['description']}")
            respond("\n".join(lines))

    elif command == "!status":
        calls_1h  = len(calls_since(time.time() - 3600))
        calls_15m = len(calls_since(time.time() - 900))
        incs      = active_incidents()
        held      = _current_hold_tgid
        hold_str  = f"Holding TGID {held}" if held else "No hold active"
        respond(
            f"🛰 Battle Buddy Status\n"
            f"Calls (last 15m): {calls_15m}  |  Calls (last 1h): {calls_1h}\n"
            f"Active incidents: {len(incs)}\n"
            f"OP25 hold: {hold_str}\n"
            f"Transcription: faster-whisper large-v3-turbo INT8 (local)"
        )

    elif command == "!subscribe":
        beat = parts[1].lower() if len(parts) > 1 else "all"
        valid = {"all", "apd", "fire-ems", "general"}
        if beat not in valid:
            respond(f"Unknown beat '{beat}'. Valid options: all, apd, fire-ems, general")
        else:
            # Resolve Nextcloud username from display name
            nc_user = data.get("actor", {}).get("id", "").replace("users/", "")
            if not nc_user:
                nc_user = actor.lower().replace(" ", "")
            add_subscription(nc_user, beat)
            respond(
                f"✅ {actor} subscribed to 🔴 alerts"
                + (f" for beat: {beat}" if beat != "all" else " for all incidents")
                + "\nYou'll receive a direct message when a priority incident is detected."
            )

    elif command == "!unsubscribe":
        beat = parts[1].lower() if len(parts) > 1 else "all"
        nc_user = data.get("actor", {}).get("id", "").replace("users/", "")
        if not nc_user:
            nc_user = actor.lower().replace(" ", "")
        remove_subscription(nc_user, beat)
        respond(f"🔕 {actor} unsubscribed from alerts (beat: {beat})")

    elif command == "!unknowns":
        # Show recent unknown TGID guesses
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT tgid, guess, confidence, COUNT(*) as cnt FROM tgid_guesses "
            "WHERE confirmed=0 GROUP BY tgid ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        conn.close()
        if not rows:
            respond("No unknown talkgroups identified yet.")
        else:
            lines = [f"🔍 Unknown TGIDs ({len(rows)} groups):"]
            for r in rows:
                lines.append(f"• TGID {r[0]}: {r[1] or '?'} ({r[2]}, {r[3]} guesses)")
            lines.append("\nTo confirm: !addtag <tgid> <name>")
            respond("\n".join(lines))

    elif command == "!addtag":
        # !addtag <tgid> <name with spaces>
        if len(parts) < 3:
            respond("Usage: !addtag <tgid> <name>  (e.g. !addtag 1373 APD South Patrol)")
        else:
            try:
                tgid_arg = int(parts[1])
                name_arg = " ".join(parts[2:])
                with open(TGID_TSV, "a") as f:
                    f.write(f"{tgid_arg}\t{name_arg}\n")
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid_arg,))
                conn.commit()
                conn.close()
                load_talkgroups()
                respond(f"✅ TGID {tgid_arg} → **{name_arg}** saved and loaded.")
            except ValueError:
                respond("Error: tgid must be a number.")
            except Exception as e:
                respond(f"Error saving tag: {e}")

    elif command == "!query":
        question = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if not question:
            respond(
                "Usage: !query <your question>\n\nExamples:\n"
                "• !query how many shootings this week\n"
                "• !query any structure fires near downtown today\n"
                "• !query what did radio say during the last homicide call\n"
                "• !query show last 5 APD transcripts\n"
                "• !query how many homicides in 2026"
            )
        else:
            def _do_query(q, tok, name):
                try:
                    import json as _j
                    import os as _os
                    import time as _t
                    import urllib.request as _ur
                    _now = _t.time()
                    conn = sqlite3.connect(DB_PATH, timeout=5)

                    rows_inc = conn.execute(
                        "SELECT itype, location, description, agencies, "
                        "datetime(ts_start,'unixepoch','localtime') as ts, status "
                        "FROM incidents WHERE ts_start >= ? "
                        "AND (is_test IS NULL OR is_test=0) "
                        "ORDER BY ts_start DESC LIMIT 80",
                        (_now - 30*86400,)
                    ).fetchall()

                    rows_calls = conn.execute(
                        "SELECT tag, transcript, datetime(ts,'unixepoch','localtime') as ts "
                        "FROM calls WHERE ts >= ? AND transcript != '' "
                        "ORDER BY ts DESC LIMIT 120",
                        (_now - 48*3600,)
                    ).fetchall()

                    conn.close()

                    try:
                        _hf = _os.path.join(_os.path.dirname(__file__), "homicides_2026.json")
                        _hdata = _j.load(open(_hf))
                        hom_summary = f"{len(_hdata)} homicide incidents, {sum(h.get('count',1) for h in _hdata)} victims YTD 2026"
                    except Exception:
                        hom_summary = "homicide data unavailable"

                    inc_text = "\n".join(
                        f"[{r[4]}] {r[0]} @ {r[1] or 'unknown location'} | {r[2] or 'no desc'} | agencies: {r[3]} | {r[5]}"
                        for r in rows_inc
                    ) or "no incidents in window"

                    calls_text = "\n".join(
                        f"[{r[2]}] {r[0]}: {r[1][:200]}"
                        for r in rows_calls
                    ) or "no calls in window"

                    system_prompt = (
                        "You are Battle Buddy, an AI assistant with access to Austin Texas public safety radio data. "
                        "Answer the user's question using ONLY the data provided below. Be concise and direct. "
                        "If the answer is not in the data, say so clearly — do not guess or hallucinate. "
                        "Format counts, lists, and summaries in plain text. No markdown headers or asterisks."
                    )
                    user_msg = (
                        f"DATABASE CONTEXT\n"
                        f"Homicides 2026: {hom_summary}\n\n"
                        f"Recent incidents (last 30 days, newest first):\n{inc_text}\n\n"
                        f"Recent radio transcripts (last 48h, newest first):\n{calls_text}\n\n"
                        f"USER QUESTION: {q}"
                    )

                    req = _ur.Request(
                        "https://openrouter.ai/api/v1/chat/completions",
                        data=_j.dumps({
                            "model": "google/gemini-2.5-flash",
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user",   "content": user_msg},
                            ],
                            "max_tokens": 400,
                            "temperature": 0.1,
                        }).encode(),
                        headers={
                            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                            "Content-Type":  "application/json",
                            "HTTP-Referer":  "https://battlebuddy.news",
                        },
                        method="POST",
                    )
                    with _ur.urlopen(req, timeout=25) as resp:
                        result = _j.loads(resp.read())
                    answer = result["choices"][0]["message"]["content"].strip()
                    _bot_reply(tok, f"🔍 {name} asked: {q}\n\n{answer}")
                except Exception as _e:
                    print(f"[bot] !query error: {_e}", flush=True)
                    _bot_reply(tok, f"❌ Query failed: {_e}")

            respond("⏳ Searching the database, one moment...")
            threading.Thread(target=_do_query, args=(question, token, actor), daemon=True).start()

    elif command == "!help":
        respond(
            "🤖 Battle Buddy Commands\n\n"
            "📋 SITUATION REPORTS\n"
            "!sitrep [minutes] — AI situation report (default 60m, max 360m)\n"
            "!incidents — Active incidents right now\n"
            "!status — System status and call volume\n\n"
            "🔍 DATABASE QUERY\n"
            "!query <question> — Ask anything about incidents and radio transcripts\n\n"
            "  Examples:\n"
            "  !query how many shootings happened this week\n"
            "  !query any structure fires near downtown today\n"
            "  !query what did radio say during the last homicide call\n"
            "  !query show me the last 5 APD transcripts\n"
            "  !query how many homicides in 2026\n"
            "  !query were there any SWAT callouts in the last 30 days\n"
            "  !query what agencies responded to the last multi-agency incident\n"
            "  !query any mass casualty events this month\n\n"
            "🔔 ALERTS\n"
            "!subscribe [beat] — 🔴 DM alerts (beats: all, apd, fire-ems, general)\n"
            "!unsubscribe [beat] — Stop DM alerts\n\n"
            "🛠 ADMIN\n"
            "!unknowns — Show unidentified talkgroup guesses\n"
            "!addtag <tgid> <name> — Confirm a talkgroup name\n\n"
            "!help — This message"
        )

    else:
        respond(f"Unknown command: {command}. Try !help")

    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; background: #0f172a; color: #e2e8f0; display: flex; flex-direction: column; height: 100vh; }
#header { padding: 8px 16px; background: #1e293b; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#header h1 { font-size: 1.1rem; color: #f8fafc; letter-spacing: 2px; }
#status { font-size: 0.75rem; color: #64748b; margin-left: auto; }
#incident-bar { display: none; padding: 6px 16px; background: #7f1d1d; border-bottom: 2px solid #ef4444; font-size: 0.78rem; color: #fca5a5; }
#incident-bar.active { display: block; }
#main { display: flex; flex: 1; overflow: hidden; }
#map { flex: 1; }
#sidebar { width: 380px; display: flex; flex-direction: column; border-left: 1px solid #334155; overflow: hidden; }
#tabs { display: flex; border-bottom: 1px solid #334155; }
.tab { flex: 1; padding: 8px; text-align: center; cursor: pointer; font-size: 0.75rem; color: #64748b; border-bottom: 2px solid transparent; }
.tab.active { color: #f8fafc; border-bottom-color: #3b82f6; }
.tab.alert { color: #ef4444 !important; }
#feed, #sitrep-panel, #incidents-panel { flex: 1; overflow-y: auto; padding: 8px; display: none; }
#feed.active, #sitrep-panel.active, #incidents-panel.active { display: block; }
.call { padding: 6px 8px; border-bottom: 1px solid #1e293b; font-size: 0.72rem; }
.call .meta { display: flex; gap: 8px; margin-bottom: 2px; flex-wrap: wrap; }
.call .time { color: #64748b; }
.call .tag { font-weight: bold; }
.call .cat { padding: 1px 5px; border-radius: 3px; font-size: 0.65rem; }
.call .transcript { color: #94a3b8; }
.call .loc { color: #34d399; font-size: 0.65rem; }
.incident-card { padding: 8px; margin-bottom: 8px; border-radius: 4px; border-left: 3px solid #ef4444; background: #1e293b; font-size: 0.75rem; }
.incident-card.active { border-left-color: #ef4444; }
.incident-card.cleared { border-left-color: #334155; opacity: 0.6; }
.incident-card .itype { font-weight: bold; color: #f87171; font-size: 0.8rem; margin-bottom: 4px; }
.incident-card.cleared .itype { color: #64748b; }
.incident-card .imeta { color: #64748b; font-size: 0.68rem; margin-bottom: 3px; }
.incident-card .idesc { color: #94a3b8; }
#sitrep-panel { white-space: pre-wrap; font-size: 0.72rem; color: #94a3b8; line-height: 1.6; }
#sitrep-controls { padding: 8px; border-top: 1px solid #334155; display: none; }
#sitrep-controls.active { display: flex; gap: 8px; }
#sitrep-controls select, #sitrep-controls button { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; padding: 4px 8px; font-family: monospace; font-size: 0.75rem; cursor: pointer; }
</style>
</head>
<body>
<div id="header">
  <h1>&#9652; BATTLE BUDDY</h1>
  <span id="status">connecting...</span>
</div>
<div id="incident-bar" id="incident-bar"></div>
<div id="main">
  <div id="map"></div>
  <div id="sidebar">
    <div id="tabs">
      <div class="tab active" onclick="showTab('feed')">LIVE FEED</div>
      <div class="tab" id="tab-incidents" onclick="showTab('incidents')">INCIDENTS</div>
      <div class="tab" onclick="showTab('sitrep')">SITREP</div>
    </div>
    <div id="feed" class="active"></div>
    <div id="incidents-panel"></div>
    <div id="sitrep-panel"></div>
    <div id="sitrep-controls">
      <select id="sitrep-mins">
        <option value="30">30 min</option>
        <option value="60" selected>1 hr</option>
        <option value="180">3 hr</option>
        <option value="360">6 hr</option>
      </select>
      <button onclick="loadSitrep()">Refresh</button>
    </div>
  </div>
</div>

<script>
const CAT_COLORS = """ + json.dumps(CAT_COLORS) + r""";

const map = L.map('map').setView([30.2672, -97.7431], 10);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 18
}).addTo(map);

const markers = {};

function catColor(cat) { return CAT_COLORS[cat] || CAT_COLORS['Unknown']; }

function makeIcon(cat, big) {
  const color = catColor(cat);
  const sz = big ? 18 : 12;
  return L.divIcon({
    html: `<div style="width:${sz}px;height:${sz}px;background:${color};border:2px solid white;border-radius:50%;box-shadow:0 0 6px ${color}"></div>`,
    iconSize: [sz,sz], iconAnchor: [sz/2,sz/2], className: ''
  });
}

function timeStr(ts) {
  return new Date(ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
}

function addCallToFeed(c) {
  const feed = document.getElementById('feed');
  const div = document.createElement('div');
  div.className = 'call';
  const color = catColor(c.category);
  div.innerHTML = `
    <div class="meta">
      <span class="time">${timeStr(c.ts)}</span>
      <span class="tag">${c.tag || 'TGID '+c.tgid}</span>
      <span class="cat" style="background:${color}22;color:${color}">${c.category||'?'}</span>
      ${c.location ? `<span class="loc">&#9654; ${c.location}</span>` : ''}
    </div>
    <div class="transcript">${c.transcript || '<em>transcribing...</em>'}</div>
  `;
  feed.insertBefore(div, feed.firstChild);
  while (feed.children.length > 100) feed.removeChild(feed.lastChild);
}

function addMarker(c) {
  if (!c.lat || !c.lon) return;
  const key = `${c.tgid}-${Math.round(c.ts)}`;
  if (markers[key]) return;
  const m = L.marker([c.lat, c.lon], {icon: makeIcon(c.category, false)}).addTo(map);
  const txt = c.transcript ? c.transcript.substring(0,120) : '(no transcript)';
  m.bindPopup(`<b>${c.tag || 'TGID '+c.tgid}</b><br><small>${timeStr(c.ts)}</small><br>${txt}`);
  markers[key] = m;
}

let lastCount = 0;

async function poll() {
  try {
    const resp = await fetch('/api/calls');
    const calls = await resp.json();
    document.getElementById('status').textContent =
      `${calls.length} calls | ${new Date().toLocaleTimeString()}`;
    if (calls.length !== lastCount) {
      if (lastCount === 0) {
        document.getElementById('feed').innerHTML = '';
        calls.forEach(c => { addCallToFeed(c); addMarker(c); });
      } else {
        const newCalls = calls.slice(0, calls.length - lastCount);
        newCalls.reverse().forEach(c => { addCallToFeed(c); addMarker(c); });
      }
      lastCount = calls.length;
    }
  } catch(e) {
    document.getElementById('status').textContent = 'connection error';
  }
}

async function pollIncidents() {
  try {
    const resp = await fetch('/api/incidents/active');
    const incidents = await resp.json();
    const bar = document.getElementById('incident-bar');
    const tab = document.getElementById('tab-incidents');

    if (incidents.length > 0) {
      bar.className = 'active';
      bar.textContent = '⚠ ACTIVE: ' + incidents.map(i => i.itype).join(' | ');
      tab.classList.add('alert');
    } else {
      bar.className = '';
      tab.classList.remove('alert');
    }

    if (document.getElementById('incidents-panel').classList.contains('active')) {
      renderIncidents();
    }
  } catch(e) {}
}

async function renderIncidents() {
  const resp = await fetch('/api/incidents');
  const incidents = await resp.json();
  const panel = document.getElementById('incidents-panel');
  if (!incidents.length) {
    panel.innerHTML = '<div style="padding:16px;color:#64748b">No incidents recorded yet.</div>';
    return;
  }
  panel.innerHTML = incidents.map(inc => {
    const age = Math.round((Date.now()/1000 - inc.ts_start) / 60);
    const agencies = (() => { try { return JSON.parse(inc.agencies||'[]').join(', '); } catch(e){ return ''; }})();
    const tgids    = (() => { try { return JSON.parse(inc.tgids||'[]').join(', '); } catch(e){ return ''; }})();
    const loc = inc.location ? ` @ ${inc.location}` : '';
    return `<div class="incident-card ${inc.status}">
      <div class="itype">${inc.itype}${loc}</div>
      <div class="imeta">${new Date(inc.ts_start*1000).toLocaleString()} · ${age}m ago · ${inc.status.toUpperCase()}</div>
      <div class="imeta">Agencies: ${agencies || 'unknown'} · TGIDs: ${tgids || 'unknown'}</div>
      <div class="idesc">${inc.description || ''}</div>
    </div>`;
  }).join('');
}

async function loadSitrep() {
  const mins = document.getElementById('sitrep-mins').value;
  const resp = await fetch(`/api/sitrep?minutes=${mins}`);
  const data = await resp.json();
  document.getElementById('sitrep-panel').textContent = data.sitrep;
}

function showTab(name) {
  ['feed','incidents','sitrep'].forEach(t => {
    document.getElementById(t === 'sitrep' ? 'sitrep-panel' : (t === 'incidents' ? 'incidents-panel' : 'feed'))
      .classList.toggle('active', t === name);
  });
  document.querySelectorAll('.tab').forEach((el, i) => {
    el.classList.toggle('active', ['feed','incidents','sitrep'][i] === name);
  });
  document.getElementById('sitrep-controls').classList.toggle('active', name === 'sitrep');
  if (name === 'sitrep') loadSitrep();
  if (name === 'incidents') renderIncidents();
}

const incMarkers = {};

function makeIncidentIcon(itype, status) {
  const active = status === 'active';
  const colors = {
    'SHOOTING': '#ef4444', 'OFFICER DOWN': '#ef4444', 'MASS CASUALTY': '#ef4444',
    'STRUCTURE FIRE': '#f97316', 'FIRE DISPATCH': '#f97316', 'FIRE/EMS DISPATCH': '#f97316',
    'FIRE ALARM': '#fbbf24', 'CRASH/COLLISION': '#fb923c', 'PURSUIT': '#fb923c',
    'HAZMAT': '#22c55e', 'WEAPONS': '#f97316',
  };
  const bg = colors[itype] || '#9ca3af';
  const abbr = itype.split(/[\s\/]/)[0].substring(0,3).toUpperCase();
  const opacity = active ? 1.0 : 0.55;
  const border = active ? '3px solid white' : '2px solid #94a3b8';
  const glow = active ? `box-shadow:0 0 10px ${bg},0 0 20px ${bg};` : '';
  return L.divIcon({
    html: `<div style="width:32px;height:32px;background:${bg};border:${border};border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:bold;color:white;opacity:${opacity};${glow}">${abbr}</div>`,
    iconSize: [32,32], iconAnchor: [16,16], className: ''
  });
}

async function pollIncidentMarkers() {
  try {
    const resp = await fetch('/api/incidents');
    const incidents = await resp.json();
    const seen = new Set();
    for (const inc of incidents) {
      // Only plot incidents with a real geocoded address (location field present)
      if (!inc.location || !inc.lat || !inc.lon) continue;
      seen.add(inc.id);
      const existing = incMarkers[inc.id];
      if (existing) {
        // Update icon if status changed
        existing.setIcon(makeIncidentIcon(inc.itype, inc.status));
      } else {
        const m = L.marker([inc.lat, inc.lon], {icon: makeIncidentIcon(inc.itype, inc.status), zIndexOffset: 1000}).addTo(map);
        const age = Math.round((Date.now()/1000 - inc.ts_start) / 60);
        m.bindPopup(`<b>${inc.itype}</b><br><small>${inc.status.toUpperCase()} · ${age}m ago</small><br>📍 ${inc.location}<br><small>${inc.description || ''}</small>`);
        incMarkers[inc.id] = m;
      }
    }
    // Remove markers for incidents no longer returned
    for (const id of Object.keys(incMarkers)) {
      if (!seen.has(parseInt(id))) {
        map.removeLayer(incMarkers[id]);
        delete incMarkers[id];
      }
    }
  } catch(e) {}
}

poll();
pollIncidents();
pollIncidentMarkers();
setInterval(poll, 5000);
setInterval(pollIncidents, 15000);
setInterval(pollIncidentMarkers, 30000);

// ── ADS-B helicopter layer ──────────────────────────────────────────────────
const adsbMarkers = {};
const adsbTrails  = {};

function makeHeloIcon(isLeo) {
  const color = isLeo ? '#f59e0b' : '#a855f7';  // amber=LEO, purple=unknown
  return L.divIcon({
    html: `<div style="font-size:20px;line-height:1;filter:drop-shadow(0 0 4px ${color});color:${color}">🚁</div>`,
    iconSize: [24,24], iconAnchor: [12,12], className: ''
  });
}

function adsbPopup(ac) {
  const ago = Math.round((Date.now()/1000 - ac.ts) / 60);
  const leo = ac.is_leo ? '<br><b style="color:#f59e0b">🔴 LEO</b>' : '';
  return `<b>${ac.label || ac.icao24}</b>${leo}
    <br>${ac.callsign ? 'Flight: ' + ac.callsign + '<br>' : ''}
    Alt: ${ac.alt_ft ? ac.alt_ft + ' ft' : '?'}
    | ICAO: ${ac.icao24}
    <br><small>${ago}m ago</small>`;
}

async function pollAdsb() {
  try {
    const resp = await fetch('/api/adsb');
    const aircraft = await resp.json();
    const seen = new Set();

    for (const ac of aircraft) {
      const key = ac.icao24;
      seen.add(key);

      // Draw / update trail
      const trailPts = ac.trail.map(p => [p[0], p[1]]);
      const trailColor = ac.is_leo ? '#f59e0b' : '#a855f7';
      if (adsbTrails[key]) {
        adsbTrails[key].setLatLngs(trailPts);
      } else {
        adsbTrails[key] = L.polyline(trailPts, {
          color: trailColor, weight: 1.5, opacity: 0.6, dashArray: '4 4'
        }).addTo(map);
      }

      // Draw / update marker
      if (adsbMarkers[key]) {
        adsbMarkers[key].setLatLng([ac.lat, ac.lon]);
        adsbMarkers[key].setPopupContent(adsbPopup(ac));
      } else {
        adsbMarkers[key] = L.marker([ac.lat, ac.lon], {icon: makeHeloIcon(ac.is_leo)})
          .bindPopup(adsbPopup(ac))
          .addTo(map);
      }
    }

    // Remove stale aircraft that dropped off
    for (const key of Object.keys(adsbMarkers)) {
      if (!seen.has(key)) {
        adsbMarkers[key].remove();
        delete adsbMarkers[key];
        if (adsbTrails[key]) { adsbTrails[key].remove(); delete adsbTrails[key]; }
      }
    }
  } catch(e) {}
}

pollAdsb();
setInterval(pollAdsb, 30000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------




def _load_active_incidents_from_db():
    """On startup, reload active incidents into _active_incidents so _release_stale can close them.
    Any incident older than 4 hours is closed immediately as stale."""
    MAX_AGE = 4 * 3600  # close anything untouched for >4 hours
    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status='active' AND is_test=0"
    ).fetchall()
    conn.close()

    to_close_now = []
    loaded = 0
    for row in rows:
        inc = dict(row)
        iid = inc['id']
        age = now - inc['ts_updated']
        if age > MAX_AGE:
            to_close_now.append(iid)
        else:
            with _incident_lock:
                _active_incidents[iid] = {
                    'itype':            inc.get('itype', 'UNKNOWN'),
                    'ts_updated':       inc['ts_updated'],
                    'agencies':         set(json.loads(inc['agencies']) if inc.get('agencies') else []),
                    'tgids':            set(json.loads(inc['tgids'])    if inc.get('tgids')    else []),
                    'lat':              inc.get('lat'),
                    'lon':              inc.get('lon'),
                    'escalation_stage': None,
                }
            loaded += 1

    if to_close_now:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany(
            "UPDATE incidents SET status='cleared', ts_cleared=? WHERE id=?",
            [(now, iid) for iid in to_close_now]
        )
        conn.commit()
        conn.close()
        print(f"[incident] startup cleanup: closed {len(to_close_now)} stale incident(s) (>4h old)", flush=True)

    if loaded:
        print(f"[incident] startup: loaded {loaded} active incident(s) into memory for timeout tracking", flush=True)

# ---------------------------------------------------------------------------
# Incident flagging — capture a full snapshot for demo/presentation use
# ---------------------------------------------------------------------------

def _nc_upload(path: str, data: bytes, content_type: str = "text/markdown"):
    """Upload a file to Nextcloud via WebDAV."""
    import base64 as _b64
    import urllib.request as _ur
    creds = _b64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    url   = f"{NC_WEBDAV}/{path}"
    req   = _ur.Request(url, data=data,
                        headers={"Authorization": f"Basic {creds}",
                                 "Content-Type": content_type},
                        method="PUT")
    try:
        _ur.urlopen(req, timeout=15)
        return True
    except Exception as exc:
        print(f"[flag] Nextcloud upload failed: {exc}", flush=True)
        return False


def _export_incident_snapshot(inc_id: int):
    """Build a markdown snapshot of a flagged incident and push to Nextcloud."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    inc = conn.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
    if not inc:
        conn.close()
        return
    inc = dict(inc)
    # Fetch all calls linked to this incident within its time window (+/- 5 min)
    t0 = inc["ts_start"] - 300
    t1 = (inc["ts_cleared"] or time.time()) + 300
    try:
        tgids = json.loads(inc.get("tgids") or "[]")
    except Exception:
        tgids = []
    calls = conn.execute(
        "SELECT ts, tgid, category, transcript, duration FROM calls "
        "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
        (t0, t1)
    ).fetchall()
    conn.close()

    from datetime import datetime as _dt
    start_str = _dt.fromtimestamp(inc["ts_start"]).strftime("%Y-%m-%d %H:%M:%S CDT")
    clear_str  = (_dt.fromtimestamp(inc["ts_cleared"]).strftime("%H:%M:%S CDT")
                  if inc.get("ts_cleared") else "ongoing")
    try:
        agencies = ", ".join(json.loads(inc.get("agencies") or "[]"))
    except Exception:
        agencies = "unknown"

    lines = [
        f"# Flagged Incident — {inc['itype']}",
        f"**Incident ID**: {inc_id}  ",
        f"**Time**: {start_str} → {clear_str}  ",
        f"**Location**: {inc.get('location') or 'No address extracted'}  ",
        f"**Coordinates**: {inc.get('lat')}, {inc.get('lon')}  ",
        f"**Agencies**: {agencies}  ",
        f"**TGIDs**: {', '.join(str(t) for t in tgids)}  ",
        f"**Status**: {inc['status'].upper()}  ",
        "",
        "## Description",
        inc.get("description") or "*(no description)*",
        "",
        "## Radio Traffic Timeline",
        f"*Calls within 5 minutes of incident window ({len(calls)} total)*",
        "",
    ]
    for c in calls:
        ts_str = _dt.fromtimestamp(c["ts"]).strftime("%H:%M:%S")
        tag    = c["category"] or f"TGID {c['tgid']}"
        xscr   = (c["transcript"] or "*(no transcript)*").strip()
        dur    = f"{c['duration']:.1f}s" if c["duration"] else ""
        lines.append(f"**{ts_str}** `{tag}` {dur}  ")
        lines.append(f"> {xscr}")
        lines.append("")

    lines += [
        "---",
        f"*Exported by Battle Buddy — {_dt.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*",
    ]

    slug     = start_str[:10].replace("-", "") + f"_{inc['itype'].replace(' ','_').replace('/','_')}_{inc_id}"
    filename = f"{NC_REPORT_DIR}/{slug}.md"
    data     = "\n".join(lines).encode("utf-8")

    # Ensure directory exists
    import base64 as _b64
    import urllib.request as _ur
    creds = _b64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    mkcol_req = _ur.Request(
        f"{NC_WEBDAV}/{NC_REPORT_DIR}",
        headers={"Authorization": f"Basic {creds}"},
        method="MKCOL"
    )
    try: _ur.urlopen(mkcol_req, timeout=10)  # noqa: E701
    except Exception: pass  # noqa: E701

    if _nc_upload(filename, data):
        print(f"[flag] snapshot uploaded: {filename}", flush=True)
    return filename


@app.route("/api/incidents/<int:inc_id>/flag", methods=["POST"])
def api_flag_incident(inc_id):
    """Flag an incident and export a snapshot to Nextcloud."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE incidents SET flagged=1 WHERE id=?", (inc_id,))
    conn.commit()
    conn.close()
    threading.Thread(target=_export_incident_snapshot, args=(inc_id,), daemon=True).start()
    return jsonify({"status": "flagged", "id": inc_id})


@app.route("/api/incidents/flagged")
def api_flagged_incidents():
    """Return all flagged incidents."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents WHERE flagged=1 ORDER BY ts_start DESC"
    ).fetchall()
    conn.close()
    return jsonify([_fill_incident_coords(dict(r)) for r in rows])

# ── TIP SUBMISSION SYSTEM ────────────────────────────────────────────────────
import os as _os  # noqa: E402

_os.makedirs(TIPS_UPLOAD_DIR, exist_ok=True)

_ALLOWED_TIP_EXT = {"jpg", "jpeg", "png", "gif", "webp"}

def _save_tip_photo(file) -> str | None:
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _ALLOWED_TIP_EXT:
        return None
    filename = uuid.uuid4().hex + "." + ext
    file.save(os.path.join(TIPS_UPLOAD_DIR, filename))
    return filename


def _notify_new_tip(tip_id: int, location_text: str, description: str,
                    photo_path: str | None, lat, lon, ts: float):
    """DM kevin when a new tip arrives. Runs in a thread."""
    token = _get_or_create_dm_room("kevin")
    if not token:
        print("[tip] could not get DM room for kevin", flush=True)
        return

    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %I:%M %p")
    coords_str = f"{lat:.5f}, {lon:.5f}" if (lat and lon) else "not geocoded"

    lines = [
        f"📍 NEW TIP #{tip_id} — review needed",
        "",
        f"Location: {location_text}",
        f"Coords: {coords_str}",
        f"Time: {time_str}",
    ]
    if description:
        lines += ["", "What they saw:", description]
    if photo_path:
        lines += ["", f"📷 https://battlebuddy.news/static/tips/{photo_path}"]
    lines += [
        "",
        "Review: https://battlebuddy.news/admin/tips",
        f"To investigate: ask me to look into tip #{tip_id}",
    ]

    _bot_reply(token, chr(10).join(lines))
    print(f"[tip] DM sent to kevin for tip #{tip_id}", flush=True)


TIP_FORM_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Submit a Tip</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; min-height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover { color: #3b82f6; }
#topbar .nav a.active { color: #3b82f6; }
.container { max-width: 600px; margin: 40px auto; padding: 0 20px 60px; }
h1 { font-size: 1.4rem; font-weight: 700; color: #e2e8f0; margin-bottom: 6px; }
.subtitle { color: #64748b; font-size: 0.85rem; margin-bottom: 32px; line-height: 1.5; }
.field { margin-bottom: 22px; }
label { display: block; font-size: 0.8rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
label .req { color: #ef4444; margin-left: 3px; }
input[type=text], input[type=datetime-local], textarea {
  width: 100%; background: #1e293b; border: 1px solid #1e3a5f; border-radius: 6px;
  color: #e2e8f0; padding: 10px 14px; font-size: 0.9rem; outline: none;
  font-family: inherit; transition: border-color 0.2s;
}
input[type=text]:focus, textarea:focus, input[type=datetime-local]:focus { border-color: #3b82f6; }
textarea { min-height: 120px; resize: vertical; line-height: 1.5; }
.file-label {
  display: flex; align-items: center; gap: 10px; background: #1e293b;
  border: 1px dashed #1e3a5f; border-radius: 6px; padding: 14px;
  cursor: pointer; transition: border-color 0.2s; color: #64748b; font-size: 0.85rem;
}
.file-label:hover { border-color: #3b82f6; color: #94a3b8; }
input[type=file] { display: none; }
#file-name { font-size: 0.8rem; color: #3b82f6; }
.hint { font-size: 0.75rem; color: #475569; margin-top: 6px; }
.anon-note { background: #0f1729; border: 1px solid #1e3a5f; border-radius: 8px; padding: 14px 16px; margin-bottom: 28px; font-size: 0.8rem; color: #64748b; line-height: 1.6; }
.anon-note strong { color: #94a3b8; }
button[type=submit] {
  width: 100%; background: #1d4ed8; color: #fff; border: none; border-radius: 8px;
  padding: 13px; font-size: 0.95rem; font-weight: 600; cursor: pointer;
  letter-spacing: 0.5px; transition: background 0.2s;
}
button[type=submit]:hover { background: #2563eb; }
.hp { display: none; }
#confirm { display: none; text-align: center; padding: 40px 20px; }
#confirm .check { font-size: 3rem; margin-bottom: 16px; }
#confirm h2 { color: #22c55e; margin-bottom: 10px; }
#confirm p { color: #64748b; font-size: 0.9rem; line-height: 1.6; }
#confirm a { color: #3b82f6; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="/tip" class="active">Submit Tip</a>
  </nav>
</div>
<div class="container">
  <div id="form-wrap">
    <h1>Submit a Tip</h1>
    <p class="subtitle">Saw something in Austin worth knowing about? City operations, unusual activity, police presence, encampments, infrastructure — anything that adds to the picture. All tips are reviewed before appearing anywhere.</p>
    <div class="anon-note"><strong>Anonymous by default.</strong> We do not log your IP address or require an account. What you submit is all we receive.</div>
    <form id="tip-form" enctype="multipart/form-data">
      <div class="field">
        <label>Location <span class="req">*</span></label>
        <input type="text" name="location_text" id="location_text" placeholder="e.g. South Congress and Wasson Road" required autocomplete="off">
        <div class="hint">Street intersection, address, or landmark. Be as specific as you can.</div>
      </div>
      <div class="field">
        <label>What did you see?</label>
        <textarea name="description" placeholder="Describe what you observed — who, what, how many, any vehicles or equipment..."></textarea>
      </div>
      <div class="field">
        <label>When</label>
        <input type="datetime-local" name="observed_at" id="observed_at">
        <div class="hint">Leave blank to use current time.</div>
      </div>
      <div class="field">
        <label>Photo <span style="color:#475569;font-weight:400;text-transform:none">(optional)</span></label>
        <label class="file-label" for="photo">
          <span>&#128247; Attach a photo</span>
          <span id="file-name"></span>
        </label>
        <input type="file" name="photo" id="photo" accept="image/*">
        <div class="hint">JPG, PNG, GIF, or WebP. Max 10 MB.</div>
      </div>
      <div class="hp"><input type="text" name="website" tabindex="-1" autocomplete="off"></div>
      <button type="submit">Submit Tip</button>
    </form>
  </div>
  <div id="confirm">
    <div class="check">&#10003;</div>
    <h2>Tip received</h2>
    <p>Thank you. Your tip has been logged and will be reviewed shortly.<br><br><a href="/public">Return to the live map</a> &nbsp;·&nbsp; <a href="/tip">Submit another</a></p>
  </div>
</div>
<script>
document.getElementById('observed_at').value = new Date().toISOString().slice(0,16);
document.getElementById('photo').addEventListener('change', function() {
  document.getElementById('file-name').textContent = this.files[0] ? this.files[0].name : '';
});
document.getElementById('tip-form').addEventListener('submit', async function(e) {
  e.preventDefault();
  const btn = this.querySelector('button[type=submit]');
  btn.disabled = true; btn.textContent = 'Submitting...';
  const fd = new FormData(this);
  try {
    const r = await fetch('/tip', {method:'POST', body: fd});
    if (r.ok) {
      document.getElementById('form-wrap').style.display = 'none';
      document.getElementById('confirm').style.display = 'block';
    } else {
      btn.disabled = false; btn.textContent = 'Submit Tip';
      alert('Submission failed. Please try again.');
    }
  } catch(err) {
    btn.disabled = false; btn.textContent = 'Submit Tip';
    alert('Network error. Please try again.');
  }
});
</script>
</body>
</html>"""

TIPS_ADMIN_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Tip Review</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; padding: 30px; }
h1 { font-size: 1.3rem; font-weight: 700; color: #3b82f6; letter-spacing: 2px; margin-bottom: 6px; }
.counts { color: #64748b; font-size: 0.8rem; margin-bottom: 28px; }
.tip-card { background: #0f1729; border: 1px solid #1e3a5f; border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }
.tip-card.approved { border-color: #166534; }
.tip-card.rejected { border-color: #374151; opacity: 0.55; }
.tip-meta { font-size: 0.72rem; color: #64748b; margin-bottom: 8px; }
.tip-location { font-size: 1rem; font-weight: 600; color: #e2e8f0; margin-bottom: 6px; }
.tip-desc { font-size: 0.85rem; color: #94a3b8; line-height: 1.5; margin-bottom: 10px; white-space: pre-wrap; }
.tip-coords { font-size: 0.72rem; color: #475569; margin-bottom: 10px; }
.tip-photo img { max-width: 280px; max-height: 200px; border-radius: 6px; border: 1px solid #1e3a5f; margin-bottom: 10px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.btn-approve { background: #166534; color: #86efac; border: 1px solid #166534; border-radius: 6px; padding: 6px 16px; font-size: 0.8rem; cursor: pointer; font-weight: 600; }
.btn-approve:hover { background: #15803d; }
.btn-reject { background: transparent; color: #94a3b8; border: 1px solid #374151; border-radius: 6px; padding: 6px 16px; font-size: 0.8rem; cursor: pointer; }
.btn-reject:hover { border-color: #ef4444; color: #ef4444; }
.badge { font-size: 0.7rem; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.badge-pending { background: #1e3a5f; color: #60a5fa; }
.badge-approved { background: #14532d; color: #86efac; }
.badge-rejected { background: #1f2937; color: #6b7280; }
.section-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: #475569; margin: 28px 0 12px; }
.note-input { background: #1e293b; border: 1px solid #1e3a5f; border-radius: 6px; color: #e2e8f0; padding: 6px 10px; font-size: 0.8rem; width: 220px; }
</style>
</head>
<body>
<h1>&#9652; TIP REVIEW</h1>
<div class="counts" id="counts">Loading...</div>
<div class="section-label">Pending Review</div>
<div id="pending-list"></div>
<div class="section-label">Reviewed</div>
<div id="reviewed-list"></div>
<script>
async function loadTips() {
  const r = await fetch('/api/tips');
  const tips = await r.json();
  const pending = tips.filter(t => t.status === 'pending');
  const reviewed = tips.filter(t => t.status !== 'pending');
  document.getElementById('counts').textContent =
    pending.length + ' pending · ' + reviewed.length + ' reviewed · ' + tips.length + ' total';
  document.getElementById('pending-list').innerHTML = pending.map(tipCard).join('') || '<p style="color:#475569;font-size:0.85rem">No pending tips.</p>';
  document.getElementById('reviewed-list').innerHTML = reviewed.map(tipCard).join('') || '<p style="color:#475569;font-size:0.85rem">None yet.</p>';
}
function tipCard(t) {
  const dt = new Date(t.ts * 1000).toLocaleString();
  const badge = `<span class="badge badge-${t.status}">${t.status.toUpperCase()}</span>`;
  const photo = t.photo_path ? `<div class="tip-photo"><img src="/static/tips/${t.photo_path}"></div>` : '';
  const coords = (t.lat && t.lon) ? `<div class="tip-coords">&#128205; ${t.lat.toFixed(5)}, ${t.lon.toFixed(5)}</div>` : '<div class="tip-coords">Location not geocoded</div>';
  const actions = t.status === 'pending' ? `
    <div class="actions">
      <input class="note-input" id="note-${t.id}" placeholder="Reviewer note (optional)">
      <button class="btn-approve" onclick="act(${t.id},'approve')">&#10003; Approve</button>
      <button class="btn-reject" onclick="act(${t.id},'reject')">&#215; Reject</button>
    </div>` : (t.reviewer_note ? `<div style="font-size:0.75rem;color:#475569">Note: ${t.reviewer_note}</div>` : '');
  return `<div class="tip-card ${t.status}" id="card-${t.id}">
    <div class="tip-meta">#${t.id} &nbsp;·&nbsp; ${dt} &nbsp;·&nbsp; ${badge}</div>
    <div class="tip-location">${t.location_text || '(no location)'}</div>
    ${coords}
    <div class="tip-desc">${t.description || '<em style="color:#475569">No description provided.</em>'}</div>
    ${photo}
    ${actions}
  </div>`;
}
async function act(id, action) {
  const note = document.getElementById('note-' + id)?.value || '';
  const r = await fetch('/api/tips/' + id + '/' + action, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({reviewer_note: note})
  });
  if (r.ok) loadTips();
  else alert('Action failed');
}
loadTips();
</script>
</body>
</html>"""


@app.route("/tip", methods=["GET"])
def tip_form():
    return TIP_FORM_HTML


@app.route("/tip", methods=["POST"])
def tip_submit():
    # Honeypot — bots fill the hidden website field
    if request.form.get("website"):
        return jsonify({"status": "ok"}), 200  # silent drop

    location_text = (request.form.get("location_text") or "").strip()
    if not location_text:
        return jsonify({"error": "location required"}), 400

    description = (request.form.get("description") or "").strip()
    observed_at  = request.form.get("observed_at") or ""

    # Parse observed time or use now
    try:
        ts = datetime.fromisoformat(observed_at).timestamp() if observed_at else time.time()
    except ValueError:
        ts = time.time()

    # Geocode
    lat, lon = None, None
    coords = _geocode_address(location_text)
    if coords:
        lat, lon = coords

    # Photo
    photo_path = None
    if "photo" in request.files:
        photo_path = _save_tip_photo(request.files["photo"])

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO tips (ts, location_text, lat, lon, description, photo_path, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', 'web')",
        (ts, location_text, lat, lon, description, photo_path)
    )
    tip_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"[tip] new submission: {location_text} | geocoded={lat},{lon} | photo={'yes' if photo_path else 'no'}", flush=True)
    threading.Thread(
        target=_notify_new_tip,
        args=(tip_id, location_text, description, photo_path, lat, lon, ts),
        daemon=True
    ).start()
    return jsonify({"status": "ok"}), 200


@app.route("/api/reddit_tips")
def api_reddit_tips():
    cutoff = time.time() - 48 * 3600
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT r.ts, r.post_id, r.subreddit, r.title, r.url, r.author,
               r.keywords, r.tip_status, r.tip_location, r.tip_lat, r.tip_lon,
               r.tip_summary, r.tip_ts_start, r.tip_ts_cleared,
               r.incident_id, i.itype, i.location as inc_location
        FROM reddit_intel r
        LEFT JOIN incidents i ON r.incident_id = i.id
        WHERE r.ts > ? AND r.tip_status IS NOT NULL AND r.tip_status != 'new'
        ORDER BY r.ts DESC LIMIT 50
    """, (cutoff,)).fetchall()
    conn.close()
    keys = ["ts","post_id","subreddit","title","url","author","keywords",
            "tip_status","tip_location","tip_lat","tip_lon","tip_summary",
            "tip_ts_start","tip_ts_cleared","incident_id","incident_type","incident_location"]
    return jsonify([dict(zip(keys, row)) for row in rows])


@app.route("/admin/tips")
def tips_admin():
    return TIPS_ADMIN_HTML


@app.route("/api/tips")
def api_tips():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tips ORDER BY ts DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/tips/<int:tip_id>/approve", methods=["POST"])
def api_tip_approve(tip_id):
    data = request.get_json(silent=True) or {}
    note = (data.get("reviewer_note") or "").strip()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tips SET status='approved', reviewer_note=? WHERE id=?", (note, tip_id))
    conn.commit()
    conn.close()
    print(f"[tip] approved #{tip_id}", flush=True)
    return jsonify({"status": "approved", "id": tip_id})


@app.route("/api/tips/<int:tip_id>/reject", methods=["POST"])
def api_tip_reject(tip_id):
    data = request.get_json(silent=True) or {}
    note = (data.get("reviewer_note") or "").strip()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tips SET status='rejected', reviewer_note=? WHERE id=?", (note, tip_id))
    conn.commit()
    conn.close()
    print(f"[tip] rejected #{tip_id}", flush=True)
    return jsonify({"status": "rejected", "id": tip_id})



# ===========================================================================
# STRIPE + AUTH — Premium membership integration
# ===========================================================================
import secrets as _secrets  # noqa: E402

import stripe as _stripe  # noqa: E402

STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")  # legacy
STRIPE_PLANS = {
    "premium_monthly": {"price_id": "price_1TGmOYIkODTTsH8IeoQPtVXf", "tier": "premium"},
    "premium_annual":  {"price_id": "price_1TGmPjIkODTTsH8IKHU4a5xK", "tier": "premium"},
    "basic_monthly":   {"price_id": "price_1TGmMzIkODTTsH8IipK3zPVr", "tier": "basic"},
    "basic_annual":    {"price_id": "price_1TGmNrIkODTTsH8IpSy0yHNi", "tier": "basic"},
}
STRIPE_PRICE_TO_TIER = {v["price_id"]: v["tier"] for v in STRIPE_PLANS.values()}

if STRIPE_SECRET_KEY:
    _stripe.api_key = STRIPE_SECRET_KEY

NEXTCLOUD_WEB_BASE = os.environ.get("NEXTCLOUD_WEB_BASE", "https://nextcloud.example.com")

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _nc_validate_user(username, password):
    """Validate Nextcloud credentials via OCS API. Returns True/False."""
    import urllib.parse
    url = os.environ.get("NEXTCLOUD_OCS_USER_URL", "https://nextcloud.example.com/ocs/v2.php/cloud/user")
    auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=6) as r:
            data = json.loads(r.read())
            return data.get("ocs", {}).get("meta", {}).get("status") == "ok"
    except Exception:
        return False


def _is_premium(username):
    """Check if username has an active premium record."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT status FROM premium_users WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    return row is not None and row[0] == "active"


def _is_admin(username):
    """Simple admin check — kevin is always admin."""
    return username.lower() in ("kevin", "mrrob")


def _issue_session(username):
    """Create a session token, store in DB, return token string."""
    token = _secrets.token_hex(32)
    now = time.time()
    expires = now + 86400 * 30  # 30 days
    premium = _is_premium(username)
    admin = _is_admin(username)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO sessions (token, username, created_ts, expires_ts, is_admin, is_premium) "
        "VALUES (?,?,?,?,?,?)",
        (token, username, now, expires, int(admin), int(premium))
    )
    conn.commit()
    conn.close()
    return token


def _get_session(request_obj):
    """Extract and validate session token from cookie or Authorization header.
    Returns dict {username, is_admin, is_premium} or None."""
    token = request_obj.cookies.get("bb_session") or \
            request_obj.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, expires_ts, is_admin, is_premium FROM sessions WHERE token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    username, expires_ts, is_admin, is_premium = row
    if time.time() > expires_ts:
        return None
    return {"username": username, "is_admin": bool(is_admin), "is_premium": bool(is_premium)}


def _require_premium(f):
    """Decorator: require valid session with is_premium=True."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        sess = _get_session(request)
        if not sess:
            return jsonify({"error": "login required"}), 401
        if not sess["is_premium"] and not sess["is_admin"]:
            return jsonify({"error": "premium required"}), 403
        return f(*args, **kwargs)
    return decorated


def _require_admin(f):
    """Decorator: require valid session with is_admin=True."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        sess = _get_session(request)
        if not sess or not sess["is_admin"]:
            return jsonify({"error": "admin required"}), 403
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Provisioning helpers
# ---------------------------------------------------------------------------

TALK_ROOMS = {
    "incidents": "89q5fnh5",
    "apd":       "m38srso2",
    "fire-ems":  "ee6si4vj",
    "general":   "iyidr3xy",
}


def _nc_create_user(username, password, email, display_name, tier="premium"):
    """Create Nextcloud user and add to the appropriate membership group."""
    nc_group = "Premium Members" if tier == "premium" else "Basic Members"
    nc_base = os.environ.get("NEXTCLOUD_OCS_BASE", "https://nextcloud.example.com/ocs/v2.php/cloud")
    auth_b64 = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    def _post(url, body):
        data = urllib.parse.urlencode(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"[stripe] NC POST {url} error: {e}", flush=True)
            return {}

    # Create user
    _post(f"{nc_base}/users", {
        "userid": username, "password": password,
        "email": email, "displayName": display_name,
    })
    # Add to group
    _post(f"{nc_base}/users/{username}/groups", {"groupid": nc_group})
    print(f"[stripe] Nextcloud user '{username}' created and added to {nc_group}", flush=True)
    _subscribe_news_feed(username)


def _subscribe_news_feed(username):
    """Subscribe a Nextcloud user to the Battle Buddy RSS feed in Nextcloud News."""
    try:
        result = subprocess.run(
            ["sudo", "-u", "www-data", "php", "/var/www/nextcloud/occ",
             "news:feed:add", username, "https://battlebuddy.news/public/feed.rss",
             "--title", "Battle Buddy Intel Feed"],
            capture_output=True, text=True, timeout=30
        )
        print(f"[provision] News feed subscribed for '{username}': "
              f"{result.stdout.strip() or result.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"[provision] News feed subscribe error for '{username}': {e}", flush=True)



def _plant_user_guide(username):
    """Copy Battle Buddy User Guide.md into a new user's NC files and scan it into the DB."""
    NC_DATA   = "/var/www/nextcloud-data"
    GUIDE_SRC = "/var/www/nextcloud/core/skeleton/Battle Buddy User Guide.md"
    dest_dir  = os.path.join(NC_DATA, username, "files")
    dest_file = os.path.join(dest_dir, "Battle Buddy User Guide.md")
    try:
        if not os.path.isdir(dest_dir):
            print(f"[provision] guide: user dir not ready yet for '{username}', skipping filesystem copy", flush=True)
        else:
            shutil.copy2(GUIDE_SRC, dest_file)
            os.chown(dest_file, pwd.getpwnam("www-data").pw_uid, pwd.getpwnam("www-data").pw_gid)
            result = subprocess.run(
                ["sudo", "-u", "www-data", "php", "/var/www/nextcloud/occ",
                 "files:scan", "--path=/" + username + "/files/Battle Buddy User Guide.md"],
                capture_output=True, text=True, timeout=30
            )
            print(f"[provision] guide planted for '{username}': {result.stdout.strip() or 'ok'}", flush=True)
    except Exception as e:
        print(f"[provision] guide plant error for '{username}': {e}", flush=True)

def _add_to_talk_rooms(username):
    """Add Nextcloud user to all 4 Battle Buddy Talk rooms."""
    auth_b64 = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    for room_name, token in TALK_ROOMS.items():
        url = f"{os.environ.get('NEXTCLOUD_SPREED_API_BASE', 'https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v4')}/room/{token}/participants"
        data = urllib.parse.urlencode({"newParticipant": username}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=8) as r:
                resp = json.loads(r.read())
                print(f"[stripe] Added {username} to Talk room '{room_name}': {resp.get('ocs',{}).get('meta',{}).get('status')}", flush=True)
        except Exception as e:
            print(f"[stripe] Talk room '{room_name}' add error: {e}", flush=True)


def _enroll_subscriptions(username):
    """Add to subscriptions table for DM incident alerts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (username, beat) VALUES (?, 'all')",
        (username,)
    )
    conn.commit()
    conn.close()
    print(f"[stripe] {username} enrolled in subscriptions (beat=all)", flush=True)


def _send_welcome_email(email, username, setup_token, tier="premium"):
    """Send Mailgun welcome email with onboarding instructions, tier-aware."""
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        return

    atak_step = (
        "=== STEP 7 — ATAK FIELD PACKAGE ===\n"
        "Battle Buddy pushes live incident markers directly to WinTAK, ATAK, and iTAK.\n"
        "Reply to this email to request your ATAK data package and connection instructions.\n\n"
    ) if tier == "premium" else ""

    plain = (
        f"Welcome to Battle Buddy Premium, {username}!\n\n"
        f"=== SET YOUR PASSWORD ===\n"
        f"Use the link below to set your password and access your dashboard.\n"
        f"This link expires in 72 hours.\n"
        f"  https://battlebuddy.news/premium/setup?token={setup_token}\n\n"
        f"Your username is: {username}\n\n"
        f"=== STEP 1 — DOWNLOAD THE APPS ===\n"
        f"Install the Nextcloud app on your phone or tablet to access everything on the go.\n"
        f"  Android: https://play.google.com/store/apps/details?id=com.nextcloud.client\n"
        f"  iPhone/iPad: https://apps.apple.com/us/app/nextcloud/id1125420102\n"
        f"  Desktop (Windows/Mac/Linux): https://nextcloud.com/install/#desktop-clients\n\n"
        f"For Talk (incident alert notifications on your phone):\n"
        f"  Android: https://play.google.com/store/apps/details?id=com.nextcloud.talk2\n"
        f"  iPhone/iPad: https://apps.apple.com/us/app/nextcloud-talk/id1296825574\n\n"
        f"=== STEP 2 — INCIDENT ALERTS (TALK) ===\n"
        f"You have been added to all Battle Buddy alert rooms in Nextcloud Talk.\n"
        f"  Web: {NEXTCLOUD_WEB_BASE}/apps/talk\n"
        f"  Rooms you are in:\n"
        f"    - Incidents  (all detected incidents)\n"
        f"    - APD        (Austin Police press releases and scanner intel)\n"
        f"    - Fire & EMS (AFD, Travis County EMS, STAR Flight)\n"
        f"    - General    (Battle Buddy updates and announcements)\n\n"
        f"Enable push notifications in the Talk app so alerts reach you immediately.\n\n"
        f"=== STEP 3 — INTEL NEWS FEED ===\n"
        f"You are auto-subscribed to the Battle Buddy Intel Feed in Nextcloud News.\n"
        f"  Web: {NEXTCLOUD_WEB_BASE}/apps/news\n"
        f"The feed updates continuously with every confirmed incident, APD press release,\n"
        f"and homicide update — one scrollable stream of verified Austin intelligence.\n\n"
        f"You can also subscribe any RSS reader to the feed directly:\n"
        f"  Feed URL: https://battlebuddy.news/public/feed.rss\n\n"
        f"=== STEP 4 — INTEL QUERY ===\n"
        f"Ask questions about Austin radio traffic in plain English. Battle Buddy searches\n"
        f"the full scanner transcript database and returns an AI-synthesized summary.\n"
        f"  URL: https://battlebuddy.news/premium/\n"
        f"  Quota: 5 queries per month (resets the 1st of each month)\n"
        f"  Example: 'How many shootings near North Lamar this week?'\n\n"
        f"=== STEP 5 — COMMUTE MONITOR ===\n"
        f"Save your commute route and get a Talk alert whenever an incident is detected\n"
        f"near your path — with live travel time vs your normal commute.\n"
        f"  Dashboard:   https://battlebuddy.news/premium/\n"
        f"  Commute Map: https://battlebuddy.news/premium/commute\n\n"
        f"=== STEP 6 — LIVE INTELLIGENCE ===\n"
        f"  Live incident map:  https://battlebuddy.news/public\n"
        f"  Live incident feed: https://battlebuddy.news/public/feed\n"
        f"  2026 Homicide map:  https://battlebuddy.news/public/homicides\n\n"
        + atak_step
        + "=== SUPPORT ===\n"
        + "Reply to this email with any questions. We will get back to you promptly.\n\n"
        + "— Battle Buddy Operations\n"
        + "  https://battlebuddy.news"
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#e2e8f0;margin:0;padding:0}}
  .wrap{{max-width:600px;margin:0 auto;padding:32px 20px}}
  .header{{background:#0f1729;border:1px solid #1e3a5f;border-radius:10px;padding:28px 24px;margin-bottom:24px;text-align:center}}
  .header h1{{color:#f8fafc;font-size:1.5rem;margin:0 0 6px}}
  .header p{{color:#64748b;font-size:0.88rem;margin:0}}
  .creds{{background:#0f1729;border:1px solid #f59e0b;border-radius:8px;padding:20px 24px;margin-bottom:24px}}
  .creds h2{{color:#f59e0b;font-size:0.72rem;letter-spacing:2px;text-transform:uppercase;margin:0 0 14px}}
  .cred-row{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #1e293b;font-size:0.88rem}}
  .cred-row:last-child{{border-bottom:none}}
  .cred-label{{color:#64748b}}
  .cred-val{{color:#f8fafc;font-weight:600;font-family:monospace}}
  .section{{background:#0f1729;border:1px solid #1e3a5f;border-radius:8px;padding:20px 24px;margin-bottom:16px}}
  .section-num{{display:inline-block;background:#1e3a5f;color:#60a5fa;font-size:0.7rem;font-weight:700;padding:2px 8px;border-radius:3px;letter-spacing:1px;margin-bottom:10px}}
  .section h2{{color:#f8fafc;font-size:1rem;margin:0 0 10px}}
  .section p{{color:#94a3b8;font-size:0.84rem;line-height:1.6;margin:0 0 10px}}
  .section p:last-child{{margin-bottom:0}}
  .app-links{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}}
  .app-link{{display:inline-block;background:#1e3a5f;color:#60a5fa;text-decoration:none;font-size:0.78rem;padding:6px 14px;border-radius:5px;border:1px solid #2d4a7a}}
  .rooms{{list-style:none;margin:10px 0 0;padding:0}}
  .rooms li{{font-size:0.83rem;color:#94a3b8;padding:5px 0;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:8px}}
  .rooms li:last-child{{border-bottom:none}}
  .dot{{width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0}}
  .feed-url{{background:#060c18;border:1px solid #1e3a5f;border-radius:5px;padding:10px 14px;font-family:monospace;font-size:0.8rem;color:#60a5fa;word-break:break-all;margin-top:10px}}
  .cta{{display:inline-block;background:#f59e0b;color:#0a0a0f;font-weight:700;text-decoration:none;padding:11px 24px;border-radius:6px;font-size:0.9rem;margin-top:12px}}
  .links{{display:flex;flex-direction:column;gap:7px;margin-top:10px}}
  .links a{{color:#60a5fa;font-size:0.84rem;text-decoration:none}}
  .footer{{text-align:center;color:#334155;font-size:0.75rem;margin-top:24px;line-height:1.6}}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <h1>Welcome to Battle Buddy Premium</h1>
    <p>Austin's real-time public safety intelligence platform</p>
  </div>

  <div class="creds">
    <h2>Activate Your Account</h2>
    <p style="color:#94a3b8;font-size:0.9rem;margin:0 0 16px">Click below to set your password and access your dashboard. This link expires in 72 hours.</p>
    <a class="cta" href="https://battlebuddy.news/premium/setup?token={setup_token}">Set Your Password →</a>
    <p style="color:#64748b;font-size:0.78rem;margin:14px 0 0">Your username is: <strong style="color:#cbd5e1">{username}</strong></p>
  </div>

  <div class="section">
    <span class="section-num">STEP 1</span>
    <h2>Download the Apps</h2>
    <p>Install Nextcloud on your phone or tablet for access to Talk alerts, the News feed, files, and calendar — all in one place.</p>
    <p><strong style="color:#cbd5e1">Nextcloud (main app)</strong></p>
    <div class="app-links">
      <a class="app-link" href="https://play.google.com/store/apps/details?id=com.nextcloud.client">Android</a>
      <a class="app-link" href="https://apps.apple.com/us/app/nextcloud/id1125420102">iPhone / iPad</a>
      <a class="app-link" href="https://nextcloud.com/install/#desktop-clients">Desktop (Win/Mac/Linux)</a>
    </div>
    <p style="margin-top:14px"><strong style="color:#cbd5e1">Nextcloud Talk (for push alert notifications)</strong></p>
    <div class="app-links">
      <a class="app-link" href="https://play.google.com/store/apps/details?id=com.nextcloud.talk2">Android</a>
      <a class="app-link" href="https://apps.apple.com/us/app/nextcloud-talk/id1296825574">iPhone / iPad</a>
    </div>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">After installing, tap <em>Add account</em> and enter the server URL above with your username and password.</p>
  </div>

  <div class="section">
    <span class="section-num">STEP 2</span>
    <h2>Incident Alerts — Talk</h2>
    <p>You are enrolled in all four Battle Buddy alert rooms. Incidents are posted the moment they are detected — before any news broadcast exists.</p>
    <ul class="rooms">
      <li><span class="dot"></span><strong style="color:#cbd5e1">Incidents</strong> &nbsp;— all detected incidents across agencies</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">APD</strong> &nbsp;— Austin Police press releases and scanner intel</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">Fire &amp; EMS</strong> &nbsp;— AFD, Travis County EMS, STAR Flight</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">General</strong> &nbsp;— Battle Buddy updates and announcements</li>
    </ul>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">Enable push notifications in the Talk app so critical incident alerts wake your phone immediately.</p>
    <div class="app-links" style="margin-top:10px">
      <a class="app-link" href="{NEXTCLOUD_WEB_BASE}/apps/talk">Open Talk on Web</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 3</span>
    <h2>Intel News Feed</h2>
    <p>You are auto-subscribed to the <strong style="color:#cbd5e1">Battle Buddy Intel Feed</strong> in Nextcloud News. Open it from the News app on the web or inside the Nextcloud mobile app — it updates continuously with every confirmed incident, APD press release, and homicide update.</p>
    <div class="app-links">
      <a class="app-link" href="{NEXTCLOUD_WEB_BASE}/apps/news">Open News on Web</a>
    </div>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">You can also subscribe any RSS reader (Feedly, Reeder, etc.) to the feed directly:</p>
    <div class="feed-url">https://battlebuddy.news/public/feed.rss</div>
  </div>

  <div class="section">
    <span class="section-num">STEP 4</span>
    <h2>Intel Query — AI Search</h2>
    <p>Ask any question about Austin radio traffic in plain English. Battle Buddy searches the full scanner transcript and press release database and returns an AI-written intelligence summary.</p>
    <p style="font-size:0.8rem;color:#64748b"><em>Examples: "How many shootings near North Lamar this week?" &nbsp;·&nbsp; "Any AFD structure fires in East Austin this month?"</em></p>
    <p style="font-size:0.8rem;color:#64748b">Quota: <strong style="color:#cbd5e1">5 queries per month</strong>, resets on the 1st.</p>
    <div class="app-links">
      <a class="app-link" href="https://battlebuddy.news/premium/">Open Intel Query</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 5</span>
    <h2>🚗 Commute Monitor</h2>
    <p>Save your daily commute route and Battle Buddy will alert you via Talk whenever an active incident is detected near your path — with live travel time vs your normal commute.</p>
    <p style="font-size:0.8rem;color:#64748b">Open your premium dashboard, find the Commute Monitor card, and enter your origin and destination (include city and state — e.g. <em>Slaughter Ln &amp; I-35, Austin TX</em>).</p>
    <div class="app-links">
      <a class="app-link" href="https://battlebuddy.news/premium/commute">Open Commute Map</a>
      <a class="app-link" href="https://battlebuddy.news/premium/">Premium Dashboard</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 6</span>
    <h2>Live Intelligence — Web</h2>
    <p>The public intelligence portal is available to you at any time from any browser — no login required.</p>
    <div class="links">
      <a href="https://battlebuddy.news/public">battlebuddy.news/public &nbsp;— Live incident map</a>
      <a href="https://battlebuddy.news/public/feed">battlebuddy.news/public/feed &nbsp;— Live incident feed</a>
      <a href="https://battlebuddy.news/public/homicides">battlebuddy.news/public/homicides &nbsp;— 2026 Austin Homicide Map</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 7</span>
    <h2>ATAK Field Package</h2>
    <p>Battle Buddy pushes live incident markers directly to WinTAK, ATAK (Android), and iTAK as Cursor-on-Target (CoT) markers — appearing on your tactical map the moment an incident is confirmed.</p>
    <p>Reply to this email to request your ATAK data package (.zip with certs and connection profile) and setup instructions for your device.</p>
  </div>

  <div class="footer">
    <p>Questions? Reply to this email — we respond promptly.</p>
    <p style="margin-top:6px"><a href="https://battlebuddy.news" style="color:#3b82f6">battlebuddy.news</a> &nbsp;·&nbsp; Battle Buddy Operations</p>
  </div>

</div>
</body>
</html>"""

    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    auth_b64 = base64.b64encode(f"api:{MAILGUN_API_KEY}".encode()).decode()
    data = urllib.parse.urlencode({
        "from": MAILGUN_FROM,
        "to": email,
        "subject": "Welcome to Battle Buddy Premium — Getting Started",
        "text": plain,
        "html": html,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {auth_b64}",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[stripe] Welcome email sent to {email}: {r.status}", flush=True)
    except Exception as e:
        print(f"[stripe] Welcome email error: {e}", flush=True)


def _provision_premium_user(session_obj):
    """Full provisioning flow after successful Stripe checkout."""
    customer_email = (session_obj.get("customer_details") or {}).get("email") or                      session_obj.get("customer_email") or ""
    customer_id    = session_obj.get("customer", "")
    sub_id         = session_obj.get("subscription", "")
    metadata       = session_obj.get("metadata") or {}

    username     = metadata.get("username", "").strip().lower()
    nc_password  = metadata.get("nc_password", "").strip()
    display_name = metadata.get("display_name", "") or username
    tier         = metadata.get("tier", "premium")

    if not username or not customer_email:
        print("[stripe] provision skipped — missing username or email in session", flush=True)
        return

    # 1. Nextcloud user
    _nc_create_user(username, nc_password, customer_email, display_name, tier)

    # 2. Talk rooms
    _add_to_talk_rooms(username)

    # 3. Subscriptions
    _enroll_subscriptions(username)

    # 3b. Plant user guide in NC files
    _plant_user_guide(username)

    # 4. premium_users table
    setup_token   = _secrets.token_urlsafe(32)
    setup_expires = int(time.time()) + 72 * 3600  # 72-hour window
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO premium_users "
        "(username, email, stripe_customer_id, stripe_subscription_id, status, created_ts, setup_token, setup_token_expires) "
        "VALUES (?,?,?,?,'active',?,?,?)",
        (username, customer_email, customer_id, sub_id, time.time(), setup_token, setup_expires)
    )
    conn.commit()
    conn.close()
    print(f"[stripe] premium_users record created for '{username}'", flush=True)

    # 5. Welcome email
    _send_welcome_email(customer_email, username, setup_token, tier)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    if not _nc_validate_user(username, password):
        return jsonify({"error": "invalid credentials"}), 401
    token = _issue_session(username)
    sess = _get_session_by_token(token)
    from flask import make_response
    resp = make_response(jsonify({
        "token": token,
        "username": username,
        "is_premium": sess["is_premium"],
        "is_admin": sess["is_admin"],
    }))
    resp.set_cookie("bb_session", token, max_age=86400*30, httponly=True, samesite="Lax")
    return resp


def _get_session_by_token(token):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, expires_ts, is_admin, is_premium FROM sessions WHERE token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    username, expires_ts, is_admin, is_premium = row
    return {"username": username, "is_admin": bool(is_admin), "is_premium": bool(is_premium)}


@app.route("/api/logout", methods=["POST"])
def api_logout():
    sess = _get_session(request)
    if sess:
        token = request.cookies.get("bb_session") or \
                request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    from flask import make_response
    resp = make_response(jsonify({"status": "ok"}))
    resp.set_cookie("bb_session", "", expires=0)
    return resp


@app.route("/api/me")
def api_me():
    sess = _get_session(request)
    if not sess:
        return jsonify({"logged_in": False}), 200
    return jsonify({"logged_in": True, **sess})


# ---------------------------------------------------------------------------
# Stripe checkout + webhook
# ---------------------------------------------------------------------------

@app.route("/api/stripe/create_checkout", methods=["POST"])
def api_stripe_create_checkout():
    """Create a Stripe Checkout Session. Client sends username, display_name, plan."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "payments not configured"}), 503
    data = request.get_json(silent=True) or {}
    username     = (data.get("username") or "").strip().lower()
    display_name = (data.get("display_name") or username).strip()
    plan         = (data.get("plan") or "premium_monthly").strip()
    if not username:
        return jsonify({"error": "username required"}), 400
    if plan not in STRIPE_PLANS:
        return jsonify({"error": "invalid plan"}), 400

    plan_info   = STRIPE_PLANS[plan]
    nc_password = _secrets.token_urlsafe(12)

    try:
        session = _stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan_info["price_id"], "quantity": 1}],
            subscription_data={"trial_period_days": 7},
            success_url="https://battlebuddy.news/premium/welcome?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://battlebuddy.news/premium/",
            metadata={
                "username": username,
                "display_name": display_name,
                "nc_password": nc_password,
                "tier": plan_info["tier"],
            },
        )
        return jsonify({"checkout_url": session.url})
    except Exception as e:
        print(f"[stripe] create_checkout error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe sends signed events here. Verify signature, then provision."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = _stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except _stripe.error.SignatureVerificationError as e:
        print(f"[stripe] webhook signature invalid: {e}", flush=True)
        return jsonify({"error": "invalid signature"}), 400
    except Exception as e:
        print(f"[stripe] webhook parse error: {e}", flush=True)
        return jsonify({"error": "bad payload"}), 400

    event_type = event["type"]
    print(f"[stripe] webhook received: {event_type}", flush=True)

    if event_type == "checkout.session.completed":
        # Parse session from raw payload — avoids SDK v15 StripeObject attribute issues
        import json as _json
        session_data = _json.loads(payload)["data"]["object"]
        threading.Thread(
            target=_provision_premium_user,
            args=(session_data,),
            daemon=True
        ).start()

    elif event_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer", "")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE premium_users SET status='cancelled' WHERE stripe_customer_id=?",
            (customer_id,)
        )
        conn.commit()
        conn.close()
        print(f"[stripe] subscription cancelled for customer {customer_id}", flush=True)

    return jsonify({"status": "ok"})



# ---------------------------------------------------------------------------
# Commute travel time — premium feature
# ---------------------------------------------------------------------------

_COMMUTE_ALERT_ITYPES = {
    "SHOOTING", "OFFICER DOWN", "PURSUIT", "STRUCTURE FIRE",
    "HAZMAT", "WEAPONS", "CRASH/COLLISION", "STABBING", "MASS CASUALTY",
}
_COMMUTE_CORRIDOR_MILES = 3.0  # incident must be within this distance of route line





@app.route("/api/commute/save", methods=["POST"])
@_require_premium
def api_commute_save():
    sess = _get_session(request)
    username = sess["username"]
    data = request.get_json(force=True) or {}
    origin = (data.get("origin") or "").strip()
    dest   = (data.get("destination") or "").strip()
    if not origin or not dest:
        return jsonify({"error": "origin and destination required"}), 400

    # Single Routes API call: geocodes addresses AND returns baseline travel time
    info = _commute_route_info(origin, dest, traffic=False)
    if not info:
        return jsonify({"error": "Could not resolve addresses or fetch travel time. Check that both addresses are valid US locations."}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE premium_users SET commute_origin=?, commute_origin_lat=?, commute_origin_lon=?, "
        "commute_dest=?, commute_dest_lat=?, commute_dest_lon=?, commute_baseline_mins=? "
        "WHERE username=?",
        (origin, info["origin_lat"], info["origin_lon"],
         dest,   info["dest_lat"],   info["dest_lon"],
         info["mins"], username)
    )
    conn.commit()
    conn.close()
    print(f"[commute] {username} saved route: {origin} -> {dest}, baseline {info['mins']}min", flush=True)
    return jsonify({"ok": True, "baseline_mins": info["mins"], "origin": origin, "destination": dest})


@app.route("/api/commute/time", methods=["GET"])
@_require_premium
def api_commute_time():
    sess = _get_session(request)
    username = sess["username"]
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT commute_origin, commute_origin_lat, commute_origin_lon, "
        "commute_dest, commute_dest_lat, commute_dest_lon, commute_baseline_mins "
        "FROM premium_users WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    if not row or row[1] is None:
        return jsonify({"configured": False})
    origin, olat, olon, dest, dlat, dlon, baseline = row
    live = _routes_travel_time(olat, olon, dlat, dlon, traffic=True)
    if live is None:
        return jsonify({"error": "Routes API unavailable"}), 503
    delta = live - baseline if baseline else None
    return jsonify({
        "configured": True,
        "origin": origin,
        "destination": dest,
        "live_mins": live,
        "baseline_mins": baseline,
        "delta_mins": delta,
    })




@app.route("/api/commute/polyline", methods=["GET"])
def api_commute_polyline():
    """Return encoded polyline for user's commute route. Auth via session or share token."""
    import json as _json
    token = request.args.get("token")
    if token:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT commute_origin, commute_origin_lat, commute_origin_lon, "
            "commute_dest, commute_dest_lat, commute_dest_lon "
            "FROM premium_users WHERE commute_share_token=?", (token,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "invalid token"}), 403
        origin, olat, olon, dest, dlat, dlon = row
    else:
        sess = _get_session(request)
        if not sess:
            return jsonify({"error": "login required"}), 401
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT commute_origin, commute_origin_lat, commute_origin_lon, "
            "commute_dest, commute_dest_lat, commute_dest_lon "
            "FROM premium_users WHERE username=?", (sess["username"],)
        ).fetchone()
        conn.close()
        if not row or row[1] is None:
            return jsonify({"configured": False})
        origin, olat, olon, dest, dlat, dlon = row

    body = _json.dumps({
        "origin":      {"location": {"latLng": {"latitude": olat, "longitude": olon}}},
        "destination": {"location": {"latLng": {"latitude": dlat, "longitude": dlon}}},
        "travelMode":  "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }).encode()
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_ROUTES_KEY,
            "X-Goog-FieldMask": "routes.polyline.encodedPolyline",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10).read().decode()
        data = _json.loads(resp)
        poly = data.get("routes", [{}])[0].get("polyline", {}).get("encodedPolyline", "")
        return jsonify({"configured": True, "polyline": poly, "origin": origin, "destination": dest})
    except Exception as e:
        print(f"[commute] polyline error: {e}", flush=True)
        return jsonify({"error": "Routes API unavailable"}), 503

@app.route("/api/commute/incidents", methods=["GET"])
def api_commute_incidents():
    """Return active incidents near the user's commute route. Auth via session or share token."""
    token = request.args.get("token")
    if token:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT username, commute_origin_lat, commute_origin_lon, "
            "commute_dest_lat, commute_dest_lon FROM premium_users WHERE commute_share_token=?",
            (token,)
        ).fetchone()
        conn.close()
        if not row:
            return jsonify({"error": "invalid token"}), 403
        _, olat, olon, dlat, dlon = row
    else:
        sess = _get_session(request)
        if not sess:
            return jsonify({"error": "login required"}), 401
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT commute_origin_lat, commute_origin_lon, commute_dest_lat, commute_dest_lon "
            "FROM premium_users WHERE username=?", (sess["username"],)
        ).fetchone()
        conn.close()
        if not row or row[0] is None:
            return jsonify({"configured": False, "incidents": []})
        olat, olon, dlat, dlon = row

    cutoff = time.time() - 3600 * 4
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, itype, location, lat, lon, description, ts_start FROM incidents "
        "WHERE status='active' AND lat IS NOT NULL AND ts_start > ? AND is_test=0",
        (cutoff,)
    ).fetchall()
    conn.close()

    nearby = []
    for inc_id, itype, location, ilat, ilon, desc, ts in rows:
        dist = _point_to_segment_distance_miles(ilat, ilon, olat, olon, dlat, dlon)
        if dist <= 5.0:
            nearby.append({
                "id": inc_id,
                "itype": itype,
                "location": location,
                "lat": ilat,
                "lon": ilon,
                "description": (desc or "")[:200],
                "ts": ts,
                "dist_miles": round(dist, 1),
            })
    nearby.sort(key=lambda x: x["dist_miles"])
    return jsonify({"configured": True, "incidents": nearby})


@app.route("/api/commute/share_token", methods=["POST"])
@_require_premium
def api_commute_share_token():
    """Generate or return existing share token for the user's commute map."""
    import secrets as _secrets
    sess = _get_session(request)
    username = sess["username"]
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT commute_share_token FROM premium_users WHERE username=?", (username,)
    ).fetchone()
    token = row[0] if row and row[0] else None
    if not token:
        token = _secrets.token_urlsafe(24)
        conn.execute(
            "UPDATE premium_users SET commute_share_token=? WHERE username=?",
            (token, username)
        )
        conn.commit()
    conn.close()
    share_url = f"https://battlebuddy.news/premium/commute?token={token}"
    return jsonify({"token": token, "share_url": share_url})


@app.route("/premium/commute")
def premium_commute():
    """Live commute map — premium-gated or token-gated."""
    token = request.args.get("token")
    if not token:
        sess = _get_session(request)
        if not sess or not sess.get("is_premium"):
            return redirect("/premium/")
    maps_key = GOOGLE_MAPS_JS_KEY
    html = f"""<!DOCTYPE html>
<html><head>
<title>Commute Map — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:sans-serif;background:#0a0f1e;color:#eee;height:100vh;display:flex;flex-direction:column}}
  #header{{background:#111827;border-bottom:1px solid #1e3a5f;padding:12px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}}
  #header h1{{font-size:1rem;color:#f8fafc;font-weight:700;letter-spacing:1px}}
  #travel-info{{display:flex;align-items:center;gap:20px}}
  #travel-mins{{font-size:2rem;font-weight:800;color:#f90;line-height:1}}
  #travel-delta{{font-size:0.8rem;color:#94a3b8}}
  #travel-route{{font-size:0.7rem;color:#475569;margin-top:2px}}
  #incident-bar{{background:#111827;border-bottom:1px solid #1e3a5f;padding:8px 20px;font-size:0.78rem;color:#94a3b8;flex-shrink:0;min-height:34px}}
  #incident-bar span.inc-badge{{display:inline-block;background:#7f1d1d;color:#fca5a5;border-radius:4px;padding:2px 8px;margin-right:6px;font-size:0.72rem;cursor:pointer}}
  #map{{flex:1}}
  #share-btn{{background:#1e3a5f;color:#93c5fd;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.75rem;white-space:nowrap}}
  #share-btn:hover{{background:#2d4f7c}}
  #updated{{font-size:0.65rem;color:#334155;margin-top:2px}}
  .no-route{{display:flex;align-items:center;justify-content:center;height:100%;color:#475569;font-size:1rem}}
</style>
</head><body>
<div id="header">
  <div>
    <h1>🚗 BATTLE BUDDY — COMMUTE MONITOR</h1>
    <div id="updated">Loading...</div>
  </div>
  <div id="travel-info">
    <div>
      <div id="travel-mins">--</div>
      <div id="travel-delta"></div>
      <div id="travel-route"></div>
    </div>
    <button id="share-btn" onclick="getShareLink()">Share Link</button>
  </div>
</div>
<div id="incident-bar">Checking for incidents near your route...</div>
<div id="map"></div>

<script>
const TOKEN = "{token or ''}";
const MAPS_KEY = "{maps_key}";
let map, directionsRenderer, incidentMarkers = [];

function initMap() {{
  map = new google.maps.Map(document.getElementById('map'), {{
    zoom: 12,
    center: {{lat: 30.267, lng: -97.743}},
    mapTypeId: 'roadmap',
    styles: [
      {{elementType:'geometry',stylers:[{{color:'#1d2c4d'}}]}},
      {{elementType:'labels.text.fill',stylers:[{{color:'#8ec3b9'}}]}},
      {{elementType:'labels.text.stroke',stylers:[{{color:'#1a3646'}}]}},
      {{featureType:'road',elementType:'geometry',stylers:[{{color:'#304a7d'}}]}},
      {{featureType:'road',elementType:'geometry.stroke',stylers:[{{color:'#255763'}}]}},
      {{featureType:'road.highway',elementType:'geometry',stylers:[{{color:'#2c6675'}}]}},
      {{featureType:'water',elementType:'geometry',stylers:[{{color:'#0e1626'}}]}},
    ]
  }});
  // directionsRenderer removed — using polyline decode instead
  refresh();
  setInterval(refresh, 300000);
}}

async function refresh() {{
  await Promise.all([loadTravel(), loadIncidents()]);
  document.getElementById('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
}}

async function loadTravel() {{
  try {{
    const url = TOKEN ? `/api/commute/time` : `/api/commute/time`;
    const r = await fetch(url, {{credentials:'include'}});
    const d = await r.json();
    if (!d.configured) {{
      document.getElementById('travel-mins').textContent = '--';
      document.getElementById('travel-delta').textContent = 'No route configured';
      return;
    }}
    document.getElementById('travel-mins').textContent = d.live_mins + ' min';
    const delta = d.delta_mins;
    const deltaEl = document.getElementById('travel-delta');
    if (delta > 2)        deltaEl.style.color = '#f87171', deltaEl.textContent = '+' + delta + ' min vs normal';
    else if (delta < -2)  deltaEl.style.color = '#4ade80', deltaEl.textContent = delta + ' min vs normal';
    else                  deltaEl.style.color = '#94a3b8', deltaEl.textContent = 'Normal traffic';
    document.getElementById('travel-route').textContent = d.origin + ' → ' + d.destination;

    // Draw route via encoded polyline from server (avoids legacy Directions API)
    const purl = TOKEN ? `/api/commute/polyline?token=${{TOKEN}}` : `/api/commute/polyline`;
    fetch(purl, {{credentials:'include'}}).then(r => r.json()).then(pd => {{
      if (pd.polyline && window.google) {{
        const path = google.maps.geometry.encoding.decodePath(pd.polyline);
        new google.maps.Polyline({{
          path,
          map,
          strokeColor: '#f90',
          strokeWeight: 5,
          strokeOpacity: 0.85,
        }});
        // Fit map to route bounds
        const bounds = new google.maps.LatLngBounds();
        path.forEach(p => bounds.extend(p));
        map.fitBounds(bounds, {{top:40, right:40, bottom:40, left:40}});
      }}
    }}).catch(e => console.warn('polyline fetch failed', e));
  }} catch(e) {{
    document.getElementById('travel-delta').textContent = 'Travel data unavailable';
  }}
}}

async function loadIncidents() {{
  try {{
    const url = TOKEN ? `/api/commute/incidents?token=${{TOKEN}}` : `/api/commute/incidents`;
    const r = await fetch(url, {{credentials:'include'}});
    const d = await r.json();

    // Clear old markers
    incidentMarkers.forEach(m => m.setMap(null));
    incidentMarkers = [];

    if (!d.incidents || d.incidents.length === 0) {{
      document.getElementById('incident-bar').textContent = '✓ No active incidents near your route';
      return;
    }}

    const bar = d.incidents.map(inc =>
      `<span class="inc-badge" title="${{inc.description}}" onclick="focusIncident(${{inc.lat}},${{inc.lon}})">`+
      `${{inc.itype}} — ${{inc.dist_miles}}mi</span>`
    ).join('');
    document.getElementById('incident-bar').innerHTML = `⚠️ ${{d.incidents.length}} incident(s) near route: ` + bar;

    d.incidents.forEach(inc => {{
      const marker = new google.maps.Marker({{
        position: {{lat: inc.lat, lng: inc.lon}},
        map: map,
        icon: {{
          path: google.maps.SymbolPath.CIRCLE,
          scale: 10,
          fillColor: '#ef4444',
          fillOpacity: 0.9,
          strokeColor: '#fff',
          strokeWeight: 2,
        }},
        title: inc.itype,
      }});
      const info = new google.maps.InfoWindow({{
        content: `<div style="color:#111;max-width:220px"><strong>${{inc.itype}}</strong><br>${{inc.location || ''}}<br><small>${{inc.description}}</small></div>`
      }});
      marker.addListener('click', () => info.open(map, marker));
      incidentMarkers.push(marker);
    }});
  }} catch(e) {{
    document.getElementById('incident-bar').textContent = 'Incident data unavailable';
  }}
}}

function focusIncident(lat, lon) {{
  map.panTo({{lat, lng: lon}});
  map.setZoom(14);
}}

async function getShareLink() {{
  try {{
    const r = await fetch('/api/commute/share_token', {{method:'POST',credentials:'include'}});
    const d = await r.json();
    if (d.share_url) {{
      navigator.clipboard.writeText(d.share_url).then(() => {{
        document.getElementById('share-btn').textContent = 'Copied!';
        setTimeout(() => document.getElementById('share-btn').textContent = 'Share Link', 2000);
      }});
    }}
  }} catch(e) {{
    alert('Could not generate share link.');
  }}
}}
</script>
<script src="https://maps.googleapis.com/maps/api/js?key={maps_key}&libraries=geometry&callback=initMap" async defer></script>
</body></html>"""
    return html

# ---------------------------------------------------------------------------
# Intel Query — premium feature (5/month)
# ---------------------------------------------------------------------------

@app.route("/api/intel", methods=["POST"])
@_require_premium
def api_intel_query():
    sess = _get_session(request)
    username = sess["username"]

    # Check quota
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT intel_queries_used, intel_quota FROM premium_users WHERE username=?",
        (username,)
    ).fetchone()
    conn.close()

    if row:
        used, quota = row
        if used >= quota:
            return jsonify({"error": f"Monthly intel query limit reached ({quota}/month). Resets on the 1st."}), 429

    data = request.get_json(silent=True) or {}
    query_text = (data.get("query") or "").strip()
    if not query_text:
        return jsonify({"error": "query required"}), 400

    # Keyword extraction — simple approach, good enough for now
    keywords = re.findall(r'\b[A-Za-z]{4,}\b', query_text.lower())
    stop = {"what", "when", "where", "with", "have", "been", "that", "this",
            "from", "about", "were", "there", "they", "them", "their", "then"}
    keywords = [k for k in keywords if k not in stop][:8]

    # Build DB query — search transcripts and talkgroup names
    conn = sqlite3.connect(DB_PATH)
    results = []
    tgids_hit = set()

    if keywords:
        like_clauses = " OR ".join(["LOWER(transcript) LIKE ?" for _ in keywords])
        params = [f"%{k}%" for k in keywords]
        rows = conn.execute(
            f"SELECT ts, tgid, tag, transcript "
            f"FROM calls WHERE tgid != 0 AND ({like_clauses}) ORDER BY ts DESC LIMIT 50",
            params
        ).fetchall()
        for ts, tgid, tgname, transcript in rows:
            tgids_hit.add(str(tgid))
            results.append({
                "ts": ts,
                "tgid": tgid,
                "talkgroup": tgname or str(tgid),
                "transcript": transcript,
            })
    conn.close()

    if not results:
        return jsonify({
            "query": query_text,
            "summary": "No matching radio traffic found in the database for that query.",
            "calls_hit": 0,
            "tgids": [],
        })

    # Claude Haiku synthesis
    summary = f"Found {len(results)} matching call(s) across talkgroups: {', '.join(tgids_hit)}."
    if ANTHROPIC_ENABLED and results:
        excerpts = "\n".join(
            f"[{datetime.fromtimestamp(r['ts']).strftime('%Y-%m-%d %H:%M')}] "
            f"{r['talkgroup']}: {r['transcript'][:300]}"
            for r in results[:20]
        )
        prompt = (
            f"You are an intelligence analyst for Austin, TX first responder monitoring. "
            f"A user asked: \"{query_text}\"\n\n"
            f"Here are matching radio transcripts:\n{excerpts}\n\n"
            f"Write a concise intelligence summary (3-6 sentences) answering the user's question "
            f"based only on the transcripts above. Note dates, locations, and patterns if present. "
            f"Do not speculate beyond what the transcripts say."
        )
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                temperature=0.2,
                system="You are an intelligence analyst monitoring Austin, TX public safety radio traffic. You summarize publicly broadcast scanner data for journalists, researchers, and public safety professionals.",
                messages=[{"role": "user", "content": prompt}],
            )
            summary = response.content[0].text.strip()
        except Exception as e:
            print(f"[intel] Anthropic error: {e}", flush=True)

    # Increment usage counter and log
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE premium_users SET intel_queries_used = intel_queries_used + 1 WHERE username=?",
        (username,)
    )
    conn.execute(
        "INSERT INTO intel_queries (username, ts, query, result, tgids_hit, calls_hit) "
        "VALUES (?,?,?,?,?,?)",
        (username, time.time(), query_text, summary, ",".join(tgids_hit), len(results))
    )
    conn.commit()
    conn.close()

    return jsonify({
        "query": query_text,
        "summary": summary,
        "calls_hit": len(results),
        "tgids": list(tgids_hit),
        "quota_remaining": (quota - used - 1) if row else None,
    })


# ---------------------------------------------------------------------------
# Premium welcome page + subscription status
# ---------------------------------------------------------------------------

@app.route("/premium/welcome")
def premium_welcome():
    sess = _get_session(request)
    if sess and sess.get("is_premium"):
        from flask import redirect
        return redirect("/premium/")
    username = sess["username"] if sess else ""
    html = f"""<!DOCTYPE html>
<html><head><title>Welcome — Battle Buddy Premium</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:sans-serif;background:#111;color:#eee;max-width:600px;margin:60px auto;padding:20px}}
  h1{{color:#f90}}
  .box{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:24px;margin:20px 0}}
  a{{color:#f90}}
  input{{display:block;width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:10px;font-size:15px;margin-bottom:10px;box-sizing:border-box}}
  button{{background:#f90;color:#111;border:none;padding:12px 24px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:15px;width:100%}}
  button:hover{{background:#ffa820}}
  #login-err{{color:#f44;font-size:14px;margin-top:8px;display:none}}
</style></head><body>
<h1>Welcome to Battle Buddy Premium</h1>
<div class="box">
  <p>Payment confirmed. <strong>Check your email</strong> for a link to set your password.</p>
  <p>Once you've set your password, sign in here:</p>
  <h3>Sign In</h3>
  <input type="text" id="l-user" placeholder="Username" value="{username}" autocomplete="off">
  <input type="password" id="l-pass" placeholder="Password from your welcome email">
  <button onclick="doLogin()">Sign In to Dashboard</button>
  <div id="login-err"></div>
</div>
<div class="box">
  <h3>What's included:</h3>
  <ul>
    <li>Live incident alerts via Nextcloud Talk</li>
    <li>Priority notifications for high-severity events</li>
    <li>Intel Query — natural language search of Austin radio traffic (5/month)</li>
    <li>ATAK data package for field situational awareness</li>
    <li>Intel News Feed in Nextcloud News — auto-subscribed, live incident and press release updates</li>
  </ul>
  <p><a href="{NEXTCLOUD_WEB_BASE}" target="_blank">Open Nextcloud →</a></p>
</div>
<script>
async function doLogin() {{
  const username = document.getElementById('l-user').value.trim().toLowerCase();
  const password = document.getElementById('l-pass').value;
  const err = document.getElementById('login-err');
  err.style.display = 'none';
  if (!username || !password) {{ err.textContent = 'Enter username and password.'; err.style.display='block'; return; }}
  try {{
    const r = await fetch('/api/login', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{username, password}})
    }});
    if (r.ok) {{
      window.location.href = '/premium/';
    }} else {{
      const d = await r.json();
      err.textContent = d.error || 'Login failed. Check your credentials and try again.';
      err.style.display = 'block';
    }}
  }} catch(e) {{
    err.textContent = 'Network error. Try again.';
    err.style.display = 'block';
  }}
}}
</script>
</body></html>"""
    return html
@app.route("/premium/setup")
def premium_setup():
    token = request.args.get("token", "").strip()
    error = ""
    if not token:
        error = "Missing or invalid setup link."
    else:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT username, setup_token_expires FROM premium_users WHERE setup_token=?",
            (token,)
        ).fetchone()
        conn.close()
        if not row:
            error = "This setup link is invalid or has already been used."
        elif int(time.time()) > row[1]:
            error = "This setup link has expired. Contact support to get a new one."

    if error:
        return f"""<!DOCTYPE html>
<html><head><title>Setup — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:sans-serif;background:#111;color:#eee;max-width:500px;margin:80px auto;padding:20px}}
h1{{color:#f90}} .box{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:24px}} a{{color:#f90}}</style></head><body>
<h1>Battle Buddy Premium</h1><div class="box"><p style="color:#f44">{error}</p>
<p><a href="mailto:ops@mail.battlebuddy.news">Contact support</a></p></div></body></html>""", 400

    username = row[0]
    return f"""<!DOCTYPE html>
<html><head><title>Set Password — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:sans-serif;background:#111;color:#eee;max-width:500px;margin:80px auto;padding:20px}}
  h1{{color:#f90}}
  .box{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:24px;margin:20px 0}}
  input{{display:block;width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;
         padding:10px;font-size:15px;margin-bottom:12px;box-sizing:border-box}}
  button{{background:#f90;color:#111;border:none;padding:12px 24px;border-radius:6px;
          font-weight:bold;cursor:pointer;font-size:15px;width:100%}}
  button:hover{{background:#ffa820}}
  #err{{color:#f44;font-size:14px;margin-top:8px;display:none}}
</style></head><body>
<h1>Battle Buddy Premium</h1>
<div class="box">
  <p>Welcome, <strong>{username}</strong>. Set a password to access your dashboard.</p>
  <input type="password" id="pw1" placeholder="New password (min 8 characters)">
  <input type="password" id="pw2" placeholder="Confirm password">
  <button onclick="doSetup()">Set Password &amp; Sign In</button>
  <div id="err"></div>
</div>
<script>
async function doSetup() {{
  const pw1 = document.getElementById('pw1').value;
  const pw2 = document.getElementById('pw2').value;
  const err = document.getElementById('err');
  err.style.display = 'none';
  if (pw1.length < 8) {{ err.textContent = 'Password must be at least 8 characters.'; err.style.display='block'; return; }}
  if (pw1 !== pw2) {{ err.textContent = 'Passwords do not match.'; err.style.display='block'; return; }}
  const btn = document.querySelector('button');
  btn.textContent = 'Setting up...'; btn.disabled = true;
  try {{
    const r = await fetch('/api/premium/setpassword', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{token: '{token}', password: pw1}})
    }});
    const d = await r.json();
    if (r.ok) {{
      window.location.href = '/premium/';
    }} else {{
      err.textContent = d.error || 'Setup failed. Try again.';
      err.style.display = 'block';
      btn.textContent = 'Set Password & Sign In'; btn.disabled = false;
    }}
  }} catch(e) {{
    err.textContent = 'Network error. Try again.';
    err.style.display = 'block';
    btn.textContent = 'Set Password & Sign In'; btn.disabled = false;
  }}
}}
</script>
</body></html>"""


@app.route("/api/premium/setpassword", methods=["POST"])
def api_premium_setpassword():
    data = request.get_json(silent=True) or {}
    token    = (data.get("token") or "").strip()
    password = (data.get("password") or "").strip()
    if not token or len(password) < 8:
        return jsonify({"error": "Invalid request"}), 400

    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, setup_token_expires FROM premium_users WHERE setup_token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Invalid or already-used setup link"}), 400
    username, expires = row
    if int(time.time()) > expires:
        return jsonify({"error": "Setup link has expired"}), 400

    # Change password via NC admin API
    nc_base   = os.environ.get("NEXTCLOUD_OCS_BASE", "https://nextcloud.example.com/ocs/v2.php/cloud")
    auth_b64  = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers   = {"Authorization": f"Basic {auth_b64}", "OCS-APIREQUEST": "true",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"}
    body      = urllib.parse.urlencode({"key": "password", "value": password}).encode()
    req       = urllib.request.Request(
        f"{nc_base}/users/{username}", data=body, headers=headers, method="PUT"
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
            resp = json.loads(r.read())
            status = resp.get("ocs", {}).get("meta", {}).get("status")
            if status != "ok":
                print(f"[setup] NC password change failed for {username}: {resp}", flush=True)
                return jsonify({"error": "Password update failed. Contact support."}), 500
    except Exception as e:
        print(f"[setup] NC password change error for {username}: {e}", flush=True)
        return jsonify({"error": "Could not reach Nextcloud. Try again."}), 500

    # Invalidate token
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE premium_users SET setup_token=NULL, setup_token_expires=NULL WHERE username=?",
        (username,)
    )
    conn.commit()
    conn.close()

    # Issue session
    token_sess = _issue_session(username)
    from flask import make_response
    resp = make_response(jsonify({"status": "ok"}))
    resp.set_cookie("bb_session", token_sess, max_age=86400*30, httponly=True, samesite="Lax")
    print(f"[setup] password set and session issued for {username}", flush=True)
    return resp


@app.route("/api/subscription_status")
def api_subscription_status():
    sess = _get_session(request)
    if not sess:
        return jsonify({"premium": False, "logged_in": False})
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT status, intel_queries_used, intel_quota FROM premium_users WHERE username=?",
        (sess["username"],)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"premium": False, "logged_in": True, **sess})
    status, used, quota = row
    return jsonify({
        "logged_in": True,
        "premium": status == "active",
        "status": status,
        "intel_queries_used": used,
        "intel_quota": quota,
        "intel_quota_remaining": max(0, quota - used),
        **sess,
    })



# ---------------------------------------------------------------------------
# Weather API (NWS — no key required)
# ---------------------------------------------------------------------------

@app.route("/api/premium/homicides/summary")
def api_premium_homicides_summary():
    sess = _get_session(request)
    if not sess or not sess.get("is_premium"):
        return jsonify({"error": "premium required"}), 403
    import os
    seed_count = 0
    seed_path  = "/opt/battlebuddy/homicides_2026.json"
    if os.path.exists(seed_path):
        try:
            import json as _json
            seed_data = _json.load(open(seed_path))
            seed_count = sum(int(e.get("count", 1)) for e in seed_data)
        except Exception:
            pass
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT ts_start, location FROM incidents
           WHERE itype = 'HOMICIDE'
             AND lat IS NOT NULL AND lon IS NOT NULL
             AND ts_start > strftime('%s','2026-01-01')
             AND is_test = 0
           ORDER BY ts_start DESC"""
    ).fetchall()
    conn.close()
    live_count = len(rows)
    # Derive "last" — prefer live geocoded entry, fall back to seed file
    last = None
    if rows:
        import datetime as _dt
        last = {
            "date":     _dt.datetime.fromtimestamp(rows[0][0]).strftime("%b %d"),
            "location": rows[0][1] or "",
        }
    elif seed_count:
        try:
            import json as _json2
            seed_data = sorted(_json2.load(open(seed_path)), key=lambda x: x.get("date",""))
            newest = seed_data[-1]
            from datetime import datetime as _dt2
            last = {
                "date":     _dt2.strptime(newest["date"], "%Y-%m-%d").strftime("%b %d"),
                "location": newest.get("address", ""),
            }
        except Exception:
            pass
    return jsonify({
        "ytd":   seed_count + live_count,
        "year":  2026,
        "last":  last,
    })


@app.route("/api/premium/atak/status")
def api_premium_atak_status():
    sess = _get_session(request)
    if not sess or not sess.get("is_premium"):
        return jsonify({"error": "premium required"}), 403
    return jsonify(_atak_mod.get_connection_status())


@app.route("/api/premium/weather")
def api_premium_weather():
    sess = _get_session(request)
    if not sess or not sess.get("is_premium"):
        return jsonify({"error": "premium required"}), 403
    # Use commute origin coords if saved, otherwise default to Austin
    lat, lon, location_label = 30.2672, -97.7431, "Austin TX"
    try:
        conn = sqlite3.connect(DB_PATH)
        row  = conn.execute(
            "SELECT commute_origin_lat, commute_origin_lon, commute_origin "
            "FROM premium_users WHERE username=?", (sess["username"],)
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            lat, lon = row[0], row[1]
            # Use first part of address as label (strip to city if possible)
            raw = (row[2] or "").strip()
            # "S IH-35 and Slaughter Ln, Austin TX" → "Austin TX"
            if "," in raw:
                location_label = raw.split(",")[-1].strip()
            elif raw:
                location_label = raw
    except Exception as e:
        print(f"[weather] DB lookup error: {e}", flush=True)
    data = weather_mod.get_nws_weather(lat, lon)
    if not data:
        return jsonify({"error": "weather unavailable"}), 503
    data["location"] = location_label
    return jsonify(data)


# ---------------------------------------------------------------------------
# Premium Display Dashboard (/premium/display)
# ---------------------------------------------------------------------------
@app.route("/premium/display")
def premium_display():
    html = """<!DOCTYPE html>
<html><head>
<title>Battle Buddy Display</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#e2e8f0;min-height:100vh;padding:16px}
  #login-overlay{position:fixed;inset:0;background:#0a0a0f;display:flex;align-items:center;justify-content:center;z-index:100}
  .login-box{background:#111827;border:1px solid #1e3a5f;border-radius:12px;padding:36px;width:340px;text-align:center}
  .login-box h2{color:#f90;margin-bottom:20px;font-size:22px}
  .login-box input{width:100%;background:#1f2937;color:#eee;border:1px solid #374151;border-radius:6px;padding:10px;font-size:15px;margin-bottom:10px}
  .login-box button{width:100%;background:#f90;color:#111;border:none;padding:11px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:16px}
  .login-box button:hover{background:#ffa820}
  .login-err{color:#f44;font-size:13px;margin-top:8px;display:none}

  #dashboard{display:none}
  .top-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
  .clock{font-size:48px;font-weight:700;color:#fff;letter-spacing:-1px;line-height:1}
  .clock-date{font-size:15px;color:#94a3b8;margin-top:3px}
  .top-links{display:flex;gap:12px;align-items:center}
  .top-links a{color:#64748b;font-size:13px;text-decoration:none}
  .top-links a:hover{color:#f90}
  .panel-toggles{display:flex;gap:8px;flex-wrap:wrap}
  .ptog{background:#1f2937;color:#94a3b8;border:1px solid #374151;border-radius:20px;padding:4px 12px;font-size:12px;cursor:pointer;user-select:none}
  .ptog.on{background:#1a2e1a;color:#4ade80;border-color:#16a34a}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
  .panel{background:#111827;border:1px solid #1e293b;border-radius:12px;padding:18px}
  .panel-title{font-size:11px;font-weight:700;color:#475569;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:12px}

  /* Weather */
  .wx-current{display:flex;align-items:center;gap:16px;margin-bottom:14px}
  .wx-temp{font-size:56px;font-weight:700;color:#fff;line-height:1}
  .wx-desc{font-size:15px;color:#94a3b8;margin-top:4px}
  .wx-wind{font-size:13px;color:#64748b}
  .wx-days{display:flex;gap:8px;flex-wrap:wrap}
  .wx-day{flex:1;min-width:80px;background:#0f172a;border-radius:8px;padding:8px;text-align:center}
  .wx-day-name{font-size:11px;color:#64748b;margin-bottom:4px}
  .wx-day-temp{font-size:18px;font-weight:700;color:#f90}
  .wx-day-short{font-size:11px;color:#94a3b8;margin-top:3px}
  .wx-day-precip{font-size:11px;color:#38bdf8;margin-top:2px}

  /* Incidents */
  .inc-list{display:flex;flex-direction:column;gap:8px;max-height:280px;overflow:hidden}
  .inc-item{display:flex;gap:10px;align-items:flex-start;padding:8px;background:#0f172a;border-radius:8px}
  .inc-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;margin-top:4px}
  .inc-type{font-size:13px;font-weight:600;color:#e2e8f0}
  .inc-loc{font-size:12px;color:#64748b;margin-top:2px}
  .inc-time{font-size:11px;color:#475569;white-space:nowrap;flex-shrink:0}
  .inc-none{color:#475569;font-size:14px;padding:12px 0}

  /* Commute */
  .commute-time{font-size:42px;font-weight:700;color:#fff;line-height:1}
  .commute-vs{font-size:14px;margin-top:6px}
  .commute-faster{color:#4ade80}
  .commute-slower{color:#f87171}
  .commute-normal{color:#94a3b8}
  .commute-setup{color:#64748b;font-size:14px;line-height:1.6}
  .commute-setup a{color:#f90;text-decoration:none}

  /* Headlines */
  .hl-list{display:flex;flex-direction:column;gap:6px}
  .hl-item{padding:7px 0;border-bottom:1px solid #1e293b;font-size:13px;color:#cbd5e1;line-height:1.4}
  .hl-item:last-child{border-bottom:none}
  .hl-time{font-size:11px;color:#475569;margin-top:2px}

  .dot-red{background:#ef4444}
  .dot-orange{background:#f97316}
  .dot-yellow{background:#eab308}
  .dot-blue{background:#3b82f6}
  .dot-gray{background:#6b7280}

  @media(max-width:600px){.clock{font-size:32px}.wx-temp{font-size:40px}}
  .atak-status{display:flex;align-items:center;gap:6px;font-size:11px;color:#64748b;margin-top:4px}
  .atak-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
  .atak-dot.on{background:#4ade80;box-shadow:0 0 6px #4ade80}
  .atak-dot.off{background:#ef4444;box-shadow:0 0 6px #ef4444}
</style>
</head><body>

<div id="login-overlay">
  <div class="login-box">
    <h2>Battle Buddy</h2>
    <input type="text" id="l-user" placeholder="Username" autocomplete="username">
    <input type="password" id="l-pass" placeholder="Password" autocomplete="current-password">
    <button onclick="doLogin()">Sign In</button>
    <div class="login-err" id="l-err"></div>
  </div>
</div>

<div id="dashboard">
  <div class="top-bar">
    <div>
      <div class="clock" id="clock">--:--</div>
      <div class="clock-date" id="clock-date"></div>
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">
      <div class="top-links">
        <a href="/premium/">Dashboard</a>
        <a href="{NEXTCLOUD_WEB_BASE}" target="_blank">Nextcloud</a>
        <a href="#" onclick="toggleFullscreen()" id="fs-btn">⛶ Full Screen</a>
        <a href="#" onclick="doLogout()" style="color:#ef4444">Sign out</a>
      </div>
      <div class="atak-status">
        <div class="atak-dot off" id="atak-dot"></div>
        <span id="atak-label">TAK Server</span>
      </div>
      <div class="panel-toggles" id="panel-toggles"></div>
    </div>
  </div>
  <div class="grid" id="grid"></div>
</div>

<script>
const PANELS = [
  {id:'weather',   label:'Weather'},
  {id:'incidents', label:'Incidents'},
  {id:'commute',   label:'Commute'},
  {id:'headlines', label:'Headlines'},
  {id:'homicides', label:'Homicides'},
];

let state = {weather:null, incidents:null, commute:null, headlines:null, homicides:null};
let lastUpdated = {weather:null, incidents:null, commute:null, headlines:null, homicides:null};
let username = '';

// ── Clock ────────────────────────────────────────────────────────────────────
function tickClock(){
  const now = new Date();
  const h = now.getHours(), m = now.getMinutes();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12  = h % 12 || 12;
  document.getElementById('clock').textContent = h12 + ':' + String(m).padStart(2,'0') + ' ' + ampm;
  document.getElementById('clock-date').textContent = now.toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric',year:'numeric'});
}
setInterval(tickClock, 1000);
tickClock();

// ── Panel toggles ────────────────────────────────────────────────────────────
function getVisible(){
  try { return JSON.parse(localStorage.getItem('bb_display_panels') || 'null') || PANELS.map(p=>p.id); }
  catch(e){ return PANELS.map(p=>p.id); }
}
function setVisible(ids){ localStorage.setItem('bb_display_panels', JSON.stringify(ids)); }

function buildToggles(){
  const vis = getVisible();
  const el = document.getElementById('panel-toggles');
  el.innerHTML = '';
  PANELS.forEach(p => {
    const on = vis.includes(p.id);
    const b = document.createElement('span');
    b.className = 'ptog' + (on ? ' on' : '');
    b.textContent = p.label;
    b.onclick = () => {
      let v = getVisible();
      if(v.includes(p.id)) v = v.filter(x=>x!==p.id);
      else v.push(p.id);
      setVisible(v);
      buildToggles();
      renderGrid();
    };
    el.appendChild(b);
  });
}

// ── Auth ─────────────────────────────────────────────────────────────────────
async function checkSession(){
  const r = await fetch('/api/me');
  const d = await r.json();
  if(d.logged_in && d.is_premium){
    username = d.username;
    showDashboard();
  }
}
async function doLogin(){
  const u = document.getElementById('l-user').value.trim().toLowerCase();
  const p = document.getElementById('l-pass').value;
  const err = document.getElementById('l-err');
  err.style.display='none';
  if(!u||!p){err.textContent='Enter username and password.';err.style.display='block';return;}
  const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
  const d = await r.json();
  if(d.token && d.is_premium){ username=d.username; showDashboard(); }
  else if(d.token){ err.textContent='This account does not have premium access.'; err.style.display='block'; }
  else { err.textContent=d.error||'Invalid credentials.'; err.style.display='block'; }
}
function toggleFullscreen(){
  const el = document.documentElement;
  if (!document.fullscreenElement) {
    el.requestFullscreen().catch(()=>{});
    document.getElementById("fs-btn").textContent = "✕ Exit Full Screen";
  } else {
    document.exitFullscreen();
    document.getElementById("fs-btn").textContent = "⛶ Full Screen";
  }
}
document.addEventListener("fullscreenchange", () => {
  const btn = document.getElementById("fs-btn");
  if(btn) btn.textContent = document.fullscreenElement ? "✕ Exit Full Screen" : "⛶ Full Screen";
});
async function pollAtak(){
  try{
    const r = await fetch('/api/premium/atak/status');
    const d = await r.json();
    const dot   = document.getElementById('atak-dot');
    const label = document.getElementById('atak-label');
    if(d.connected){
      dot.className='atak-dot on';
      label.textContent='TAK Server';
    } else {
      dot.className='atak-dot off';
      label.textContent='TAK Offline';
    }
  }catch(e){}
}
async function doLogout(){
  await fetch('/api/logout',{method:'POST'});
  location.reload();
}
function showDashboard(){
  document.getElementById('login-overlay').style.display='none';
  document.getElementById('dashboard').style.display='block';
  buildToggles();
  loadAll();
  pollAtak();
  setInterval(loadAll, 60000);
  setInterval(pollAtak, 30000);
}

// ── Data loaders ─────────────────────────────────────────────────────────────
async function loadAll(){
  const vis = getVisible();
  if(vis.includes('weather'))   loadWeather();
  if(vis.includes('incidents')) loadIncidents();
  if(vis.includes('commute'))   loadCommute();
  if(vis.includes('headlines')) loadHeadlines();
  if(vis.includes('homicides')) loadHomicides();
}

async function loadWeather(){
  try{
    const r = await fetch('/api/premium/weather');
    state.weather = await r.json();
    if(!state.weather.error) lastUpdated.weather = new Date();
  }catch(e){ state.weather = null; }
  renderGrid();
}
async function loadIncidents(){
  try{
    const r = await fetch('/api/incidents/active');
    state.incidents = await r.json();
    lastUpdated.incidents = new Date();
  }catch(e){ state.incidents = null; }
  renderGrid();
}
async function loadCommute(){
  try{
    const r = await fetch('/api/commute/time');
    state.commute = await r.json();
    lastUpdated.commute = new Date();
  }catch(e){ state.commute = null; }
  renderGrid();
}
async function loadHomicides(){
  try{
    const r = await fetch('/api/premium/homicides/summary');
    state.homicides = await r.json();
    lastUpdated.homicides = new Date();
  }catch(e){ state.homicides = null; }
  renderGrid();
}
async function loadHeadlines(){
  try{
    const r = await fetch('/api/premium/headlines');
    state.headlines = await r.json();
    lastUpdated.headlines = new Date();
  }catch(e){ state.headlines = null; }
  renderGrid();
}

// ── Renderers ─────────────────────────────────────────────────────────────────
function incDotClass(itype){
  const t = (itype||'').toUpperCase();
  if(['SHOOTING','OFFICER DOWN','PURSUIT','WEAPONS','STABBING','MASS CASUALTY'].includes(t)) return 'dot-red';
  if(['STRUCTURE FIRE','HAZMAT','AIR ASSET ACTIVE'].includes(t)) return 'dot-orange';
  if(['FIRE DISPATCH','EMS DISPATCH'].includes(t)) return 'dot-yellow';
  if(['CRASH/COLLISION'].includes(t)) return 'dot-blue';
  return 'dot-gray';
}

function _ts(key){
  const d = lastUpdated[key];
  if(!d) return '';
  return `<div style="font-size:10px;color:#334155;margin-top:10px;text-align:right">Updated ${d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})}</div>`;
}
function renderWeather(w){
  if(!w || w.error) return '<div class="panel"><div class="panel-title">Weather</div><div class="inc-none">Weather unavailable</div></div>';
  const c = w.current;
  const days = (w.forecast||[]).slice(0,5).map(d=>`
    <div class="wx-day">
      <div class="wx-day-name">${d.name}</div>
      <div class="wx-day-temp">${d.temp}&deg;</div>
      <div class="wx-day-short">${d.short}</div>
      ${d.precip>0?`<div class="wx-day-precip">&#9730; ${d.precip}%</div>`:''}
    </div>`).join('');
  return `<div class="panel">
    <div class="panel-title">Weather &mdash; ${w.location||'Austin TX'}</div>
    <div class="wx-current">
      <div>
        <div class="wx-temp">${c.temp}&deg;${c.unit}</div>
        <div class="wx-desc">${c.desc}</div>
        <div class="wx-wind">${c.wind}</div>
      </div>
    </div>
    <div class="wx-days">${days}</div>
    ${_ts("weather")}
  </div>`;
}

function renderIncidents(data){
  let items = Array.isArray(data) ? data : (data&&data.incidents ? data.incidents : []);
  items = items.slice(0,6);
  const rows = items.length ? items.map(i=>{
    const t = new Date(i.started_at*1000||i.ts*1000||Date.now());
    const age = Math.round((Date.now()-t)/60000);
    const ageStr = age < 60 ? age+'m ago' : Math.round(age/60)+'h ago';
    const artLink = i.article_url
      ? `<a href="${i.article_url}" target="_blank" rel="noopener" style="display:block;font-size:11px;color:#38bdf8;margin-top:3px;text-decoration:none">📰 Press coverage ↗</a>`
      : '';
    return `<div class="inc-item">
      <div class="inc-dot ${incDotClass(i.itype)}"></div>
      <div style="flex:1">
        <div class="inc-type">${i.itype||'Unknown'}</div>
        <div class="inc-loc">${i.location||i.description||''}</div>
        ${artLink}
      </div>
      <div class="inc-time">${ageStr}</div>
    </div>`;
  }).join('') : '<div class="inc-none">No active incidents</div>';
  return `<div class="panel"><div class="panel-title">Active Incidents</div><div class="inc-list">${rows}</div>${_ts("incidents")}</div>`;
}

function renderCommute(data){
  if(!data || data.error === 'no_route'){
    return `<div class="panel"><div class="panel-title">Commute</div>
      <div class="commute-setup">No commute route configured.<br><br>
      <a href="/premium/">Set up your commute &rarr;</a></div></div>`;
  }
  if(!data || data.error){
    return `<div class="panel"><div class="panel-title">Commute</div><div class="inc-none">Traffic unavailable</div></div>`;
  }
  const mins = data.live_mins||0;
  const base = data.baseline_mins||0;
  const diff = mins - base;
  let vs = '', cls = 'commute-normal';
  if(base > 0){
    if(diff > 3){ vs=`+${diff} min vs normal`; cls='commute-slower'; }
    else if(diff < -3){ vs=`${diff} min vs normal`; cls='commute-faster'; }
    else{ vs='On time'; cls='commute-normal'; }
  }
  const h = Math.floor(mins/60), m = mins%60;
  const timeStr = h>0 ? `${h}h ${m}m` : `${m} min`;
  return `<div class="panel"><div class="panel-title">Commute &mdash; ${data.origin||''} &rarr; ${data.dest||''}</div>
    <div class="commute-time">${timeStr}</div>
    ${vs?`<div class="commute-vs ${cls}">${vs}</div>`:''}
    ${_ts("commute")}
  </div>`;
}

function renderHeadlines(data){
  if(!Array.isArray(data)||data.length===0)
    return `<div class="panel"><div class="panel-title">Incident Headlines</div><div class="inc-none">No press coverage in last 24h</div>${_ts("headlines")}</div>`;
  const rows = data.map(h=>{
    const age = Math.round((Date.now()-h.ts*1000)/60000);
    const ageStr = age<60?age+'m ago':(age<1440?Math.round(age/60)+'h ago':Math.round(age/1440)+'d ago');
    const src = h.source?`<span style="color:#475569;font-size:11px"> · ${h.source}</span>`:'';
    const link = h.url
      ? `<a href="${h.url}" target="_blank" rel="noopener" style="color:#38bdf8;text-decoration:none;font-weight:500">${h.headline.replace(/ - [^-]+$/,"")}</a>`
      : `<span>${h.headline}</span>`;
    let radio = '';
    if(h.incident){
      const inc = h.incident;
      const lead = inc.ts_start ? Math.round((h.ts-inc.ts_start)/60) : null;
      const leadStr = lead!==null?(lead>=0?lead+'m before article':Math.abs(lead)+'m after'):'';
      const loc = inc.location?` · ${inc.location}`:'';
      radio = `<div style="font-size:11px;color:#4ade80;margin-top:3px">&#128225; Radio: ${inc.itype||'??'}${loc}${leadStr?" ("+leadStr+")":""}</div>`;
    }
    return `<div class="hl-item">${link}${src}${radio}<div class="hl-time">${ageStr}</div></div>`;
  }).join('');
  return `<div class="panel"><div class="panel-title">Incident Headlines</div><div class="hl-list">${rows}</div>${_ts("headlines")}</div>`;
}

function renderHomicides(data){
  if(!data || data.error) return '<div class="panel"><div class="panel-title">Homicides YTD</div><div class="inc-none">Unavailable</div></div>';
  const last = data.last ? `<div style="font-size:12px;color:#64748b;margin-top:6px">Last: ${data.last.date}${data.last.location ? ' &mdash; ' + data.last.location : ''}</div>` : '';
  return `<div class="panel">
    <div class="panel-title">Homicides &mdash; Austin ${data.year} YTD</div>
    <div style="font-size:72px;font-weight:800;color:#ef4444;line-height:1">${data.ytd}</div>
    ${last}
    ${_ts("homicides")}
  </div>`;
}
function renderGrid(){
  const vis = getVisible();
  const map = {
    weather:   renderWeather(state.weather),
    incidents: renderIncidents(state.incidents),
    commute:   renderCommute(state.commute),
    headlines: renderHeadlines(state.headlines),
    homicides: renderHomicides(state.homicides),
  };
  document.getElementById('grid').innerHTML = vis.map(id=>map[id]||'').join('');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
document.getElementById('l-pass').addEventListener('keydown', e => {
  if(e.key === 'Enter') doLogin();
});
// Apply ?zoom= URL param
const _zp = new URLSearchParams(location.search).get("zoom");
if(_zp && parseFloat(_zp) > 0) document.body.style.zoom = (parseFloat(_zp) * 100) + "%";
checkSession();
</script>
</body></html>"""
    return html



@app.route("/api/premium/headlines")
def premium_headlines():
    """Return incident_articles linked in the last 24h for the Headlines panel."""
    sess = _get_session(request)
    if not sess or (not sess.get("is_admin") and not sess.get("is_premium")):
        return jsonify({"error": "unauthorized"}), 401
    cutoff = time.time() - 24 * 3600
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT ia.id, ia.ts, ia.headline, ia.url, ia.source, ia.snippet, ia.match_score,
               i.id   AS inc_id,    i.itype,    i.location,
               i.ts_start,          i.description
        FROM incident_articles ia
        LEFT JOIN incidents i ON ia.incident_id = i.id
        WHERE ia.ts >= ?
        ORDER BY ia.ts DESC LIMIT 20
    """, (cutoff,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "id":          r["id"],
            "ts":          r["ts"],
            "headline":    r["headline"],
            "url":         r["url"],
            "source":      r["source"],
            "snippet":     r["snippet"],
            "match_score": r["match_score"],
            "incident": {
                "id":       r["inc_id"],
                "itype":    r["itype"],
                "location": r["location"],
                "ts_start": r["ts_start"],
            } if r["inc_id"] else None,
        })
    return jsonify(out)


@app.route("/api/premium/citizen_intel")
def api_premium_citizen_intel():
    sess = _get_session(request)
    if not sess:
        return jsonify({"error": "unauthorized"}), 401
    cutoff = time.time() - 7 * 86400
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT r.ts, r.post_id, r.subreddit, r.title, r.url, r.author,
               r.keywords, r.incident_id, r.match_score,
               i.ts_start, i.itype, i.location,
               (SELECT COUNT(*) FROM incident_calls ic WHERE ic.incident_id = r.incident_id) as call_count
        FROM reddit_intel r
        LEFT JOIN incidents i ON r.incident_id = i.id
        WHERE r.ts > ?
        ORDER BY r.ts DESC LIMIT 40
    """, (cutoff,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        ts,post_id,subreddit,title,url,author,keywords,inc_id,score,inc_ts,itype,location,calls = row
        result.append({
            "ts": ts, "post_id": post_id, "subreddit": subreddit,
            "title": title, "url": url, "author": author,
            "keywords": keywords or "", "incident_id": inc_id,
            "match_score": score, "incident_ts": inc_ts,
            "incident_type": itype, "incident_location": location,
            "call_count": calls or 0
        })
    return jsonify(result)

@app.route("/premium/")
def premium_index():
    sess = _get_session(request)
    is_premium = sess["is_premium"] if sess else False
    is_admin   = sess["is_admin"]   if sess else False
    username = sess["username"] if sess else ""

    if is_premium or is_admin:
        # Redirect active premium members to dashboard
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT intel_queries_used, intel_quota FROM premium_users WHERE username=?",
            (username,)
        ).fetchone()
        conn.close()
        used = row[0] if row else 0
        quota = row[1] if row else 5
        remaining = max(0, quota - used)
        dashboard = f"""<!DOCTYPE html>
<html><head><title>Premium Dashboard — Battle Buddy</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:sans-serif;background:#111;color:#eee;max-width:700px;margin:40px auto;padding:20px}}
  h1{{color:#f90;margin-bottom:4px}}
  .sub{{color:#888;margin-bottom:30px}}
  .card{{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:20px;margin:16px 0}}
  .card h3{{margin:0 0 8px;color:#f90}}
  .badge{{display:inline-block;background:#f90;color:#111;font-weight:bold;border-radius:4px;padding:2px 10px;font-size:13px}}
  .quota{{font-size:28px;font-weight:bold;color:#f90}}
  textarea{{width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:10px;font-size:14px;resize:vertical}}
  button{{background:#f90;color:#111;border:none;padding:10px 24px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:15px}}
  button:hover{{background:#ffa820}}
  #result{{background:#222;border:1px solid #333;border-radius:6px;padding:16px;margin-top:16px;display:none;white-space:pre-wrap;line-height:1.6}}
  .logout{{float:right;color:#888;text-decoration:none;font-size:13px}}
  .logout:hover{{color:#eee}}
</style></head><body>
<a class="logout" href="#" onclick="logout()">Sign out</a>
<h1>Battle Buddy Premium</h1>
<p class="sub">Welcome back, <strong>{username}</strong> &nbsp;<span class="badge">PREMIUM</span></p>

<div class="card">
  <h3>Intel Query</h3>
  <p>Ask a question about Austin radio traffic. We search the database and synthesize an intelligence summary.</p>
  <p>Queries remaining this month: <span class="quota">{remaining}</span> / {quota}</p>
  <textarea id="q" rows="3" placeholder="e.g. Any shootings near North Lamar in the last week?"></textarea><br><br>
  <button onclick="runQuery()">Run Intel Query</button>
  <div id="result"></div>
</div>

<div class="card">
  <h3>&#128269; Citizen Intel &mdash; Reddit &times; Radio</h3>
  <p style="font-size:13px;color:#888;margin-bottom:14px">Citizens reporting incidents on r/Austin, auto cross-referenced against captured radio traffic. Updated every 5 minutes.</p>
  <div id="ci-list"><div style="color:#666;font-size:13px">Loading...</div></div>
</div>

<div class="card">
  <h3>Live Alerts</h3>
  <p>You are enrolled in priority incident alerts via Nextcloud Talk.</p>
  <p><a href="{NEXTCLOUD_WEB_BASE}" target="_blank" style="color:#f90">Open Nextcloud Talk →</a></p>
</div>

<div class="card">
  <h3>🚗 Commute Monitor</h3>
  <p>Save your commute route and Battle Buddy will alert you via Talk when an active incident is detected near your path — with current travel time vs your normal commute.</p>
  <div id="commute-status" style="margin:12px 0;font-size:14px;color:#aaa">Loading...</div>
  <div id="commute-form" style="display:none">
    <input type="text" id="commute-origin" placeholder="Origin — include city & state (e.g. Slaughter Ln &amp; I-35, Austin TX)"
      style="width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:8px;font-size:14px;margin-bottom:8px;box-sizing:border-box">
    <input type="text" id="commute-dest" placeholder="Destination — include city &amp; state (e.g. 220 E 6th St, Austin TX)"
      style="width:100%;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:8px;font-size:14px;margin-bottom:8px;box-sizing:border-box">
    <button onclick="saveCommute()" style="background:#f90;color:#111;border:none;padding:8px 20px;border-radius:6px;font-weight:bold;cursor:pointer">Save Route</button>
    <div id="commute-err" style="color:#f44;font-size:13px;margin-top:6px;display:none"></div>
  </div>
  <div id="commute-live" style="display:none;margin-top:10px">
    <div style="font-size:28px;font-weight:bold;color:#f90" id="commute-mins">--</div>
    <div style="font-size:13px;color:#888" id="commute-delta"></div>
    <div style="font-size:12px;color:#555;margin-top:4px" id="commute-route"></div>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <a href="/premium/commute" target="_blank" style="background:#f90;color:#111;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:bold;text-decoration:none">Open Live Map →</a>
      <button onclick="showCommuteForm()" style="background:#333;color:#eee;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">Change Route</button>
    </div>
  </div>
</div>

<div class="card">
  <h3>Intel News Feed</h3>
  <p>You are auto-subscribed to the <strong style="color:#f90">Battle Buddy Intel Feed</strong> in Nextcloud News. Every confirmed incident, APD press release, and homicide update — one scrollable feed, updates continuously.</p>
  <p><a href="{NEXTCLOUD_WEB_BASE}/apps/news" target="_blank" style="color:#f90">Open Nextcloud News →</a></p>
  <p style="margin-top:10px;font-size:12px;color:#666">You can also subscribe any RSS reader to the feed directly:<br>
  <code style="color:#aaa;font-size:11px">https://battlebuddy.news/public/feed.rss</code></p>
</div>

<div class="card">
  <h3>ATAK Field Package</h3>
  <p>Contact Battle Buddy Ops to receive your ATAK data package for field situational awareness.</p>
</div>

<script>
async function runQuery() {{
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const btn = document.querySelector('button');
  btn.textContent = 'Searching...';
  btn.disabled = true;
  const res = document.getElementById('result');
  res.style.display = 'block';
  res.textContent = 'Querying database and synthesizing...';
  try {{
    const r = await fetch('/api/intel', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{query: q}})
    }});
    const d = await r.json();
    if (d.error) {{
      res.textContent = 'Error: ' + d.error;
    }} else {{
      res.textContent = d.summary + '\\n\\n[' + d.calls_hit + ' call(s) matched | TGIDs: ' + (d.tgids.join(', ') || 'none') + ' | ' + (d.quota_remaining ?? '?') + ' queries remaining]';
    }}
  }} catch(e) {{
    res.textContent = 'Request failed: ' + e;
  }}
  btn.textContent = 'Run Intel Query';
  btn.disabled = false;
}}
async function logout() {{
  await fetch('/api/logout', {{method:'POST'}});
  window.location.href = '/premium/';
}}

async function loadCommuteStatus() {{
  try {{
    const r = await fetch('/api/commute/time');
    const d = await r.json();
    const status = document.getElementById('commute-status');
    const live   = document.getElementById('commute-live');
    const form   = document.getElementById('commute-form');
    status.style.display = 'none';
    if (!d.configured) {{
      form.style.display = 'block';
      live.style.display = 'none';
    }} else {{
      form.style.display = 'none';
      live.style.display = 'block';
      document.getElementById('commute-mins').textContent = d.live_mins + ' min';
      const delta = d.delta_mins;
      const deltaEl = document.getElementById('commute-delta');
      if (delta > 0)       deltaEl.textContent = '+' + delta + ' min over normal';
      else if (delta < 0)  deltaEl.textContent = Math.abs(delta) + ' min faster than normal';
      else                 deltaEl.textContent = 'Normal commute time';
      document.getElementById('commute-route').textContent = d.origin + ' → ' + d.destination;
    }}
  }} catch(e) {{
    document.getElementById('commute-status').textContent = 'Commute data unavailable.';
  }}
}}

function showCommuteForm() {{
  document.getElementById('commute-live').style.display = 'none';
  document.getElementById('commute-form').style.display = 'block';
}}

async function saveCommute() {{
  const origin = document.getElementById('commute-origin').value.trim();
  const dest   = document.getElementById('commute-dest').value.trim();
  const err    = document.getElementById('commute-err');
  err.style.display = 'none';
  if (!origin || !dest) {{ err.textContent = 'Both addresses required.'; err.style.display='block'; return; }}
  const btn = document.querySelector('#commute-form button');
  btn.textContent = 'Saving...'; btn.disabled = true;
  try {{
    const r = await fetch('/api/commute/save', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{origin, destination: dest}})
    }});
    const d = await r.json();
    if (d.ok) {{
      await loadCommuteStatus();
    }} else {{
      err.textContent = d.error || 'Save failed.'; err.style.display='block';
    }}
  }} catch(e) {{
    err.textContent = 'Network error.'; err.style.display='block';
  }}
  btn.textContent = 'Save Route'; btn.disabled = false;
}}

loadCommuteStatus();

async function loadCitizenIntel() {{
  try {{
    const r = await fetch('/api/premium/citizen_intel');
    const posts = await r.json();
    const el = document.getElementById('ci-list');
    if (!posts || !posts.length) {{
      el.innerHTML = '<div style="color:#666;font-size:13px">No recent citizen reports.</div>';
      return;
    }}
    const HI_KW = ['standoff','barricade','swat','shooting','shots','hostage','pursuit','homicide','murder','stabbing','armed','crime scene','avoid the area'];
    el.innerHTML = posts.map(p => {{
      const age = Math.round((Date.now()/1000 - p.ts) / 60);
      const ageStr = age < 60 ? age + 'm ago' : (Math.round(age/60) + 'h ago');
      const kws = (p.keywords||'').split(',').filter(Boolean).slice(0,5);
      const isHi = kws.some(k => HI_KW.includes(k.trim()));
      const kwHtml = kws.map(k =>
        '<span style="background:#2a1a0a;color:#f90;border-radius:3px;padding:1px 6px;font-size:11px;margin-right:3px">' + k.trim() + '</span>'
      ).join('');
      const badge = isHi
        ? '<span style="background:#7f1d1d;color:#fca5a5;border-radius:3px;padding:1px 7px;font-size:10px;font-weight:bold;margin-left:8px">HIGH</span>'
        : '';
      let matchHtml = '';
      if (p.incident_id) {{
        const iAge = p.incident_ts ? Math.round((Date.now()/1000 - p.incident_ts) / 60) : null;
        const iAgeStr = iAge !== null ? (iAge < 60 ? iAge + 'm ago' : Math.round(iAge/60) + 'h ago') : '';
        const loc = p.incident_location || 'Location TBD';
        const itype = p.incident_type || 'INCIDENT';
        const calls = p.call_count || 0;
        matchHtml = '<div style="margin-top:10px;padding-top:10px;border-top:1px solid #252525">'
          + '<div style="background:#0d1f0d;border:1px solid #1a4a1a;border-radius:6px;padding:10px 14px">'
          + '<div style="font-size:10px;color:#4ade80;font-weight:bold;letter-spacing:1.5px;margin-bottom:5px">&#9654; RADIO CROSS-REFERENCE (score: ' + p.match_score + ')</div>'
          + '<div style="font-size:14px;color:#eee;font-weight:bold">' + itype + '</div>'
          + '<div style="font-size:12px;color:#888;margin-top:2px">' + loc + (iAgeStr ? ' &middot; ' + iAgeStr : '') + '</div>'
          + '<div style="font-size:12px;color:#4ade80;margin-top:4px">&#128225; ' + calls + ' radio call' + (calls!==1?'s':'') + ' captured</div>'
          + '</div></div>';
      }}
      const borderStyle = p.incident_id ? 'border-color:#1a3a1a' : '';
      return '<div style="border:1px solid #252525;border-radius:8px;padding:14px;margin-bottom:10px;background:#161616;' + borderStyle + '">'
        + '<div style="margin-bottom:6px">'
        + '<a href="' + p.url + '" target="_blank" style="color:#f90;font-size:14px;font-weight:bold;text-decoration:none;line-height:1.4">' + p.title + '</a>' + badge
        + '</div>'
        + '<div style="font-size:11px;color:#555;margin-bottom:7px">u/' + p.author + ' &middot; r/' + p.subreddit + ' &middot; ' + ageStr + '</div>'
        + '<div style="margin-bottom:4px">' + kwHtml + '</div>'
        + matchHtml
        + '</div>';
    }}).join('');
  }} catch(e) {{
    document.getElementById('ci-list').innerHTML = '<div style="color:#666;font-size:13px">Intel unavailable.</div>';
  }}
}}
loadCitizenIntel();
setInterval(loadCitizenIntel, 300000);
</script>
</body></html>"""
        return dashboard

    # Not premium — show subscribe page
    subscribe = """<!DOCTYPE html>
<html><head><title>Battle Buddy — Subscribe</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box}
  body{font-family:sans-serif;background:#111;color:#eee;max-width:760px;margin:40px auto;padding:20px}
  h1{color:#f90;margin-bottom:4px}
  .tagline{font-size:17px;color:#aaa;margin-bottom:18px}
  .trial-badge{display:inline-block;background:#1a3a1a;color:#4f4;border:1px solid #2d6b2d;border-radius:20px;padding:5px 14px;font-size:13px;font-weight:bold;margin-bottom:22px}
  .toggle{display:flex;gap:0;margin-bottom:24px;border:1px solid #444;border-radius:8px;overflow:hidden;width:fit-content}
  .toggle button{background:transparent;color:#aaa;border:none;padding:8px 20px;cursor:pointer;font-size:14px;font-weight:bold;transition:all .2s}
  .toggle button.active{background:#f90;color:#111}
  .plans{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
  .card{flex:1;min-width:280px;background:#1a1a1a;border:2px solid #333;border-radius:12px;padding:24px;cursor:pointer;transition:border-color .2s}
  .card:hover{border-color:#f90}
  .card.selected{border-color:#f90;background:#1f1a0d}
  .card-label{font-size:11px;font-weight:bold;color:#888;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
  .card-name{font-size:22px;font-weight:bold;color:#fff;margin-bottom:2px}
  .card-price{font-size:36px;font-weight:bold;color:#f90;margin:12px 0 2px}
  .card-price span{font-size:15px;color:#888;font-weight:normal}
  .card-save{font-size:12px;color:#4f4;font-weight:bold;margin-bottom:14px;min-height:18px}
  .features{list-style:none;padding:0;margin:0 0 20px}
  .features li{padding:5px 0;font-size:14px;color:#ccc}
  .features li::before{content:"✓ ";color:#f90;font-weight:bold}
  .features li.no::before{content:"✗ ";color:#555}
  .features li.no{color:#555}
  .form-row{display:flex;gap:12px;margin-bottom:10px;flex-wrap:wrap}
  input{flex:1;min-width:200px;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:10px;font-size:15px}
  input::placeholder{color:#666}
  button.sub-btn{width:100%;background:#f90;color:#111;border:none;padding:14px;border-radius:6px;font-weight:bold;cursor:pointer;font-size:17px;margin-top:8px}
  button.sub-btn:hover{background:#ffa820}
  button.sub-btn:disabled{opacity:.5;cursor:not-allowed}
  .note{color:#666;font-size:12px;margin-top:10px;text-align:center}
  #err{color:#f44;font-size:14px;margin-top:8px;display:none}
  .login-link{text-align:center;margin-top:20px;color:#888;font-size:14px}
  .login-link a{color:#f90;cursor:pointer}
</style></head><body>
<h1>Battle Buddy</h1>
<p class="tagline">Real-time Austin intelligence for people who need to know.</p>
<div class="trial-badge">🎁 7-day free trial — cancel anytime</div>

<div class="toggle">
  <button id="btn-monthly" class="active" onclick="setBilling('monthly')">Monthly</button>
  <button id="btn-annual" onclick="setBilling('annual')">Annual &nbsp;· Save 30%</button>
</div>

<div class="plans">
  <div class="card" id="card-basic" onclick="selectPlan('basic')">
    <div class="card-label">Entry</div>
    <div class="card-name">Basic</div>
    <div class="card-price" id="price-basic">$4 <span>/ month</span></div>
    <div class="card-save" id="save-basic"></div>
    <ul class="features">
      <li>Live incident alerts via Nextcloud Talk</li>
      <li>Commute Monitor with incident corridor alerts</li>
      <li>Intel News Feed in Nextcloud News</li>
      <li>Priority DMs for high-severity events</li>
      <li class="no">ATAK field data package</li>
      <li class="no">SITREP access</li>
    </ul>
  </div>
  <div class="card selected" id="card-premium" onclick="selectPlan('premium')">
    <div class="card-label">Full Access</div>
    <div class="card-name">Premium</div>
    <div class="card-price" id="price-premium">$11 <span>/ month</span></div>
    <div class="card-save" id="save-premium"></div>
    <ul class="features">
      <li>Live incident alerts via Nextcloud Talk</li>
      <li>Commute Monitor with incident corridor alerts</li>
      <li>Intel News Feed in Nextcloud News</li>
      <li>Priority DMs for high-severity events</li>
      <li>ATAK field data package (WinTAK / ATAK / iTAK)</li>
      <li>SITREP access</li>
    </ul>
  </div>
</div>

<div class="form-row">
  <input type="text" id="username" placeholder="Choose a username" autocomplete="off">
  <input type="text" id="display" placeholder="Your name (optional)">
</div>
<button class="sub-btn" id="sub-btn" onclick="subscribe()">Start Free Trial — Premium Monthly →</button>
<div id="err"></div>
<p class="note">7-day free trial. Card required but not charged until trial ends.</p>

<div class="login-link">Already a member? <a onclick="showLogin()">Sign in</a></div>

<div id="login-box" style="display:none;margin-top:20px">
  <input type="text" id="l-user" placeholder="Username" style="display:block;width:100%;margin-bottom:10px;box-sizing:border-box">
  <input type="password" id="l-pass" placeholder="Password" style="display:block;width:100%;margin-bottom:10px;box-sizing:border-box">
  <button onclick="login()">Sign In</button>
  <div id="login-err" style="color:#f44;font-size:14px;margin-top:8px;display:none"></div>
</div>

<script>
function showLogin() {
  document.getElementById('login-box').style.display = 'block';
}
let billing = 'monthly';
let tier = 'premium';
const prices = {
  basic:   {monthly:{plan:'basic_monthly',   label:'$4',  per:'/ month',save:''},
             annual: {plan:'basic_annual',    label:'$36', per:'/ year', save:'Save $12/yr'}},
  premium: {monthly:{plan:'premium_monthly', label:'$11', per:'/ month',save:''},
             annual: {plan:'premium_annual',  label:'$99', per:'/ year', save:'Save $33/yr'}}
};
function setBilling(b){
  billing=b;
  document.getElementById('btn-monthly').classList.toggle('active',b==='monthly');
  document.getElementById('btn-annual').classList.toggle('active',b==='annual');
  updateUI();
}
function selectPlan(t){
  tier=t;
  document.getElementById('card-basic').classList.toggle('selected',t==='basic');
  document.getElementById('card-premium').classList.toggle('selected',t==='premium');
  updateUI();
}
function updateUI(){
  ['basic','premium'].forEach(t=>{
    const p=prices[t][billing];
    document.getElementById('price-'+t).innerHTML=p.label+' <span>'+p.per+'</span>';
    document.getElementById('save-'+t).textContent=p.save;
  });
  const p=prices[tier][billing];
  const tname=tier.charAt(0).toUpperCase()+tier.slice(1);
  const bname=billing.charAt(0).toUpperCase()+billing.slice(1);
  document.getElementById('sub-btn').textContent='Start Free Trial — '+tname+' '+bname+' →';
}
// Pre-select tier + billing from ?plan= URL param (used by libertas.mobi deep links)
(function(){
  var params = new URLSearchParams(window.location.search);
  var p = params.get('plan');
  var valid = ['basic_monthly','basic_annual','premium_monthly','premium_annual'];
  if (p && valid.indexOf(p) !== -1) {
    var parts = p.split('_');
    selectPlan(parts[0]);
    setBilling(parts[1]);
  }
})();
function showLogin(){document.getElementById('login-box').style.display='block';}
async function subscribe() {
  const username = document.getElementById('username').value.trim().toLowerCase();
  const display  = document.getElementById('display').value.trim();
  const err = document.getElementById('err');
  err.style.display = 'none';
  if (!username) { err.textContent='Please enter a username.'; err.style.display='block'; return; }
  const btn = document.getElementById('sub-btn');
  btn.disabled=true; btn.textContent='Redirecting to Stripe...';
  const plan = prices[tier][billing].plan;
  try {
    const r = await fetch('/api/stripe/create_checkout', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, display_name: display||username, plan})
    });
    const d = await r.json();
    if (d.checkout_url) { window.location.href=d.checkout_url; }
    else { err.textContent=d.error||'Checkout failed. Try again.'; err.style.display='block'; btn.disabled=false; updateUI(); }
  } catch(e) { err.textContent='Network error. Try again.'; err.style.display='block'; btn.disabled=false; updateUI(); }
}
async function login() {
  const username = document.getElementById('l-user').value.trim().toLowerCase();
  const password = document.getElementById('l-pass').value;
  const err = document.getElementById('login-err');
  err.style.display = 'none';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password})
    });
    if (r.ok) {
      window.location.href = '/premium/';
    } else {
      const d = await r.json();
      err.textContent = d.error || 'Login failed.';
      err.style.display = 'block';
    }
  } catch(e) {
    err.textContent = 'Network error.';
    err.style.display = 'block';
  }
}
</script>
</body></html>"""
    return subscribe



# ---------------------------------------------------------------------------
# OpenClaw Control UI — Nextcloud admin auth gate (used by nginx auth_request)
# ---------------------------------------------------------------------------

@app.route("/auth/nc_admin")
def auth_nc_admin():
    """nginx auth_request endpoint. Returns 200 if caller is a Nextcloud admin, 401 otherwise."""
    from flask import make_response
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("basic "):
        resp = make_response("", 401)
        resp.headers["WWW-Authenticate"] = "Basic realm=\"OpenClaw\""
        return resp
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8", errors="replace")
        username, _, password = decoded.partition(":")
    except Exception:
        return make_response("", 401)
    if not username or not password:
        return make_response("", 401)
    if not _nc_validate_user(username, password):
        resp = make_response("", 401)
        resp.headers["WWW-Authenticate"] = "Basic realm=\"OpenClaw\""
        return resp
    if not _is_admin(username):
        return make_response("", 403)
    return make_response("", 200)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",        type=int, default=9001)
    ap.add_argument("--enable-hold", action="store_true",
                    help="Enable OP25 hold/skip commands to Pi 1 (test carefully)")
    args = ap.parse_args()

    if args.enable_hold:
        HOLD_ENABLED = True
        print("[hold] OP25 hold/skip ENABLED — will send commands to Pi 1", flush=True)
    else:
        print("[hold] OP25 hold/skip DISABLED (run with --enable-hold to activate)", flush=True)

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    load_talkgroups()
    init_db()
    _load_active_incidents_from_db()
    _atak_resync_on_startup()

    print(f"[brain] Battle Buddy v2.0 starting on port {args.port}", flush=True)
    print("[brain] Transcription: faster-whisper large-v3-turbo INT8 (local, offline-ready)", flush=True)
    print(f"[brain] DB: {DB_PATH}", flush=True)

    threading.Thread(target=_get_fw_model,            daemon=True).start()  # warm model at startup
    _atak_mod.startup()
    threading.Thread(target=incident_cleanup_thread,  daemon=True).start()
    threading.Thread(target=hold_watchdog_thread,     daemon=True).start()
    threading.Thread(target=pi_watchdog_thread,       daemon=True).start()
    AFDOpenDataPoller().start()
    threading.Thread(target=traffic_open_data_thread, daemon=True).start()
    threading.Thread(target=atxfloods_thread,         daemon=True).start()
    threading.Thread(target=austin_events_thread,    daemon=True).start()
    threading.Thread(target=apd_cad_thread,          daemon=True).start()
    APDNewsPoller().start()
    threading.Thread(target=reddit_intel_thread,      daemon=True).start()
    threading.Thread(target=adsb_air_asset_thread,    daemon=True).start()

    app.run(host="0.0.0.0", port=args.port, threaded=True)
