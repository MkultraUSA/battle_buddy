#!/usr/bin/env python3
"""
Battle Buddy — Situational Summary
===================================
Reads recent incidents and radio traffic from the DB, asks Claude Sonnet
for a plain-English intelligence briefing, and outputs it to:
  - Terminal (always)
  - Display pipe (default on)
  - Piper TTS speaker (with --speak)

Usage:
    python3 battle_buddy_summary.py                  # last 4 hours, display only
    python3 battle_buddy_summary.py --hours 2        # narrow window
    python3 battle_buddy_summary.py --hours 8 --speak
    python3 battle_buddy_summary.py --no-display     # terminal only
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
_config_env = Path(__file__).parent / "config.env"
if _config_env.exists():
    for _line in _config_env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-sonnet-4-6"
DB_PATH           = Path(__file__).parent / "logs" / "battle_buddy.db"
PIPE_PATH         = "/tmp/battle_buddy_display.pipe"
PIPER_BIN         = "/home/pi/voice-claude/bin/piper"
PIPER_MODEL       = "/home/pi/voice-claude/piper-voices/en_US-lessac-medium.onnx"
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a tactical intelligence officer for Travis County, Texas.
You monitor Austin-area police, fire, and EMS radio traffic and produce concise
situational briefings for field personnel and the public.

When given a list of recent radio incidents and transcripts, produce a briefing that:
- Opens with a one-sentence overall threat/activity level assessment
- Lists any MAJOR active incidents first (SWAT, air unit search, shots fired,
  officer down, barricaded subject, structure fire, major accident, water rescue, etc.)
- Groups related incidents by area or theme where relevant
- Flags anything that may impact traffic, public safety, or large areas
- Uses plain English — no jargon, no codes (translate any 10-codes if present)
- Is concise — aim for 3-6 sentences for routine periods, up to 10 for busy periods
- Ends with the time window covered

Do NOT include: speculation, personal details, names, or information not in the data.
If there are no significant incidents, say so briefly.
"""


def load_recent_data(hours: int) -> tuple[list[dict], list[dict]]:
    """Load incidents and heard lines from the DB for the last N hours."""
    if not DB_PATH.exists():
        return [], []

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    incidents = [
        dict(r)
        for r in conn.execute(
            """SELECT timestamp, type, address, severity, lat, lon, talkgroup_raw
               FROM incidents
               WHERE timestamp >= ? AND deleted = 0
               ORDER BY timestamp DESC""",
            (cutoff,),
        ).fetchall()
    ]

    heard = [
        dict(r)
        for r in conn.execute(
            """SELECT timestamp, text, stream
               FROM heard_lines
               WHERE timestamp >= ?
               ORDER BY timestamp DESC
               LIMIT 200""",
            (cutoff,),
        ).fetchall()
    ]

    conn.close()
    return incidents, heard


def build_prompt(incidents: list[dict], heard: list[dict], hours: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    window_start = (datetime.now() - timedelta(hours=hours)).strftime("%H:%M")

    lines = [f"Situational data for Austin Metro — {window_start} to {now} ({hours}h window)\n"]

    if incidents:
        lines.append(f"=== EXTRACTED INCIDENTS ({len(incidents)}) ===")
        for inc in incidents:
            sev = inc.get("severity", "").upper()
            tg  = f" [{inc['talkgroup_raw']}]" if inc.get("talkgroup_raw") else ""
            lines.append(
                f"[{inc['timestamp']}] [{sev}] {inc['type'].upper()} @ {inc['address']}{tg}"
            )
    else:
        lines.append("=== NO STRUCTURED INCIDENTS IN DATABASE FOR THIS WINDOW ===")

    if heard:
        lines.append(f"\n=== RECENT RADIO TRAFFIC SAMPLE ({min(len(heard), 50)} lines) ===")
        for h in heard[:50]:
            lines.append(f"[{h['timestamp']}] {h['text']}")

    return "\n".join(lines)


def call_claude(prompt: str) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[Summary error: {e}]"


def send_to_display(text: str) -> None:
    try:
        if os.path.exists(PIPE_PATH):
            with open(PIPE_PATH, "w") as p:
                p.write(f"SUMMARY: {text}\n")
    except Exception as e:
        print(f"[display] Could not send to pipe: {e}", file=sys.stderr)


def speak(text: str) -> None:
    """Speak text via Piper TTS → aplay."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name

        proc = subprocess.run(
            [PIPER_BIN, "--model", PIPER_MODEL, "--output_file", wav_path],
            input=text.encode(),
            capture_output=True,
        )
        if proc.returncode != 0:
            print(f"[tts] Piper error: {proc.stderr.decode()}", file=sys.stderr)
            return

        subprocess.run(["aplay", "-q", wav_path])
    except Exception as e:
        print(f"[tts] Error: {e}", file=sys.stderr)
    finally:
        try:
            os.unlink(wav_path)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Battle Buddy — Situational Summary")
    parser.add_argument("--hours",      type=int,  default=4,    help="Look-back window in hours (default: 4)")
    parser.add_argument("--speak",      action="store_true",      help="Speak summary via Piper TTS")
    parser.add_argument("--no-display", action="store_true",      help="Don't send to display pipe")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set in config.env", file=sys.stderr)
        sys.exit(1)

    print(f"[summary] Loading last {args.hours}h of data from DB…")
    incidents, heard = load_recent_data(args.hours)
    print(f"[summary] {len(incidents)} incidents, {len(heard)} heard lines")

    if not incidents and not heard:
        print("[summary] No data found — is the DB populated? Run run_parser.sh first.")
        sys.exit(0)

    prompt = build_prompt(incidents, heard, args.hours)

    print(f"[summary] Calling Claude ({CLAUDE_MODEL})…")
    summary = call_claude(prompt)

    print("\n" + "=" * 60)
    print("BATTLE BUDDY SITUATIONAL SUMMARY")
    print("=" * 60)
    print(summary)
    print("=" * 60 + "\n")

    if not args.no_display:
        send_to_display(summary)
        print("[summary] Sent to display.")

    if args.speak:
        print("[summary] Speaking via Piper TTS…")
        speak(summary)


if __name__ == "__main__":
    main()
