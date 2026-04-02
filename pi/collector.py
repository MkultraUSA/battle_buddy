#!/usr/bin/env python3
"""
Battle Buddy — OP25 Talkgroup Collector
Polls OP25 HTTP interface every 5 seconds, extracts active talkgroup
info from voice frequency data, and logs call activity to activity.db.
"""

import json
import os
import sqlite3
import time
import urllib.request

OP25_URL  = os.environ.get("OP25_URL",  "http://127.0.0.1:8080/")
DB_PATH   = os.environ.get("ACTIVITY_DB", "/home/pi/op25_data/activity.db")
POLL_SECS = 5

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tg_activity (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL    NOT NULL,
            tgid    INTEGER,
            tag     TEXT,
            freq    REAL,
            age_s   REAL
        )
    """)
    conn.commit()
    return conn


def log_activity(conn, tgid, tag, freq, age_s):
    conn.execute(
        "INSERT INTO tg_activity (ts, tgid, tag, freq, age_s) VALUES (?,?,?,?,?)",
        (time.time(), tgid, tag, freq, age_s),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# OP25 poll
# ---------------------------------------------------------------------------

def poll_op25():
    """POST empty command, return list of active voice channels."""
    try:
        req = urllib.request.Request(
            OP25_URL,
            data=b"[]",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            messages = json.loads(resp.read())
    except Exception:
        return []

    active = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        freq_data = msg.get("frequency_data", {})
        for freq_str, finfo in freq_data.items():
            if not isinstance(finfo, dict):
                continue
            if finfo.get("type") != "voice":
                continue
            try:
                age = float(finfo.get("last_activity", 999))
            except (ValueError, TypeError):
                age = 999.0
            if age > 30:  # only log recently active (within 30s)
                continue
            tgids = finfo.get("tgids", [None, None])
            tgid  = tgids[0] if tgids else None
            if tgid is None:
                continue
            try:
                freq = float(freq_str) / 1e6
            except (ValueError, TypeError):
                freq = 0.0
            active.append((int(tgid), freq, age))
    return active


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"[collector] starting — OP25={OP25_URL}  DB={DB_PATH}", flush=True)
    conn = init_db()
    seen_recent: dict[int, float] = {}   # tgid → last logged ts

    while True:
        active = poll_op25()
        now = time.time()

        for tgid, freq, age in active:
            last_logged = seen_recent.get(tgid, 0)
            if now - last_logged < 10:  # don't spam same tgid within 10s
                continue
            seen_recent[tgid] = now
            tag = f"TGID {tgid}"

            # Purge old entries from seen_recent to avoid unbounded growth
            cutoff = now - 120
            seen_recent = {k: v for k, v in seen_recent.items() if v > cutoff}

            try:
                log_activity(conn, tgid, tag, freq, age)
                print(
                    f"[{time.strftime('%H:%M:%S')}] tgid={tgid} freq={freq:.4f}MHz age={age:.1f}s",
                    flush=True,
                )
            except sqlite3.Error as exc:
                print(f"[collector] DB error: {exc}", flush=True)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = init_db()

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
