#!/usr/bin/env python3
"""Nightly classification review (read-only).

Scans incidents from the last 24 hours and flags weak ones based on
modules/classification_config.json:

  - single-call incidents (when the incident type requires more)
  - unknown / blank location
  - tgid not in the trusted_tgids list for that incident type

Always exits 0. Does not write to the database.

Run from /opt/battlebuddy:
    python3 scripts/nightly_review.py
"""

import json
import os
import sqlite3
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("BB_DB_PATH", os.path.join(REPO_ROOT, "calls.db"))
CONFIG_PATH = os.path.join(REPO_ROOT, "modules", "classification_config.json")

LOOKBACK_SECS = 24 * 3600


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[nightly_review] WARN could not load config: {exc}", file=sys.stderr)
        return {}


def parse_tgids(raw):
    if not raw:
        return []
    out = []
    for tok in str(raw).replace(",", " ").split():
        try:
            out.append(int(tok))
        except ValueError:
            pass
    return out


def main():
    config = load_config()
    cutoff = time.time() - LOOKBACK_SECS

    if not os.path.exists(DB_PATH):
        print(f"[nightly_review] db not found at {DB_PATH}", file=sys.stderr)
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT id, ts_start, itype, description, tgids, location, status "
        "FROM incidents WHERE ts_start >= ? ORDER BY ts_start DESC",
        (cutoff,),
    )
    incidents = cur.fetchall()

    flagged = []
    for inc in incidents:
        itype = (inc["itype"] or "").strip()
        rules = config.get(itype, {})
        tgids = parse_tgids(inc["tgids"])
        location = (inc["location"] or "").strip()

        # call count for this incident
        cur.execute(
            "SELECT COUNT(*) FROM incident_calls WHERE incident_id = ?",
            (inc["id"],),
        )
        call_count = cur.fetchone()[0]

        # latest transcript snippet
        cur.execute(
            "SELECT c.transcript, c.tgid, c.tag FROM incident_calls ic "
            "JOIN calls c ON c.id = ic.call_id "
            "WHERE ic.incident_id = ? ORDER BY c.ts DESC LIMIT 1",
            (inc["id"],),
        )
        row = cur.fetchone()
        transcript = (row["transcript"] if row else "") or ""
        primary_tgid = (row["tgid"] if row else (tgids[0] if tgids else 0))

        reasons = []

        min_calls = int(rules.get("required_min_calls", 1) or 1)
        if call_count < min_calls:
            reasons.append(
                f"only {call_count} call(s); type requires >= {min_calls}"
            )

        if not location or location.lower() in ("unknown", "none"):
            reasons.append("unknown/blank location")

        trusted = rules.get("trusted_tgids") or []
        if trusted:
            offending = [t for t in tgids if t not in trusted]
            if not tgids or offending:
                reasons.append(
                    f"tgid(s) {tgids or '[]'} not in trusted list {trusted} for {itype!r}"
                )

        excluded_kw = rules.get("keywords_excluded") or []
        tx_lower = transcript.lower()
        hit_kw = [kw for kw in excluded_kw if kw.lower() in tx_lower]
        if hit_kw:
            reasons.append(f"transcript contains excluded jargon: {hit_kw}")

        if reasons:
            flagged.append({
                "id": inc["id"],
                "itype": itype,
                "tgid": primary_tgid,
                "tgids": tgids,
                "location": location or "(none)",
                "call_count": call_count,
                "transcript": transcript[:240],
                "reasons": reasons,
            })

    print("=" * 72)
    print("Battle Buddy nightly classification review")
    print(f"Window: last {LOOKBACK_SECS // 3600}h  (since {time.ctime(cutoff)})")
    print(f"Incidents scanned: {len(incidents)}   flagged: {len(flagged)}")
    print("=" * 72)

    for f in flagged:
        print(
            f"\n#{f['id']}  itype={f['itype']!r}  tgid={f['tgid']}  "
            f"calls={f['call_count']}  loc={f['location']!r}"
        )
        print(f"  transcript: {f['transcript']!r}")
        for r in f["reasons"]:
            print(f"  - {r}")

    if not flagged:
        print("\n(no weak incidents in window)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
