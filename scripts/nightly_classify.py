#!/usr/bin/env python3
"""Nightly TGID classification + backfill pipeline. Run on kevcloud."""

import re
import sqlite3
from datetime import datetime

THRESHOLDS = [50, 10]
DB_PATH = "/opt/battlebuddy/calls.db"
TSV_PATH = "/opt/battlebuddy/gatrrs-tags.tsv"

CLASSIFIERS = {
    "FIRE": [
        r"\bfire\b",
        r"\bengine\b",
        r"\bladder\b",
        r"\bhose\b",
        r"\bsmoke\b",
        r"\balarm\b",
        r"\bbox\b",
        r"\bapparatus\b",
        r"\btruck\s*\d+\b",
        r"\bfirecom\b",
        r"\bafd\b",
        r"\btcfd\b",
        r"\bstructure\b",
        r"\bkingsland\b",
    ],
    "TCEMS": [
        r"\bmedic\b",
        r"\bems\b",
        r"\bpatient\b",
        r"\btriage\b",
        r"\btransport\b",
        r"\bhospital\b",
        r"\bmedcom\b",
        r"\btcems\b",
        r"\bccp\b",
        r"\bpcr\b",
        r"\bmedical\b",
        r"\bambulance\b",
        r"\bvitals?\b",
        r"\bgcs\b",
        r"\btrauma\b",
        r"\bcpr\b",
        r"\bhot\s+offload\b",
        r"\bstretcher\b",
    ],
    "LAW ENFORCEMENT": [
        r"\bapd\b",
        r"\bpd\b",
        r"\bsheriff\b",
        r"\bpatrol\b",
        r"\bcops?\b",
        r"\ben\s*route\b",
        r"\b10-4\b",
        r"\bcode\s*4\b",
        r"\binvestigat",
        r"\bsuspect\b",
        r"\bdetective\b",
        r"\btraffic stop\b",
        r"\bwarrant\b",
        r"\barrest\b",
        r"\bplate\b",
        r"\bregistration\b",
        r"\bcanine\b",
        r"\bk-9\b",
        r"\bk9\b",
    ],
    "UTILITIES": [
        r"\baustin\s*energy\b",
        r"\bpower\b",
        r"\belectric\b",
        r"\bhydrant\b",
        r"\btransformer\b",
        r"\bgas\b",
        r"\blift\s+station\b",
        r"\bpump\b",
        r"\baustin\s*water\b",
        r"\blcra\b",
        r"\bcentrifuge\b",
    ],
    "TRANSPORTATION": [
        r"\bhighway\b",
        r"\binterstate\b",
        r"\bcrash\b",
        r"\baccident\b",
        r"\bcollision\b",
        r"\btow\s*truck\b",
        r"\btxdot\b",
        r"\broad\b",
    ],
    "AIRPORT": [
        r"\babia\b",
        r"\bairport\b",
        r"\baircraft\b",
        r"\brunway\b",
        r"\btaxiway\b",
        r"\bair\s*asset\b",
        r"\bhelicopter\b",
        r"\bplane\b",
        r"\bflight\b",
        r"\baviation\b",
    ],
    "CORRECTIONS": [
        r"\binmate\b",
        r"\bprison\b",
        r"\bjail\b",
        r"\bcorrection",
        r"\bdetention\b",
        r"\btcj\b",
        r"\btdcj\b",
    ],
    "EDUCATION": [
        r"\bschool\b",
        r"\bcampus\b",
        r"\buniversity\b",
        r"\bclassroom\b",
        r"\bstudent\b",
        r"\bisd\b",
    ],
}

AGENCY_NAMES = {
    "FIRE": [
        (r"\bkingsland", "Kingsland Fire"),
        (r"\bafd\b", "Austin Fire"),
        (r"\btcfd\b", "Travis County Fire"),
        (r"\broundrock", "Round Rock Fire"),
        (r"\bpfluger", "Pflugerville Fire"),
        (r"\bbee\s*cave", "Bee Cave Fire"),
        (r"\bmarble\s*falls", "Marble Falls Fire"),
    ],
    "TCEMS": [
        (r"\btcems\b|medcom", "TCEMS"),
        (r"\bmedic\b", "EMS"),
        (r"\btraumacenter|trauma\s*center", "Trauma Center"),
        (r"\bhospital\b", "Hospital"),
        (r"\bphysician\b|\bnerd\b|\bcode\s+red\b|\bcode\s+blue\b", "Hospital Coord"),
    ],
    "LAW ENFORCEMENT": [
        (r"\bapd\b", "APD"),
        (r"\btcso\b|travis.*county.*sheriff", "TCSO"),
        (r"\roundrock.*pd", "Round Rock PD"),
        (r"\bpfluger.*pd|pflugerville.*pd", "Pflugerville PD"),
        (r"\bbee\s*cave.*pd", "Bee Cave PD"),
        (r"\bwestlake.*pd", "Westlake PD"),
        (r"\blakeway.*pd", "Lakeway PD"),
        (r"\bmanor.*pd", "Manor PD"),
        (r"\bsunset.*valley.*pd", "Sunset Valley PD"),
        (r"\blago\s*vista.*pd", "Lago Vista PD"),
        (r"\bustin\b|univ.*texas.*pd|utpd", "UTPD"),
        (r"\bdps\b|texas.*highway.*patrol|state.*trooper", "DPS"),
    ],
}


def classify_tgid(cur, tgid):
    cur.execute(
        "SELECT transcript FROM calls WHERE tgid=? AND transcript IS NOT NULL ORDER BY ts DESC LIMIT 20",
        (tgid,),
    )
    text = " ".join(r["transcript"] or "" for r in cur.fetchall()).lower()
    if not text.strip():
        return None, None
    scores = {}
    for cat, patterns in CLASSIFIERS.items():
        scores[cat] = sum(len(re.findall(p, text, re.IGNORECASE)) * 100 for p in patterns)
    cur.execute(
        "SELECT category, COUNT(*) as cnt FROM calls WHERE tgid=? AND category NOT IN ('Unknown','') GROUP BY category ORDER BY cnt DESC LIMIT 1",
        (tgid,),
    )
    ex = cur.fetchone()
    if ex:
        scores[ex["category"]] = scores.get(ex["category"], 0) + 200
    best = max(scores, key=scores.get) if scores and max(scores.values()) > 30 else None
    if not best:
        return None, None
    name = "TGID %s" % tgid
    for cat, patterns in AGENCY_NAMES.items():
        if cat == best:
            for pat, label in patterns:
                if re.search(pat, text, re.IGNORECASE):
                    name = label
                    break
    if name == "TGID %s" % tgid:
        name = {
            "FIRE": "Fire",
            "TCEMS": "TCEMS",
            "LAW ENFORCEMENT": "Law Enforcement",
            "UTILITIES": "Utilities",
            "TRANSPORTATION": "Transportation",
            "AIRPORT": "Airport",
            "CORRECTIONS": "Corrections",
            "EDUCATION": "Education",
        }.get(best, best.title())
    return best, name.strip()


print(f"=== TGID Classification Run: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

conn = sqlite3.connect(DB_PATH, timeout=10)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Snapshot before
cur.execute("SELECT COUNT(DISTINCT tgid), COUNT(*) FROM calls WHERE tgid!=0 AND tag LIKE 'TGID %'")
before_tgids, before_calls = cur.fetchone()
print(f"Before: {before_tgids} unmapped TGIDs ({before_calls} calls)")

# Read existing TSV entries
with open(TSV_PATH) as f:
    existing_lines = f.readlines()
existing = {
    int(line.split("\t")[0])
    for line in existing_lines
    if line.split("\t")[0].isdigit()
}

total_classified = 0
for threshold in THRESHOLDS:
    cur.execute(
        "SELECT tgid, COUNT(*) as cnt FROM calls WHERE tgid!=0 GROUP BY tgid HAVING cnt>=? ORDER BY cnt DESC",
        (threshold,),
    )
    targets = [(r["tgid"], r["cnt"]) for r in cur.fetchall() if r["tgid"] not in existing]

    new_entries = []
    for tgid, cnt in targets:
        cat, name = classify_tgid(cur, tgid)
        if cat:
            print(f"  TGID={tgid:<6} Calls={cnt:<5} -> {name[:30]:<30} [{cat[:15]}]")
            new_entries.append((tgid, name, cat))
            existing.add(tgid)

    if new_entries:
        new_lines, inserted = [], set()
        for line in existing_lines:
            tnum = int(line.split("\t")[0]) if line.split("\t")[0].isdigit() else 0
            for tgid, name, cat in sorted(new_entries):
                if tgid not in inserted and tnum > tgid:
                    new_lines.append(f"{tgid}\t{name}\t{cat}\n")
                    inserted.add(tgid)
            new_lines.append(line)
        for tgid, name, cat in sorted(new_entries):
            if tgid not in inserted:
                new_lines.append(f"{tgid}\t{name}\t{cat}\n")
        with open(TSV_PATH, "w") as f:
            f.writelines(new_lines)
        existing_lines = new_lines
        total_classified += len(new_entries)

# Backfill: update all calls with entries in tag file
tag_lookup = {}
with open(TSV_PATH) as f:
    for line in f:
        parts = line.strip().split("\t")
        if len(parts) >= 2:
            try:
                tgid = int(parts[0])
                tag_lookup[tgid] = (parts[1].strip(), parts[2].strip() if len(parts) >= 3 else "")
            except ValueError:
                pass

backfill_total = 0
for tgid, (name, cat) in tag_lookup.items():
    cur.execute(
        "UPDATE calls SET tag=?, category=? WHERE tgid=? AND (tag IS NULL OR tag='' OR tag LIKE 'TGID %' OR tag=?)",
        (name, cat, tgid, name),
    )
    if cur.rowcount > 0:
        backfill_total += cur.rowcount

conn.commit()

# Snapshot after
cur.execute("SELECT COUNT(DISTINCT tgid), COUNT(*) FROM calls WHERE tgid!=0 AND tag LIKE 'TGID %'")
after_tgids, after_calls = cur.fetchone()
conn.close()

print(f"\nClassified: {total_classified} new TGIDs | Backfilled: {backfill_total} calls")
print(f"After: {after_tgids} unmapped TGIDs ({after_calls} calls)")
print(f"Removed: {before_tgids - after_tgids} TGIDs, {before_calls - after_calls} calls")

# Highlight generic names needing review
with open(TSV_PATH) as f:
    generic = [
        line.strip()
        for line in f
        if re.search(
            r"\t(Fire Fire|Law Enforcement Law|Utilities Util|Transportation Trans|Airport Airport|Education School|Tcems EMS|Corrections Corr)",
            line,
        )
    ]
if generic:
    print(f"\n!! {len(generic)} generic names need manual refinement:")
    for g in generic:
        print(f"  {g}")
else:
    print("\nNo generic names found — all clean!")
