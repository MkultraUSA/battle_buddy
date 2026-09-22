#!/usr/bin/env python3
"""Battlebuddy assembly reviewer (v1): every 15 min via cron, ask a free
model whether recent radio traffic assembled into the right incidents.

Reads /opt/battlebuddy/calls.db, calls the local OpenCode bridge
(nemotron-3-ultra-free, mimo-v2.6-flash-free fallback), writes a dated
markdown report to /opt/battlebuddy/reviews/. Never touches creds.
"""
import datetime
import os
import sqlite3
import subprocess
import sys
import zoneinfo

LOCAL = zoneinfo.ZoneInfo("America/Chicago")

DB = "/opt/battlebuddy/calls.db"
OUTDIR = "/opt/battlebuddy/reviews"
PROCEDURES = "/opt/battlebuddy/procedures.md"
OPENCODE = "/root/.opencode/bin/opencode"
PRIMARY = "opencode/nemotron-3-ultra-free"
FALLBACK = "opencode/mimo-v2.6-flash-free"
WINDOW_MIN = 35

PROMPT_HEAD = """You are incident-assembly QA for Battlebuddy, a public-safety radio monitor in Austin TX.
You get recent transcribed radio calls plus the incidents the system filed.
Task: for each candidate real-world event visible in the calls, judge ASSEMBLED (an incident captures it with the right agencies), FRAGMENTED (pieces filed as separate/Unknown-agency incidents), or MISSED (no incident at all).
Known failure shapes: school/police tags hiding protest relevance; air assets unattributed; EMS dispatch without agency; multi-agency convergence split across incidents.
Reply in markdown: ## Verdicts (one line each: time, event, verdict, why), ## Missed details (call timestamps that should have mattered), ## Confidence (high/med/low + one line).
Be terse. Transcripts are public scanner broadcasts.
"""


def recent_data(db_path: str, window_min: int) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    lo = now - window_min * 60
    day_lo = now - 16 * 3600
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    calls = list(
        cur.execute(
            "SELECT ts, tgid, tag, transcript FROM calls WHERE ts > ? ORDER BY ts", (lo,)
        )
    )
    lines = ["RECENT CALLS (time, tgid, tag, transcript):"]
    for ts, tgid, tag, tr in calls:
        t = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone(LOCAL).strftime("%H:%M")
        lines.append(f"- {t} [{tgid} {tag}]: {(tr or '')[:220]}")
    # Retrieval backfill: earlier-today calls on the same TGs, so
    # multi-hour stories (protests, pursuits) arrive with their thread.
    tgids = sorted({c[1] for c in calls if c[1]})
    if tgids:
        q = f"SELECT ts, tgid, tag, transcript FROM calls WHERE ts > ? AND ts <= ? AND tgid IN ({','.join('?' * len(tgids))}) ORDER BY ts DESC LIMIT 40"
        lines.append("EARLIER TODAY ON THE SAME TGs (thread backfill):")
        for ts, tgid, tag, tr in cur.execute(q, (day_lo, lo, *tgids)):
            t = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone(LOCAL).strftime("%H:%M")
            lines.append(f"- {t} [{tgid} {tag}]: {(tr or '')[:160]}")
    lines.append("RECENT INCIDENTS (start, type, agencies, description):")
    for ts, itype, ag, desc in cur.execute(
        "SELECT ts_start, itype, agencies, description FROM incidents "
        "WHERE ts_start > ? ORDER BY ts_start",
        (lo - 3600,),
    ):
        t = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone(LOCAL).strftime("%H:%M")
        lines.append(f"- {t} {itype} {ag}: {(desc or '')[:160]}")
    con.close()
    return "\n".join(lines)


def ask(prompt: str, model: str) -> str:
    r = subprocess.run(
        [OPENCODE, "run", "--model", model, prompt],
        capture_output=True, text=True, timeout=540,
        env={**os.environ, "HOME": "/root"},
    )
    if r.returncode != 0:
        raise RuntimeError(f"{model} failed rc={r.returncode}: {(r.stderr or '')[:200]}")
    return r.stdout.strip()


def load_procedures() -> str:
    try:
        with open(PROCEDURES) as f:
            return f.read()
    except Exception:
        return ''


def main() -> int:
    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    data = recent_data(DB, WINDOW_MIN)
    proc = load_procedures()
    brief = (PROMPT_HEAD + ('\nPROCEDURE KNOWLEDGE:\n' + proc if proc else '') + '\n' + data)
    n_calls = data.count("\n- ") 
    model, out = PRIMARY, ""
    try:
        out = ask(brief, PRIMARY)
    except Exception as exc:
        print(f"primary failed: {exc}", flush=True)
        try:
            out = ask(brief, FALLBACK)
            model = FALLBACK
        except Exception as exc2:
            print(f"fallback failed: {exc2}", flush=True)
            return 1
    path = os.path.join(OUTDIR, f"{stamp}.md")
    with open(path, "w") as f:
        f.write(f"# Assembly review {stamp} (model {model}, window {WINDOW_MIN}m)\n\n{out}\n")
    print(f"wrote {path} ({len(out)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
