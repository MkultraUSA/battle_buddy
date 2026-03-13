#!/usr/bin/env python3
"""
Battle Buddy — Demo Audio Generator

Generates all audio clips for demo.html using Piper TTS + ffmpeg.
- HEARD lines: Piper TTS → ffmpeg radio scanner effect (bandpass + noise)
- AGENT lines: Piper TTS clean (lessac medium voice)
- SUMMARY:     Piper TTS clean, slightly slower

Output: logs/map/demo_audio/ — served by nginx alongside demo.html

Usage:
    python3 generate_demo_audio.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PIPER     = "/home/pi/voice-claude/bin/piper"
MODEL     = "/home/pi/voice-claude/piper-voices/en_US-lessac-medium.onnx"
OUT_DIR   = Path("/home/pi/battle_buddy/logs/map/demo_audio")
CHIME_SRC = Path("/home/pi/battle_buddy/chime.wav")

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Voice characters (applied to HEARD lines only) ────────────────────────────
# Each voice is a pitch/tempo ffmpeg filter added before the radio effect.
# asetrate shifts pitch; atempo compensates duration so speed stays natural.
#
#  A — Law dispatch (TCSO)     : pitch -12%  → deeper, authoritative
#  B — Law field units         : pitch -6%   → male officer in the field
#  C — Fire dispatch / LOCUTION: pitch +8%   → slightly higher, crisp dispatch
#  D — Fire field crews        : pitch +4%   → different male, physical/urgent
#  E — EMS dispatch/field      : pitch +14%  → calm, higher — distinct from fire
#  F — Pursuit officer         : pitch -10%, tempo +5% → tense, faster cadence

VOICES = {
    "A": "asetrate=22050*0.88,atempo=1.136",   # Law dispatch  — deep
    "B": "asetrate=22050*0.94,atempo=1.064",   # Law field     — male officer
    "C": "asetrate=22050*1.08,atempo=0.926",   # Fire dispatch — higher crisp
    "D": "asetrate=22050*1.04,atempo=0.962",   # Fire field    — urgent male
    "E": "asetrate=22050*1.14,atempo=0.877",   # EMS           — calm higher
    "F": "asetrate=22050*0.90,atempo=1.050",   # Pursuit       — tense faster
}

# ── All lines from the scenario in order ──────────────────────────────────────
# (kind, filename, voice, text)   voice=None for agent/summary
LINES = [
    # Block 1 — Law
    ("heard", "l01", "B", "Adam 7 show me out at FM 2222 and 620, traffic stop on a white Silverado."),
    ("heard", "l02", "B", "Dispatch, run Texas plate Hotel-Kilo-Romeo 4-4-9."),
    ("heard", "l03", "A", "Adam 7 that plate comes back clear, registered to a Kevin Flores, Austin."),
    ("heard", "l04", "B", "Baker 12, I'm out at 6200 Springdale, welfare check on a female. Caller says she hasn't been seen in two days."),
    ("heard", "l05", "B", "Baker 12, no answer at the door, going around back."),
    ("heard", "l06", "B", "Dispatch, Baker 12, I'm gonna need EMS out here. Female is conscious, appears disoriented, possible overdose."),

    # Block 2 — Fire / EMS
    ("heard", "f01", "C", "Engine 18, Rescue 18. Respond to 6200 Springdale Road, medical assist, Travis County Sheriff on scene."),
    ("heard", "f02", "E", "Medic 4 is en route to Springdale, ETA 6 minutes."),
    ("heard", "f03", "E", "Medic 4 on scene at 6200 Springdale. Female, mid-30s, altered mental status, suspected opiate overdose. Administering Narcan."),

    # Block 3 — Structure fire
    ("heard", "f04", "C", "Engine 8, Ladder 1, Rescue 8. Structure fire, AFD Box 42-07, 4800 Manor Road. Investigate reported smoke showing from a two-story residential."),
    ("heard", "f05", "D", "Engine 8 on scene, we've got smoke showing from the second floor, Alpha side. Laying a line, going in."),
    ("heard", "f06", "D", "Engine 8, fire's in the wall cavity, second floor bedroom. Pulling ceiling, getting water on it."),
    ("heard", "f07", "D", "Ladder 1 on scene, venting the roof. All civilians are confirmed out of the structure."),
    ("heard", "f08", "D", "Engine 8, fire's knocked down, starting overhaul. Requesting salvage crew."),

    # Block 4 — Pursuit
    ("heard", "l07", "A", "All units Adam-West, pursuit in progress. Black Honda northbound on I-35 from Ben White, unit failed to stop for a felony warrant stop. All units stay clear of the corridor."),
    ("heard", "l08", "F", "Pursuit is at I-35 and Rundberg, vehicle now exiting onto Rundberg Lane westbound, speeds around 65."),
    ("heard", "l09", "F", "Vehicle has stopped at 1200 Rundberg, subject fleeing on foot, setting up perimeter. Air support requested."),
    ("heard", "l10", "F", "Suspect in custody at 1240 Rundberg Lane, no injuries. Units can stand down on perimeter."),

    # Block 5 — Agent / voice
    ("agent",   "a01", None, "Yes sir."),
    ("agent",   "a02", None, "Ready. Go ahead."),
    ("agent",   "a03", None, "Tonight in Austin expect clear skies with a low around 58 degrees. Winds will be light out of the southeast at 5 to 10 miles per hour. No precipitation expected. A warm front moves in Thursday bringing a chance of storms by evening, but tonight looks clear."),
    ("agent",   "a04", None, "Roger. Returning to monitor."),

    # Block 6 — Sitrep
    ("summary", "s01", None,
     "Activity across the Austin metro area during this window was moderate with three notable incidents. "
     "A suspected opiate overdose at 6200 Springdale Road brought Sheriff's deputies and Travis County EMS Medic 4 to the scene — "
     "patient received Narcan and was transported. A structure fire at 4800 Manor Road was brought under control by Engine 8 and Ladder 1 "
     "with no civilian injuries — overhaul is underway. Separately, a felony warrant pursuit on I-35 northbound concluded with a foot "
     "pursuit and arrest at 1240 Rundberg Lane — no injuries reported. All three incidents appear resolved. "
     "No active scenes outstanding at this time."),
]


def run_piper(text: str, wav_path: str, length_scale: float = 1.0):
    """Generate speech WAV using Piper TTS."""
    cmd = [
        PIPER,
        "-m", MODEL,
        "-f", wav_path,
        "--length-scale", str(length_scale),
    ]
    result = subprocess.run(
        cmd,
        input=text.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"  Piper error: {result.stderr.decode()[:200]}", file=sys.stderr)
        return False
    return True


def apply_radio_effect(wav_in: str, mp3_out: str, voice: str = None):
    """
    Apply scanner radio effect with optional voice pitch/tempo shift.
    - Optional pitch shift via asetrate+atempo (voice character)
    - Bandpass 300-3200 Hz
    - Compression + light noise artifact
    - Mono, 22050 Hz
    """
    voice_filter = (VOICES[voice] + "," ) if voice and voice in VOICES else ""
    af = (
        f"{voice_filter}"
        "highpass=f=300,"
        "lowpass=f=3200,"
        "volume=2.5,"
        "acompressor=threshold=-20dB:ratio=4:attack=5:release=50,"
        "anlmdn=s=0.003,"
        "volume=1.8"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", wav_in,
        "-af", af,
        "-ar", "22050",
        "-ac", "1",
        "-q:a", "4",
        mp3_out,
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def apply_clean_tts(wav_in: str, mp3_out: str):
    """Convert Piper WAV to MP3 with light normalization."""
    cmd = [
        "ffmpeg", "-y",
        "-i", wav_in,
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ar", "22050",
        "-ac", "1",
        "-q:a", "3",
        mp3_out,
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def copy_chime():
    """Convert chime.wav to mp3 for web use."""
    out = OUT_DIR / "chime.mp3"
    if out.exists():
        print(f"  chime.mp3 already exists, skipping")
        return
    cmd = ["ffmpeg", "-y", "-i", str(CHIME_SRC), "-q:a", "3", str(out)]
    subprocess.run(cmd, capture_output=True)
    print(f"  chime.mp3 ✓")


def main():
    print(f"Generating {len(LINES)} audio clips → {OUT_DIR}")
    copy_chime()

    manifest = {}

    for kind, fname, voice, text in LINES:
        mp3_out = OUT_DIR / f"{fname}.mp3"
        if mp3_out.exists():
            print(f"  {fname}.mp3 already exists, skipping")
            manifest[fname] = {"kind": kind, "file": f"demo_audio/{fname}.mp3"}
            continue

        voice_label = f" voice={voice}" if voice else ""
        print(f"  [{kind}{voice_label}] {fname}: {text[:60]}...")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = tmp.name

        try:
            length = 1.05 if kind == "summary" else 1.0
            ok = run_piper(text, tmp_wav, length_scale=length)
            if not ok:
                print(f"    FAILED (piper)")
                continue

            if kind == "heard":
                ok = apply_radio_effect(tmp_wav, str(mp3_out), voice=voice)
            else:
                ok = apply_clean_tts(tmp_wav, str(mp3_out))

            if ok:
                size = mp3_out.stat().st_size
                print(f"    ✓  {size//1024}KB")
                manifest[fname] = {"kind": kind, "file": f"demo_audio/{fname}.mp3"}
            else:
                print(f"    FAILED (ffmpeg)")
        finally:
            try:
                os.unlink(tmp_wav)
            except Exception:
                pass

    # Write manifest for JS to load
    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest → {manifest_path}")
    print(f"Done — {len(manifest)} clips ready.")


if __name__ == "__main__":
    main()
