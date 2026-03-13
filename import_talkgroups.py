#!/usr/bin/env python3
"""
Import GATRRS talkgroup CSV from RadioReference into battle_buddy DB.

Source: trs_tg_2.csv (exported from RadioReference, System ID 2)
Columns: Decimal, Hex, Alpha Tag, Mode, Description, Tag, Category

Usage:
    python3 import_talkgroups.py [--csv path/to/trs_tg_2.csv]
"""

import argparse
import csv
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))
from battle_buddy_db import BattleBuddyDB

DEFAULT_CSV = Path(__file__).parent / "user_recordings" / "trs_tg_2.csv"
DB_PATH     = Path(__file__).parent / "logs" / "battle_buddy.db"


def main():
    ap = argparse.ArgumentParser(description="Import GATRRS talkgroups from RadioReference CSV")
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to trs_tg_2.csv")
    ap.add_argument("--db",  default=str(DB_PATH),     help="Path to battle_buddy.db")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    db = BattleBuddyDB(args.db)

    ok = 0
    skip = 0
    err = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tg_id_raw = row.get("Decimal", "").strip()
            if not tg_id_raw:
                skip += 1
                continue

            try:
                tg_id = int(tg_id_raw)
            except ValueError:
                print(f"  SKIP bad Decimal: {tg_id_raw!r}")
                skip += 1
                continue

            tg = {
                "talkgroup_id":  tg_id,
                "alpha_tag":     row.get("Alpha Tag", "").strip() or None,
                "description":   row.get("Description", "").strip() or None,
                "mode":          row.get("Mode", "").strip() or None,
                "tag":           row.get("Tag", "").strip() or None,
                "category":      row.get("Category", "").strip() or None,
                "coverage_area": "GATRRS",
                "lat_center":    None,
                "lon_center":    None,
            }

            try:
                db.upsert_talkgroup(tg)
                ok += 1
            except Exception as e:
                print(f"  ERROR tg {tg_id}: {e}")
                err += 1

    db.conn.commit()

    print(f"Done — {ok} upserted, {skip} skipped, {err} errors")
    print(f"DB: {args.db}")


if __name__ == "__main__":
    main()
