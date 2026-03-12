#!/usr/bin/env python3
"""
Battle Buddy — Demo Audio Generator (OpenAI TTS)

Generates all audio clips for demo.html using OpenAI TTS API.
Replaces the Piper-based generator with high-quality neural voices.

Voice assignments:
  onyx   — deep authoritative male  → law dispatch, pursuit
  echo   — mid-range male           → law field officers, fire crews
  fable  — expressive male          → fire dispatch
  nova   — calm female              → EMS dispatch/field
  alloy  — neutral                  → agent (Battle Buddy itself)
  shimmer — warm female             → sitrep narration

Radio effect still applied via ffmpeg for HEARD lines.

Usage:
    python3 generate_demo_audio_openai.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import urllib.request
import urllib.error
import json

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OUT_DIR        = Path("/home/pi/battle_buddy/logs/map/demo_audio")
CHIME_SRC      = Path("/home/pi/battle_buddy/chime.wav")
TTS_ENDPOINT   = "https://api.openai.com/v1/audio/speech"

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Voice assignments ─────────────────────────────────────────────────────────
# (kind, filename, voice, speed, text)
# speed: 0.85–1.15 range for OpenAI TTS
LINES = [
    # Block 1 — Law
    ("heard", "l01", "echo",  1.05, "Adam 7 show me out at FM 2222 and 620, traffic stop on a white Silverado."),
    ("heard", "l02", "echo",  1.05, "Dispatch, run Texas plate Hotel-Kilo-Romeo 4-4-9."),
    ("heard", "l03", "onyx",  0.95, "Adam 7 that plate comes back clear, registered to a Kevin Flores, Austin."),
    ("heard", "l04", "echo",  1.0,  "Baker 12, I'm out at 6200 Springdale, welfare check on a female. Caller says she hasn't been seen in two days."),
    ("heard", "l05", "echo",  1.05, "Baker 12, no answer at the door, going around back."),
    ("heard", "l06", "echo",  1.1,  "Dispatch, Baker 12, I'm gonna need EMS out here. Female is conscious, appears disoriented, possible overdose."),

    # Block 2 — Fire / EMS
    ("heard", "f01", "fable", 1.0,  "Engine 18, Rescue 18. Respond to 6200 Springdale Road, medical assist, Travis County Sheriff on scene."),
    ("heard", "f02", "nova",  1.0,  "Medic 4 is en route to Springdale, ETA 6 minutes."),
    ("heard", "f03", "nova",  1.0,  "Medic 4 on scene at 6200 Springdale. Female, mid-30s, altered mental status, suspected opiate overdose. Administering Narcan."),

    # Block 3 — Structure fire
    ("heard", "f04", "fable", 1.0,  "Engine 8, Ladder 1, Rescue 8. Structure fire, AFD Box 42-07, 4800 Manor Road. Investigate reported smoke showing from a two-story residential."),
    ("heard", "f05", "echo",  1.1,  "Engine 8 on scene, we've got smoke showing from the second floor, Alpha side. Laying a line, going in."),
    ("heard", "f06", "echo",  1.1,  "Engine 8, fire's in the wall cavity, second floor bedroom. Pulling ceiling, getting water on it."),
    ("heard", "f07", "echo",  1.05, "Ladder 1 on scene, venting the roof. All civilians are confirmed out of the structure."),
    ("heard", "f08", "echo",  1.0,  "Engine 8, fire's knocked down, starting overhaul. Requesting salvage crew."),

    # Block 4 — Pursuit
    ("heard", "l07", "onyx",  0.95, "All units Adam-West, pursuit in progress. Black Honda northbound on I-35 from Ben White, unit failed to stop for a felony warrant stop. All units stay clear of the corridor."),
    ("heard", "l08", "onyx",  1.1,  "Pursuit is at I-35 and Rundberg, vehicle now exiting onto Rundberg Lane westbound, speeds around 65."),
    ("heard", "l09", "onyx",  1.15, "Vehicle has stopped at 1200 Rundberg, subject fleeing on foot, setting up perimeter. Air support requested."),
    ("heard", "l10", "onyx",  1.0,  "Suspect in custody at 1240 Rundberg Lane, no injuries. Units can stand down on perimeter."),

    # Block 5 — Agent (Battle Buddy voice)
    ("agent",   "a01", "alloy",   1.0,  "Yes sir."),
    ("agent",   "a02", "alloy",   1.0,  "Ready. Go ahead."),
    ("agent",   "a03", "alloy",   0.95, "Tonight in Austin expect clear skies with a low around 58 degrees. Winds will be light out of the southeast at 5 to 10 miles per hour. No precipitation expected. A warm front moves in Thursday bringing a chance of storms by evening, but tonight looks clear."),
    ("agent",   "a04", "alloy",   1.0,  "Roger. Returning to monitor."),

    # Block 6 — Sitrep
    ("summary", "s01", "shimmer", 0.9,
     "Activity across the Austin metro area during this window was moderate with three notable incidents. "
     "A suspected opiate overdose at 6200 Springdale Road brought Sheriff's deputies and Travis County EMS Medic 4 to the scene — "
     "patient received Narcan and was transported. A structure fire at 4800 Manor Road was brought under control by Engine 8 and Ladder 1 "
     "with no civilian injuries — overhaul is underway. Separately, a felony warrant pursuit on I-35 northbound concluded with a foot "
     "pursuit and arrest at 1240 Rundberg Lane — no injuries reported. All three incidents appear resolved. "
     "No active scenes outstanding at this time."),
]


def openai_tts(text: str, voice: str, speed: float, mp3_out: str) -> bool:
    """Call OpenAI TTS API and save directly to mp3_out."""
    payload = json.dumps({
        "model":  "tts-1-hd",
        "input":  text,
        "voice":  voice,
        "speed":  speed,
        "response_format": "mp3",
    }).encode()

    req = urllib.request.Request(
        TTS_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            Path(mp3_out).write_bytes(resp.read())
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        print(f"    OpenAI error {e.code}: {body}", file=sys.stderr)
    except Exception as e:
        print(f"    Request error: {e}", file=sys.stderr)
    return False


def apply_radio_effect(mp3_in: str, mp3_out: str) -> bool:
    """Apply scanner radio filter to an already-generated mp3."""
    cmd = [
        "ffmpeg", "-y",
        "-i", mp3_in,
        "-af",
        (
            "highpass=f=300,"
            "lowpass=f=3200,"
            "volume=2.2,"
            "acompressor=threshold=-18dB:ratio=4:attack=5:release=50,"
            "anlmdn=s=0.002,"
            "volume=1.6"
        ),
        "-ar", "22050",
        "-ac", "1",
        "-q:a", "3",
        mp3_out,
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def copy_chime():
    out = OUT_DIR / "chime.mp3"
    if out.exists():
        print(f"  chime.mp3 already exists, skipping")
        return
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(CHIME_SRC), "-q:a", "3", str(out)],
        capture_output=True,
    )
    print(f"  chime.mp3 ✓")


def main():
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY not set in environment", file=sys.stderr)
        sys.exit(1)

    print(f"Generating {len(LINES)} clips via OpenAI TTS → {OUT_DIR}")
    copy_chime()

    ok_count = 0
    for kind, fname, voice, speed, text in LINES:
        mp3_out = OUT_DIR / f"{fname}.mp3"
        if mp3_out.exists():
            print(f"  {fname}.mp3 already exists, skipping")
            ok_count += 1
            continue

        print(f"  [{kind}] {fname} ({voice} {speed}x): {text[:55]}...")

        if kind == "heard":
            # Generate to temp, then apply radio filter
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                ok = openai_tts(text, voice, speed, tmp_path)
                if ok:
                    ok = apply_radio_effect(tmp_path, str(mp3_out))
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        else:
            ok = openai_tts(text, voice, speed, str(mp3_out))

        if ok:
            size = mp3_out.stat().st_size
            print(f"    ✓  {size//1024}KB")
            ok_count += 1
        else:
            print(f"    FAILED")

    print(f"\nDone — {ok_count}/{len(LINES)} clips ready.")


if __name__ == "__main__":
    main()
