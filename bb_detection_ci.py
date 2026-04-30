#!/usr/bin/env python3
"""
Battle Buddy Detection CI
Compares CAD ground truth against detected incidents to find:
  - False negatives: serious CAD calls we never detected
  - Coverage by category
  - Keyword gap analysis via Gemini on missed calls
Outputs: console report + Telegram + appends to bugs.md
"""
import sqlite3, json, time, os, urllib.request, textwrap
from datetime import datetime, timezone

DB_PATH   = "/opt/battlebuddy/calls.db"
BUGS_PATH = "/opt/battlebuddy/bugs.md"
WINDOW    = 7200   # ±2h around CAD response_ts to find nearby calls

env = {l.split("=",1)[0]: l.split("=",1)[1].strip()
       for l in open("/opt/battlebuddy/.env") if "=" in l}
OR_KEY   = env.get("OPENROUTER_API_KEY","")
TG_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = env.get("TELEGRAM_CHAT_ID", "")

SERIOUS = [
    "Shoot/Stab", "Shootings", "Aggravated Assault", "Robbery",
    "Homicide", "Sex Crimes", "Missing Persons/Kidnapping",
    "Simple Assault", "Crashes",
]

def ts_fmt(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m/%d %H:%Mz")

def llm_call(system, user, max_tokens=800):
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps({
            "model": "google/gemini-2.5-flash",
            "messages": [{"role":"system","content":system},
                         {"role":"user","content":user}],
            "max_tokens": max_tokens, "temperature": 0.2,
        }).encode(),
        headers={"Authorization": f"Bearer {OR_KEY}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://battlebuddy.news"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()

def tg(msg):
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}).encode(),
            headers={"Content-Type":"application/json"}, method="POST",
        ), timeout=10)
    except Exception as e:
        print(f"[tg] {e}")

conn = sqlite3.connect(DB_PATH, timeout=10)

# ── Use full CAD date range ───────────────────────────────────────────────────
cad_range = conn.execute("SELECT MIN(response_ts), MAX(response_ts) FROM apd_cad").fetchone()
cad_min, cad_max = float(cad_range[0]), float(cad_range[1])

print("=" * 70)
print("BATTLE BUDDY DETECTION CI REPORT")
print(f"Run:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"CAD window: {ts_fmt(cad_min)} → {ts_fmt(cad_max)}")
print("=" * 70)

# ── SECTION 1: COVERAGE BY CATEGORY ──────────────────────────────────────────
print("\n[ 1 ] DETECTION COVERAGE — CAD matched vs total by category\n")

coverage = conn.execute("""
    SELECT final_category,
           COUNT(*) as total,
           SUM(CASE WHEN matched_incident_id IS NOT NULL THEN 1 ELSE 0 END) as matched
    FROM apd_cad
    WHERE final_category IS NOT NULL AND final_category != 'missing'
    GROUP BY final_category
    ORDER BY total DESC
    LIMIT 25
""").fetchall()

for cat, total, matched in coverage:
    pct = matched / total * 100 if total else 0
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct/5))
    flag = " ◄ SERIOUS" if cat in SERIOUS else ""
    print(f"  {cat:35s} {bar} {pct:4.0f}%  ({matched:3d}/{total}){flag}")

# ── SECTION 2: FALSE NEGATIVES — SERIOUS MISSED CALLS ────────────────────────
print("\n[ 2 ] FALSE NEGATIVES — Serious unmatched CAD records\n")

unmatched = conn.execute("""
    SELECT incident_number, response_ts, final_category, final_description,
           initial_description, sector
    FROM apd_cad
    WHERE matched_incident_id IS NULL
      AND final_category IN ({})
    ORDER BY final_category, response_ts DESC
""".format(",".join("?"*len(SERIOUS))), SERIOUS).fetchall()

print(f"  Total serious unmatched: {len(unmatched)}")

# Find nearby calls for each miss
misses_with_calls = []
misses_no_signal  = []

for row in unmatched:
    inc_num, resp_ts, cat, final_desc, init_desc, sector = row
    resp_ts = float(resp_ts)
    nearby = conn.execute("""
        SELECT tag, transcript, ts FROM calls
        WHERE ts BETWEEN ? AND ?
          AND transcript != ''
          AND LENGTH(transcript) > 30
        ORDER BY ABS(ts - ?) LIMIT 8
    """, (resp_ts - WINDOW, resp_ts + WINDOW, resp_ts)).fetchall()

    entry = dict(cad_id=inc_num, ts=resp_ts, cat=cat,
                 final_desc=final_desc, init_desc=init_desc,
                 sector=sector, calls=nearby)
    if nearby:
        misses_with_calls.append(entry)
    else:
        misses_no_signal.append(entry)

print(f"  Had radio calls in ±2h window: {len(misses_with_calls)}")
print(f"  No radio signal at all:         {len(misses_no_signal)}")

print("\n  Breakdown by category:")
by_cat = {}
for m in unmatched:
    by_cat[m[2]] = by_cat.get(m[2], 0) + 1
for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
    print(f"    {cat:35s}: {cnt}")

print("\n  Sample missed incidents:")
for m in misses_with_calls[:10]:
    print(f"    {ts_fmt(m['ts'])} | {m['cat']:20s} | {m['final_desc']:30s} | {len(m['calls'])} calls nearby")

# ── SECTION 3: FALSE POSITIVES ────────────────────────────────────────────────
print("\n[ 3 ] FALSE POSITIVE CANDIDATES — single-call incidents, no CAD, cleared <10min\n")

fp_rows = conn.execute("""
    SELECT i.id, i.itype, i.ts_start, i.ts_cleared, i.location,
           COUNT(ic.call_id) as call_count
    FROM incidents i
    LEFT JOIN incident_calls ic ON ic.incident_id = i.id
    WHERE i.ts_start BETWEEN ? AND ?
      AND (i.is_test IS NULL OR i.is_test=0)
      AND i.status = 'cleared'
      AND i.ts_cleared IS NOT NULL
      AND (i.ts_cleared - i.ts_start) < 600
      AND i.description NOT LIKE '%[APD Press Release]%'
    GROUP BY i.id HAVING call_count <= 1
""", (cad_min, cad_max)).fetchall()

fp_no_cad = [r for r in fp_rows if not conn.execute(
    "SELECT 1 FROM apd_cad WHERE matched_incident_id=?", (r[0],)).fetchone()]

print(f"  Short single-call cleared incidents: {len(fp_rows)}")
print(f"  Of those with no CAD corroboration:  {len(fp_no_cad)}")
fp_by_type = {}
for r in fp_no_cad:
    fp_by_type[r[1]] = fp_by_type.get(r[1], 0) + 1
for itype, cnt in sorted(fp_by_type.items(), key=lambda x: -x[1]):
    print(f"    {itype:40s}: {cnt}")

# ── SECTION 4: SUMMARY METRICS ────────────────────────────────────────────────
total_serious = len(unmatched) + conn.execute("""
    SELECT COUNT(*) FROM apd_cad
    WHERE matched_incident_id IS NOT NULL
      AND final_category IN ({})
""".format(",".join("?"*len(SERIOUS))), SERIOUS).fetchone()[0]

matched_serious = total_serious - len(unmatched)
recall = matched_serious / total_serious * 100 if total_serious else 0
total_inc = conn.execute(
    "SELECT COUNT(*) FROM incidents WHERE ts_start BETWEEN ? AND ? AND (is_test IS NULL OR is_test=0)",
    (cad_min, cad_max)).fetchone()[0]
fp_rate = len(fp_no_cad) / total_inc * 100 if total_inc else 0

print("\n[ 4 ] SUMMARY METRICS\n")
print(f"  Serious CAD incidents in window : {total_serious}")
print(f"  Matched by Battle Buddy         : {matched_serious}")
print(f"  Estimated recall                : {recall:.1f}%")
print(f"  Total BB incidents in window    : {total_inc}")
print(f"  False positive candidates       : {len(fp_no_cad)} ({fp_rate:.1f}%)")

# ── SECTION 5: LLM KEYWORD GAP ANALYSIS ──────────────────────────────────────
print("\n[ 5 ] LLM KEYWORD GAP ANALYSIS (Gemini 2.5 Flash)\n")

analysis_set = misses_with_calls[:10]
keyword_suggestions = "No missed calls with nearby radio traffic to analyse."

if analysis_set:
    cases_text = ""
    for m in analysis_set:
        snippet = "\n".join(
            f"    [{ts_fmt(c[2])}] {c[0]}: {c[1][:250]}"
            for c in m["calls"]
        )
        cases_text += (
            f"\nCAD: {m['cad_id']} | {m['cat']} / {m['final_desc']} | {ts_fmt(m['ts'])}\n"
            f"Radio calls in ±2h window:\n{snippet}\n{'─'*50}\n"
        )

    current_kw = (
        "shots fired, shooting, person shot, subject shot, victim shot, shot victim, "
        "found dead, body found, gsw, gunshot, gun shot, stabbing, stab, assault, "
        "mass casualty, mci, cardiac arrest, multiple patients, trauma, "
        "structure fire, working fire, fully involved, hazmat, chemical spill, "
        "hostage, barricade, fatal crash, fatality, start a dts, crash, collision, "
        "rollover, medical examiner, jp responding, justice of the peace, "
        "pronounced at, death investigation, signal 48"
    )

    system = (
        "You are a public safety radio analyst helping improve an automated P25 radio "
        "incident detection system for Austin TX. The system classifies events from "
        "Whisper ASR transcripts of AFD, TCEMS, TCSO and other unencrypted GATRRS talkgroups. APD traffic is encrypted and produces no transcripts. "
        "Be specific and practical — suggest exact phrases to add as keywords."
    )
    user = (
        f"CURRENT KEYWORDS:\n{current_kw}\n\n"
        f"MISSED INCIDENTS (CAD confirmed but NOT detected by Battle Buddy):\n{cases_text}\n"
        "For each case:\n"
        "1. Did the nearby radio transcripts contain ANY catchable signal?\n"
        "2. What exact keyword or phrase would have triggered detection?\n"
        "3. What itype should it map to?\n"
        "4. Precision risk: would this keyword cause false positives on routine traffic?\n"
        "Then give a consolidated NEW KEYWORDS table at the end: phrase → itype → risk level.\n"
        "Also note any categories that had ZERO radio signal — those are not fixable with keywords."
    )

    print("  Calling Gemini 2.5 Flash...")
    try:
        keyword_suggestions = llm_call(system, user, max_tokens=1000)
        print("\n  GEMINI KEYWORD GAP ANALYSIS:\n")
        print(textwrap.indent(keyword_suggestions, "  "))
    except Exception as e:
        keyword_suggestions = f"LLM failed: {e}"
        print(f"  ERROR: {e}")
else:
    print("  No cases with nearby calls — all misses had no radio signal.")

conn.close()

# ── WRITE TO BUGS.MD ──────────────────────────────────────────────────────────
ts_now = datetime.now().strftime("%Y-%m-%d %H:%M")
entry = (
    f"\n---\n## Detection CI — {ts_now}\n"
    f"**Recall:** {recall:.1f}% ({matched_serious}/{total_serious} serious CAD incidents matched)\n"
    f"**False positive candidates:** {len(fp_no_cad)} of {total_inc} incidents ({fp_rate:.1f}%)\n"
    f"**Unmatched serious categories:** {', '.join(sorted(by_cat.keys()))}\n\n"
    f"### Gemini Keyword Analysis\n{keyword_suggestions}\n"
)
with open(BUGS_PATH, "a") as f:
    f.write(entry)
print(f"\n  Appended to {BUGS_PATH}")

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
tg(
    f"📊 <b>BB Detection CI — {ts_now}</b>\n\n"
    f"<b>Recall:</b> {recall:.1f}% ({matched_serious}/{total_serious} serious CAD matched)\n"
    f"<b>FP candidates:</b> {len(fp_no_cad)}/{total_inc} incidents\n"
    f"<b>Top missed categories:</b>\n"
    + "".join(f"  • {c}: {n}\n" for c,n in sorted(by_cat.items(),key=lambda x:-x[1])[:6])
    + "\nKeyword analysis in bugs.md"
)
print("  Telegram summary sent.")
print("\n" + "=" * 70)
print("DONE")
print("=" * 70)
