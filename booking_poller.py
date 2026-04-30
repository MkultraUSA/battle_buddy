#!/usr/bin/env python3
"""
Battle Buddy — Booking / Arrest Poller
Pulls public arrest records from available sources, stores them locally
with a first_seen timestamp so we can prove when we captured data relative
to media coverage.

Sources:
  - APD (Austin Police): data.austintexas.gov Socrata API  (monthly updates, de-identified)
  - TCSO (Travis County Sheriff): stub — currently WAF-blocked, routed via Pi when available
  - Wilco (Williamson County Sheriff): stub — CSRF-protected HTML, future work

Run:  python3 booking_poller.py
Cron: */6 * * * * /opt/battlebuddy/venv/bin/python3 /opt/battlebuddy/booking_poller.py
"""

import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request

DB_PATH      = os.environ.get("BB_DB", "/opt/battlebuddy/calls.db")
SOCRATA_URL  = "https://data.austintexas.gov/resource/9tem-ywan.json"
SOCRATA_TOKEN = os.environ.get("SOCRATA_TOKEN", "")   # optional app token for higher rate limit
PAGE_SIZE    = 1000
LOG_LEVEL    = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    level=getattr(logging, LOG_LEVEL),
)
log = logging.getLogger("booking_poller")

# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bookings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            first_seen   REAL    NOT NULL,
            source       TEXT    NOT NULL,
            occurred_date TEXT,
            case_number  TEXT,
            charges      TEXT,
            arrest_type  TEXT,
            sector       TEXT,
            agency       TEXT,
            name         TEXT,
            booking_time TEXT,
            raw_json     TEXT,
            UNIQUE(source, case_number)
        );

        CREATE INDEX IF NOT EXISTS bookings_occurred ON bookings(occurred_date);
        CREATE INDEX IF NOT EXISTS bookings_first_seen ON bookings(first_seen);
        CREATE INDEX IF NOT EXISTS bookings_sector ON bookings(sector);
    """)
    conn.commit()


def get_last_occurred_date(conn, source):
    """Return the most recent occurred_date we have for this source."""
    row = conn.execute(
        "SELECT MAX(occurred_date) FROM bookings WHERE source = ?", (source,)
    ).fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# APD Socrata
# ---------------------------------------------------------------------------

def fetch_apd_page(offset, since_date=None):
    params = {
        "$limit":  PAGE_SIZE,
        "$offset": offset,
        "$order":  "occurred_date DESC",
    }
    if since_date:
        params["$where"] = f"occurred_date >= '{since_date}'"
    if SOCRATA_TOKEN:
        params["$$app_token"] = SOCRATA_TOKEN

    url = SOCRATA_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BattleBuddy/1.0 (research project)",
            "Accept":     "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def poll_apd(conn):
    source = "apd_socrata"
    since  = get_last_occurred_date(conn, source)
    log.info(f"[APD] polling Socrata (since={since or 'beginning'})")

    new_count = 0
    offset    = 0

    while True:
        try:
            records = fetch_apd_page(offset, since_date=since)
        except Exception as e:
            log.error(f"[APD] fetch error at offset {offset}: {e}")
            break

        if not records:
            break

        now = time.time()
        for r in records:
            case_num = r.get("case_report_number")
            if not case_num:
                continue
            occurred = r.get("occurred_date", "")[:10]  # date only
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO bookings
                       (first_seen, source, occurred_date, case_number, charges,
                        arrest_type, sector, agency, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        now, source, occurred, case_num,
                        r.get("arrest_charges"),
                        r.get("arrest_type_description"),
                        r.get("arrest_sector"),
                        "APD",
                        json.dumps(r),
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    new_count += 1
            except sqlite3.Error as e:
                log.error(f"[APD] DB error: {e}")

        conn.commit()
        log.debug(f"[APD] processed offset {offset}, got {len(records)} records")

        if len(records) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.5)  # polite paging

    log.info(f"[APD] done — {new_count} new records stored")
    return new_count


# ---------------------------------------------------------------------------
# TCSO stub (Travis County Sheriff — WAF-blocked, routes via Pi relay)
# ---------------------------------------------------------------------------

def poll_tcso(conn):
    """
    Placeholder for TCSO live jail roster.
    The SIPS SOAP endpoint (public.traviscountytx.gov/sipspublicsvc/sipsvc.asmx)
    is blocked from all external IPs including residential.

    Two approaches to try when we revisit:
      1. Playwright headless browser on the Pi — simulates the Angular SPA
         and captures the XHR calls it makes to the backend.
      2. CSRF token + session scrape of wilco.org (for Williamson County
         as a proxy source that's on the same GATRRS system).

    When implemented, this function should:
      - SSH to Pi or call Pi relay endpoint
      - Parse booking records with name, booking_time, charges, bond
      - INSERT OR IGNORE into bookings table with source='tcso'
    """
    log.debug("[TCSO] stub — not yet implemented (WAF blocked)")


# ---------------------------------------------------------------------------
# Correlation helper
# ---------------------------------------------------------------------------

def correlate_booking_to_incident(conn, case_number, source="apd_socrata"):
    """
    Given a booking case number, find any radio incidents within ±4 hours
    on the same day from the same sector/agency.
    Returns list of matching incident dicts.
    """
    booking = conn.execute(
        "SELECT occurred_date, sector, charges, first_seen FROM bookings WHERE source=? AND case_number=?",
        (source, case_number)
    ).fetchone()
    if not booking:
        return []

    occurred_date, sector, charges, first_seen = booking

    # Map APD sector letter to approximate category
    sector_to_category = {
        "A": "APD", "B": "APD", "C": "APD", "D": "APD",
        "E": "APD", "F": "APD", "G": "APD", "H": "APD",
    }
    category = sector_to_category.get(sector, "APD")

    # Look for calls within 12 hours of arrest date in matching category
    day_start = occurred_date + "T00:00:00"
    day_end   = occurred_date + "T23:59:59"

    rows = conn.execute(
        """SELECT id, datetime(ts,'unixepoch','localtime') as t, tag, transcript
           FROM calls
           WHERE category = ?
             AND datetime(ts,'unixepoch') BETWEEN ? AND ?
           ORDER BY ts""",
        (category, day_start, day_end)
    ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("booking_poller starting")
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    new_apd  = poll_apd(conn)
    poll_tcso(conn)   # stub

    log.info(f"booking_poller done — apd_new={new_apd}")
    conn.close()


if __name__ == "__main__":
    main()
