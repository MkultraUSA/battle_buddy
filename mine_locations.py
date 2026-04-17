#!/usr/bin/env python3
"""
mine_locations.py — scan APD/TCSO transcripts for recurring place names
that could be added to LOCATION_HINTS.

Run anytime: python3 /opt/battlebuddy/mine_locations.py
More useful after a few days of data.
"""

import re
import sqlite3
from collections import Counter

DB = "/opt/battlebuddy/calls.db"

# Patterns that suggest a real location reference
CROSS_STREET = re.compile(
    r'\b([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s+(?:and|&)\s+([A-Z][a-z]+(?: [A-Z][a-z]+)*)\b'
)
HIGHWAY = re.compile(
    r'\b(?:highway|hwy|fm|loop|toll|us|us-|sh-?|tx-?|ranch road|rr)\s*(\d{1,4})\b',
    re.IGNORECASE
)
AT_LOCATION = re.compile(
    r'\b(?:at|near|on|northbound|southbound|eastbound|westbound|nb|sb|eb|wb)\s+'
    r'([A-Z][a-z]+(?: [A-Z][a-z]+){0,4})',
    re.IGNORECASE
)
PLACE_NAMES = re.compile(
    r'\b(walmart|heb|target|whataburger|mcdonald|walgreens|cvs|dollar general|'
    r'home depot|lowes|academy|bestbuy|best buy|kroger|central market|whole foods|'
    r'domain|barton creek|highland mall|southpark meadows|slaughter|william cannon|'
    r'ben white|rundberg|airport blvd|martin luther king|mlk|cesar chavez|'
    r'oltorf|stassney|riverside|manor road|183|290|71|loop 360|mopac|i-35|'
    r'parking garage|apartment|complex|shelter|library|school|high school|'
    r'middle school|elementary|park|rec center|community center)\b',
    re.IGNORECASE
)

conn = sqlite3.connect(DB)
rows = conn.execute("""
    SELECT transcript FROM calls
    WHERE category IN ('APD','TCSO','DPS')
    AND (location IS NULL OR location='')
    AND transcript IS NOT NULL
    AND length(transcript) > 20
""").fetchall()
conn.close()

total = len(rows)
cross_streets = Counter()
highways      = Counter()
at_places     = Counter()
known_places  = Counter()

for (txt,) in rows:
    for m in CROSS_STREET.finditer(txt):
        key = f"{m.group(1)} & {m.group(2)}"
        cross_streets[key] += 1
    for m in HIGHWAY.finditer(txt):
        highways[f"Highway/FM {m.group(1)}"] += 1
    for m in AT_LOCATION.finditer(txt):
        phrase = m.group(1).strip()
        if len(phrase) > 4:
            at_places[phrase] += 1
    for m in PLACE_NAMES.finditer(txt):
        known_places[m.group(0).lower()] += 1

print(f"\n{'='*60}")
print(f"APD/TCSO/DPS transcripts analyzed: {total}")
print(f"{'='*60}")

def show(title, counter, n=20):
    items = counter.most_common(n)
    if not items:
        print(f"\n{title}: (none yet)")
        return
    print(f"\n{title} (top {n}):")
    for name, count in items:
        print(f"  {count:3d}x  {name}")

show("Cross-streets mentioned", cross_streets)
show("Highways/roads mentioned", highways)
show("Known landmarks/places", known_places)
show("'At/near/on' phrases", at_places, 30)

print(f"\n{'='*60}")
print("Run again after more calls accumulate for better signal.")
print(f"{'='*60}\n")
