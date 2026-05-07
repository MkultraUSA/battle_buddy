#!/usr/bin/env python3
"""
Battle Buddy Data Visibility Tool
==================================
Query and analyze data collected by the Battle Buddy system.

Usage:
    bb-data calls [--limit N] [--since DAYS] [--tgid ID] [--location LOC]
    bb-data activity [--limit N] [--since DAYS] [--tgid ID]
    bb-data incidents [--limit N] [--since DAYS] [--status STATUS] [--type TYPE]
    bb-data events [--limit N] [--since DAYS]
    bb-data aircraft [--limit N] [--since DAYS] [--icao CODE]
    bb-data bookings [--limit N] [--since DAYS] [--sector SEC]
    bb-data tips [--limit N] [--status STATUS]
    bb-data tgid [--limit N] [--tgid ID]
    bb-data stats
    bb-data live [--seconds N]
    bb-data search TEXT
    bb-data export TABLE FORMAT [--since DAYS]

Tables: calls, activity, incidents, events, aircraft_positions, bookings,
        tips, tgid_guesses, apd_cad, reddit_intel, geocode_cache, drone_sightings
Formats: csv, json
"""

import sqlite3
import sys
import os
import time
import argparse
from datetime import datetime, timedelta

# ── Paths ──────────────────────────────────────────────────────────────────────
CALLS_DB = "/opt/battlebuddy/calls.db"
ACTIVITY_DB = "/opt/battlebuddy/activity.db"

# ── Helpers ───────────────────────────────────────────────────────────────────
def connect(db_path):
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def since_clause(days, col="ts"):
    if days is None:
        return "", []
    cutoff = time.time() - (days * 86400)
    return f" WHERE {col} >= ?", [cutoff]

def fmt_ts(ts):
    if ts is None:
        return "N/A"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return str(ts)

def fmt_coords(lat, lon):
    if lat and lon:
        return f"{lat:.5f},{lon:.5f}"
    return "N/A"

def print_table(headers, rows, max_col_width=60):
    """Pretty-print a table using tabulate if available, else simple format."""
    try:
        from tabulate import tabulate
        truncated = []
        for row in rows:
            truncated.append([str(c)[:max_col_width] if c is not None else "" for c in row])
        print(tabulate(truncated, headers=headers, tablefmt="simple_outline"))
    except ImportError:
        # Fallback: simple aligned output
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)[:max_col_width]))
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        print("  ".join("-" * w for w in widths))
        for row in rows:
            print(fmt.format(*[str(c)[:max_col_width] if c is not None else "" for c in row]))

def count_rows(conn, table, where="", params=None):
    q = f"SELECT COUNT(*) FROM {table}"
    if where:
        q += f" {where}"
    return conn.execute(q, params or []).fetchone()[0]

# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_calls(args):
    conn = connect(CALLS_DB)
    where = ""
    params = []
    conditions = []

    if args.since:
        conditions.append("ts >= ?")
        params.append(time.time() - args.since * 86400)
    if args.tgid:
        conditions.append("tgid = ?")
        params.append(args.tgid)
    if args.location:
        conditions.append("location LIKE ?")
        params.append(f"%{args.location}%")

    if conditions:
        where = " WHERE " + " AND ".join(conditions)

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT id, ts, tgid, tag, category, duration, transcript, location, lat, lon "
        f"FROM calls{where} ORDER BY ts DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["ID", "Timestamp", "TGID", "Tag", "Category", "Duration", "Transcript", "Location", "Coords"]
    data = []
    for r in rows:
        data.append([
            r["id"], fmt_ts(r["ts"]), r["tgid"], r["tag"], r["category"],
            f"{r['duration']:.1f}s" if r["duration"] else "-",
            (r["transcript"] or "")[:80],
            r["location"] or "-",
            fmt_coords(r["lat"], r["lon"])
        ])
    print(f"\n📞 CALLS ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_activity(args):
    conn = connect(ACTIVITY_DB)
    where = ""
    params = []
    conditions = []

    if args.since:
        conditions.append("ts >= ?")
        params.append(time.time() - args.since * 86400)
    if args.tgid:
        conditions.append("tgid = ?")
        params.append(args.tgid)

    if conditions:
        where = " WHERE " + " AND ".join(conditions)

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT ts, tgid, tag, freq, srcaddr, encrypted, counter, node "
        f"FROM activity{where} ORDER BY ts DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["Timestamp", "TGID", "Tag", "Frequency", "SrcAddr", "Encrypted", "Count", "Node"]
    data = []
    for r in rows:
        data.append([
            fmt_ts(r["ts"]), r["tgid"], r["tag"],
            f"{r['freq']/1e6:.3f} MHz" if r["freq"] else "-",
            r["srcaddr"], "🔒" if r["encrypted"] else "📻",
            r["counter"], r["node"]
        ])
    print(f"\n📡 ACTIVITY ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_incidents(args):
    conn = connect(CALLS_DB)
    where = ""
    params = []
    conditions = []

    if args.since:
        conditions.append("ts_start >= ?")
        params.append(time.time() - args.since * 86400)
    if args.status:
        conditions.append("status = ?")
        params.append(args.status)
    if args.type:
        conditions.append("itype LIKE ?")
        params.append(f"%{args.type}%")

    if conditions:
        where = " WHERE " + " AND ".join(conditions)

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT id, ts_start, ts_cleared, itype, description, agencies, "
        f"location, lat, lon, status, flagged "
        f"FROM incidents{where} ORDER BY ts_start DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["ID", "Started", "Cleared", "Type", "Description", "Agencies", "Location", "Status"]
    data = []
    for r in rows:
        status_icon = {"active": "🔴", "cleared": "✅"}.get(r["status"], "⚪")
        data.append([
            r["id"], fmt_ts(r["ts_start"]), fmt_ts(r["ts_cleared"]),
            r["itype"], (r["description"] or "")[:60],
            r["agencies"] or "-", (r["location"] or "-")[:40],
            f"{status_icon} {r['status']}"
        ])
    print(f"\n🚨 INCIDENTS ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_events(args):
    conn = connect(ACTIVITY_DB)
    where = ""
    params = []

    if args.since:
        where = " WHERE ts >= ?"
        params = [time.time() - args.since * 86400]

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT ts, event_type, description, tgids, node "
        f"FROM events{where} ORDER BY ts DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["Timestamp", "Event Type", "Description", "TGIDs", "Node"]
    data = []
    for r in rows:
        data.append([
            fmt_ts(r["ts"]), r["event_type"],
            (r["description"] or "")[:80],
            r["tgids"] or "-", r["node"] or "-"
        ])
    print(f"\n📋 EVENTS ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_aircraft(args):
    conn = connect(CALLS_DB)
    where = ""
    params = []
    conditions = []

    if args.since:
        conditions.append("ts >= ?")
        params.append(time.time() - args.since * 86400)
    if args.icao:
        conditions.append("icao24 = ?")
        params.append(args.icao.upper())

    if conditions:
        where = " WHERE " + " AND ".join(conditions)

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT ts, icao24, callsign, lat, lon, alt_ft, heading, speed_kts, is_leo, label "
        f"FROM aircraft_positions{where} ORDER BY ts DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["Timestamp", "ICAO24", "Callsign", "Lat", "Lon", "Alt(ft)", "Hdg", "Spd(kts)", "LEO", "Label"]
    data = []
    for r in rows:
        data.append([
            fmt_ts(r["ts"]), r["icao24"], r["callsign"] or "-",
            f"{r['lat']:.5f}" if r["lat"] else "-",
            f"{r['lon']:.5f}" if r["lon"] else "-",
            r["alt_ft"] or "-", r["heading"] or "-",
            r["speed_kts"] or "-",
            "🛰️" if r["is_leo"] else "",
            r["label"] or "-"
        ])
    print(f"\n✈️  AIRCRAFT ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_bookings(args):
    conn = connect(CALLS_DB)
    where = ""
    params = []
    conditions = []

    if args.since:
        conditions.append("first_seen >= ?")
        params.append(time.time() - args.since * 86400)
    if args.sector:
        conditions.append("sector = ?")
        params.append(args.sector)

    if conditions:
        where = " WHERE " + " AND ".join(conditions)

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT id, first_seen, source, occurred_date, case_number, charges, "
        f"sector, agency, name "
        f"FROM bookings{where} ORDER BY first_seen DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["ID", "First Seen", "Source", "Occurred", "Case #", "Charges", "Sector", "Agency", "Name"]
    data = []
    for r in rows:
        data.append([
            r["id"], fmt_ts(r["first_seen"]), r["source"],
            r["occurred_date"] or "-", r["case_number"] or "-",
            (r["charges"] or "-")[:40], r["sector"] or "-",
            r["agency"] or "-", r["name"] or "-"
        ])
    print(f"\n🔒 BOOKINGS ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_tips(args):
    conn = connect(CALLS_DB)
    where = ""
    params = []

    if args.status:
        where = " WHERE status = ?"
        params = [args.status]

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT id, ts, location_text, description, status, source, incident_id "
        f"FROM tips{where} ORDER BY ts DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["ID", "Timestamp", "Location", "Description", "Status", "Source", "Incident"]
    data = []
    for r in rows:
        data.append([
            r["id"], fmt_ts(r["ts"]), r["location_text"] or "-",
            (r["description"] or "")[:60], r["status"],
            r["source"], r["incident_id"] or "-"
        ])
    print(f"\n💡 TIPS ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_tgid(args):
    conn = connect(CALLS_DB)
    where = ""
    params = []

    if args.tgid:
        where = " WHERE tgid = ?"
        params = [args.tgid]

    limit = args.limit or 20
    rows = conn.execute(
        f"SELECT id, tgid, ts, guess, category, confidence, reasoning, confirmed "
        f"FROM tgid_guesses{where} ORDER BY ts DESC LIMIT ?",
        params + [limit]
    ).fetchall()

    headers = ["ID", "TGID", "Timestamp", "Guess", "Category", "Confidence", "Reasoning", "Confirmed"]
    data = []
    for r in rows:
        data.append([
            r["id"], r["tgid"], fmt_ts(r["ts"]), r["guess"],
            r["category"] or "-", r["confidence"] or "-",
            (r["reasoning"] or "")[:50], "✅" if r["confirmed"] else "❓"
        ])
    print(f"\n🔍 TGID GUESSES ({len(rows)} shown)\n")
    print_table(headers, data)
    conn.close()

def cmd_stats(args):
    """Show overall system statistics."""
    calls_conn = connect(CALLS_DB)
    act_conn = connect(ACTIVITY_DB)

    now = time.time()
    day_ago = now - 86400
    week_ago = now - 7 * 86400

    stats = {}

    # Calls stats
    stats["total_calls"] = count_rows(calls_conn, "calls")
    stats["calls_24h"] = calls_conn.execute("SELECT COUNT(*) FROM calls WHERE ts >= ?", [day_ago]).fetchone()[0]
    stats["calls_7d"] = calls_conn.execute("SELECT COUNT(*) FROM calls WHERE ts >= ?", [week_ago]).fetchone()[0]
    stats["unique_tgids"] = calls_conn.execute("SELECT COUNT(DISTINCT tgid) FROM calls WHERE tgid > 0").fetchone()[0]

    # Activity stats
    stats["total_activity"] = count_rows(act_conn, "activity")
    stats["activity_24h"] = act_conn.execute("SELECT COUNT(*) FROM activity WHERE ts >= ?", [day_ago]).fetchone()[0]
    stats["activity_7d"] = act_conn.execute("SELECT COUNT(*) FROM activity WHERE ts >= ?", [week_ago]).fetchone()[0]

    # Events
    stats["total_events"] = count_rows(act_conn, "events")
    stats["events_24h"] = act_conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ?", [day_ago]).fetchone()[0]

    # Incidents
    stats["total_incidents"] = count_rows(calls_conn, "incidents")
    stats["active_incidents"] = calls_conn.execute("SELECT COUNT(*) FROM incidents WHERE status='active'").fetchone()[0]
    stats["incidents_24h"] = calls_conn.execute("SELECT COUNT(*) FROM incidents WHERE ts_start >= ?", [day_ago]).fetchone()[0]
    stats["incidents_7d"] = calls_conn.execute("SELECT COUNT(*) FROM incidents WHERE ts_start >= ?", [week_ago]).fetchone()[0]

    # Other tables
    stats["aircraft_positions"] = count_rows(calls_conn, "aircraft_positions")
    stats["bookings"] = count_rows(calls_conn, "bookings")
    stats["tips"] = count_rows(calls_conn, "tips")
    stats["apd_cad"] = count_rows(calls_conn, "apd_cad")
    stats["reddit_intel"] = count_rows(calls_conn, "reddit_intel")
    stats["drone_sightings"] = count_rows(calls_conn, "drone_sightings")
    stats["geocode_cache"] = count_rows(calls_conn, "geocode_cache")
    stats["tgid_guesses"] = count_rows(calls_conn, "tgid_guesses")

    # Top talkgroups
    top_tgids = calls_conn.execute(
        "SELECT tgid, tag, COUNT(*) as cnt FROM calls WHERE tgid > 0 GROUP BY tgid ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    # Top incident types
    top_inc_types = calls_conn.execute(
        "SELECT itype, COUNT(*) as cnt FROM incidents GROUP BY itype ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    # DB file sizes
    calls_size = os.path.getsize(CALLS_DB) / (1024*1024)
    activity_size = os.path.getsize(ACTIVITY_DB) / (1024*1024)

    print(f"""
╔══════════════════════════════════════════════════════════╗
║           BATTLE BUDDY — SYSTEM STATISTICS              ║
╠══════════════════════════════════════════════════════════╣
║  Generated: {fmt_ts(now):<47} ║
╠══════════════════════════════════════════════════════════╣
║  📞 CALLS                                                ║
║     Total:        {stats['total_calls']:>10,}                          ║
║     Last 24h:     {stats['calls_24h']:>10,}                          ║
║     Last 7d:      {stats['calls_7d']:>10,}                          ║
║     Unique TGIDs: {stats['unique_tgids']:>10,}                          ║
╠══════════════════════════════════════════════════════════╣
║  📡 ACTIVITY                                             ║
║     Total:        {stats['total_activity']:>10,}                          ║
║     Last 24h:     {stats['activity_24h']:>10,}                          ║
║     Last 7d:      {stats['activity_7d']:>10,}                          ║
╠══════════════════════════════════════════════════════════╣
║  🚨 INCIDENTS                                            ║
║     Total:        {stats['total_incidents']:>10,}                          ║
║     Active:       {stats['active_incidents']:>10,}                          ║
║     Last 24h:     {stats['incidents_24h']:>10,}                          ║
║     Last 7d:      {stats['incidents_7d']:>10,}                          ║
╠══════════════════════════════════════════════════════════╣
║  📋 EVENTS                                               ║
║     Total:        {stats['total_events']:>10,}                          ║
║     Last 24h:     {stats['events_24h']:>10,}                          ║
╠══════════════════════════════════════════════════════════╣
║  📦 OTHER DATA                                           ║
║     Aircraft:     {stats['aircraft_positions']:>10,}                          ║
║     Bookings:     {stats['bookings']:>10,}                          ║
║     Tips:         {stats['tips']:>10,}                          ║
║     APD CAD:      {stats['apd_cad']:>10,}                          ║
║     Reddit Intel: {stats['reddit_intel']:>10,}                          ║
║     Drones:       {stats['drone_sightings']:>10,}                          ║
║     Geocodes:     {stats['geocode_cache']:>10,}                          ║
║     TGID Guesses: {stats['tgid_guesses']:>10,}                          ║
╠══════════════════════════════════════════════════════════╣
║  💾 DATABASES                                            ║
║     calls.db:     {calls_size:>8.1f} MB                            ║
║     activity.db:  {activity_size:>8.1f} MB                            ║
╠══════════════════════════════════════════════════════════╣
║  🏆 TOP 10 TALKGROUPS (by call count)                    ║""")

    for t in top_tgids:
        tag = (t["tag"] or "Unknown")[:25]
        print(f"║     TG {t['tgid']:>6}  {tag:<25} {t['cnt']:>8,}  ║")

    print("╠══════════════════════════════════════════════════════════╣")
    print("║  🔥 TOP INCIDENT TYPES                                  ║")

    for t in top_inc_types:
        itype = (t["itype"] or "Unknown")[:35]
        print(f"║     {itype:<35} {t['cnt']:>8,}  ║")

    print("╚══════════════════════════════════════════════════════════╝")

    calls_conn.close()
    act_conn.close()

def cmd_live(args):
    """Watch live data flowing in."""
    seconds = args.seconds or 30
    calls_conn = connect(CALLS_DB)
    act_conn = connect(ACTIVITY_DB)

    last_call_id = calls_conn.execute("SELECT MAX(id) FROM calls").fetchone()[0] or 0
    last_act_id = act_conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] or 0

    print(f"👁️  LIVE MODE — watching for {seconds}s (Ctrl+C to stop)")
    print(f"   Baseline: calls={last_call_id}, activity={last_act_id}\n")

    start = time.time()
    try:
        while time.time() - start < seconds:
            time.sleep(2)

            new_calls = calls_conn.execute(
                "SELECT id, ts, tgid, tag, category, duration, transcript FROM calls WHERE id > ? ORDER BY id DESC LIMIT 5",
                [last_call_id]
            ).fetchall()

            if new_calls:
                for r in reversed(new_calls):
                    print(f"  📞 [{fmt_ts(r['ts'])}] TG{r['tgid']} {r['tag']} ({r['category']}) "
                          f"dur={r['duration']:.1f}s  {(r['transcript'] or '')[:60]}")
                    last_call_id = max(last_call_id, r["id"])

            new_events = act_conn.execute(
                "SELECT ts, event_type, description FROM events WHERE ts > ? ORDER BY ts DESC LIMIT 3",
                [time.time() - 5]
            ).fetchall()

            if new_events:
                for r in new_events:
                    print(f"  📋 [{fmt_ts(r['ts'])}] {r['event_type']}: {(r['description'] or '')[:60]}")

    except KeyboardInterrupt:
        pass

    print(f"\n   Done. Last call ID: {last_call_id}")
    calls_conn.close()
    act_conn.close()

def cmd_search(args):
    """Full-text search across calls, incidents, and events."""
    text = args.text
    calls_conn = connect(CALLS_DB)
    act_conn = connect(ACTIVITY_DB)

    print(f"\n🔍 Searching for: '{text}'\n")

    # Search calls
    call_rows = calls_conn.execute(
        "SELECT id, ts, tgid, tag, transcript, location FROM calls "
        "WHERE transcript LIKE ? OR tag LIKE ? OR location LIKE ? "
        "ORDER BY ts DESC LIMIT 10",
        [f"%{text}%"] * 3
    ).fetchall()

    if call_rows:
        print(f"📞 CALLS ({len(call_rows)} results):")
        for r in call_rows:
            print(f"   [{fmt_ts(r['ts'])}] TG{r['tgid']} {r['tag']}: "
                  f"{(r['transcript'] or '')[:80]}")
        print()

    # Search incidents
    inc_rows = calls_conn.execute(
        "SELECT id, ts_start, itype, description, location FROM incidents "
        "WHERE description LIKE ? OR location LIKE ? OR itype LIKE ? "
        "ORDER BY ts_start DESC LIMIT 10",
        [f"%{text}%"] * 3
    ).fetchall()

    if inc_rows:
        print(f"🚨 INCIDENTS ({len(inc_rows)} results):")
        for r in inc_rows:
            print(f"   [{fmt_ts(r['ts_start'])}] {r['itype']}: "
                  f"{(r['description'] or '')[:80]}")
        print()

    # Search events
    evt_rows = act_conn.execute(
        "SELECT ts, event_type, description FROM events "
        "WHERE description LIKE ? OR event_type LIKE ? "
        "ORDER BY ts DESC LIMIT 10",
        [f"%{text}%"] * 2
    ).fetchall()

    if evt_rows:
        print(f"📋 EVENTS ({len(evt_rows)} results):")
        for r in evt_rows:
            print(f"   [{fmt_ts(r['ts'])}] {r['event_type']}: "
                  f"{(r['description'] or '')[:80]}")
        print()

    if not call_rows and not inc_rows and not evt_rows:
        print("   No results found.")

    calls_conn.close()
    act_conn.close()

def cmd_export(args):
    """Export table data to CSV or JSON."""
    table = args.table
    fmt = args.format
    since = args.since

    # Determine which DB
    calls_tables = {"calls", "incidents", "incident_calls", "incident_escalations",
                    "tgid_guesses", "bookings", "drone_sightings", "tips",
                    "aircraft_positions", "apd_cad", "reddit_intel", "geocode_cache",
                    "incident_articles", "intel_queries", "premium_users", "sessions",
                    "apd_seen", "tgid_sector_hints"}
    act_tables = {"activity", "events"}

    if table in calls_tables:
        conn = connect(CALLS_DB)
    elif table in act_tables:
        conn = connect(ACTIVITY_DB)
    else:
        print(f"ERROR: Unknown table '{table}'", file=sys.stderr)
        sys.exit(1)

    where = ""
    params = []
    if since:
        ts_col = "ts" if table in act_tables else "ts"
        # Try common timestamp columns
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
        for candidate in ["ts", "ts_start", "first_seen", "response_ts"]:
            if candidate in cols:
                ts_col = candidate
                break
        where = f" WHERE {ts_col} >= ?"
        params = [time.time() - since * 86400]

    rows = conn.execute(f"SELECT * FROM {table}{where} ORDER BY rowid DESC", params).fetchall()
    headers = [d[0] for d in rows[0].keys()] if rows else []

    if fmt == "csv":
        import csv
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        for r in rows:
            writer.writerow([r[h] for h in headers])
    elif fmt == "json":
        import json
        data = [dict(r) for r in rows]
        # Convert timestamps
        print(json.dumps(data, indent=2, default=str))

    conn.close()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Battle Buddy Data Visibility Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = parser.add_subparsers(dest="command")

    # Common args
    def add_common(p):
        p.add_argument("--limit", "-l", type=int, default=20, help="Max rows (default: 20)")
        p.add_argument("--since", "-s", type=float, help="Days ago filter")

    # calls
    p_calls = sub.add_parser("calls", help="Query radio calls")
    add_common(p_calls)
    p_calls.add_argument("--tgid", type=int, help="Filter by talkgroup ID")
    p_calls.add_argument("--location", help="Filter by location text")

    # activity
    p_act = sub.add_parser("activity", help="Query radio activity")
    add_common(p_act)
    p_act.add_argument("--tgid", type=int, help="Filter by talkgroup ID")

    # incidents
    p_inc = sub.add_parser("incidents", help="Query incidents")
    add_common(p_inc)
    p_inc.add_argument("--status", choices=["active", "cleared"], help="Filter by status")
    p_inc.add_argument("--type", help="Filter by incident type")

    # events
    p_evt = sub.add_parser("events", help="Query system events")
    add_common(p_evt)

    # aircraft
    p_ac = sub.add_parser("aircraft", help="Query aircraft positions")
    add_common(p_ac)
    p_ac.add_argument("--icao", help="Filter by ICAO24 code")

    # bookings
    p_bk = sub.add_parser("bookings", help="Query jail bookings")
    add_common(p_bk)
    p_bk.add_argument("--sector", help="Filter by sector")

    # tips
    p_tips = sub.add_parser("tips", help="Query tips")
    add_common(p_tips)
    p_tips.add_argument("--status", help="Filter by status")

    # tgid
    p_tgid = sub.add_parser("tgid", help="Query TGID guesses")
    add_common(p_tgid)
    p_tgid.add_argument("--tgid", type=int, help="Filter by TGID")

    # stats
    sub.add_parser("stats", help="Show system statistics")

    # live
    p_live = sub.add_parser("live", help="Watch live data")
    p_live.add_argument("--seconds", "-n", type=int, default=30, help="Watch duration")

    # search
    p_search = sub.add_parser("search", help="Full-text search")
    p_search.add_argument("text", help="Search text")

    # export
    p_export = sub.add_parser("export", help="Export data")
    p_export.add_argument("table", help="Table name")
    p_export.add_argument("format", choices=["csv", "json"], help="Output format")
    p_export.add_argument("--since", "-s", type=float, help="Days ago filter")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "calls": cmd_calls,
        "activity": cmd_activity,
        "incidents": cmd_incidents,
        "events": cmd_events,
        "aircraft": cmd_aircraft,
        "bookings": cmd_bookings,
        "tips": cmd_tips,
        "tgid": cmd_tgid,
        "stats": cmd_stats,
        "live": cmd_live,
        "search": cmd_search,
        "export": cmd_export,
    }

    dispatch[args.command](args)

if __name__ == "__main__":
    main()
