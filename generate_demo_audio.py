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

# ── All lines from the scenario in order ──────────────────────────────────────
# (kind, filename, text)
LINES = [
    # Block 1 — Law
    ("heard", "l01", "Adam 7 show me out at FM 2222 and 620, traffic stop on a white Silverado."),
    ("heard", "l02", "Dispatch, run Texas plate Hotel-Kilo-Romeo 4-4-9."),
    ("heard", "l03", "Adam 7 that plate comes back clear, registered to a Kevin Flores, Austin."),
    ("heard", "l04", "Baker 12, I'm out at 6200 Springdale, welfare check on a female. Caller says she hasn't been seen in two days."),
    ("heard", "l05", "Baker 12, no answer at the door, going around back."),
    ("heard", "l06", "Dispatch, Baker 12, I'm gonna need EMS out here. Female is conscious, appears disoriented, possible overdose."),

    # Block 2 — Fire / EMS
    ("heard", "f01", "Engine 18, Rescue 18. Respond to 6200 Springdale Road, medical assist, Travis County Sheriff on scene."),
    ("heard", "f02", "Medic 4 is en route to Springdale, ETA 6 minutes."),
    ("heard", "f03", "Medic 4 on scene at 6200 Springdale. Female, mid-30s, altered mental status, suspected opiate overdose. Administering Narcan."),

    # Block 3 — Structure fire
    ("heard", "f04", "Engine 8, Ladder 1, Rescue 8. Structure fire, AFD Box 42-07, 4800 Manor Road. Investigate reported smoke showing from a two-story residential."),
    ("heard", "f05", "Engine 8 on scene, we've got smoke showing from the second floor, Alpha side. Laying a line, going in."),
    ("heard", "f06", "Engine 8, fire's in the wall cavity, second floor bedroom. Pulling ceiling, getting water on it."),
    ("heard", "f07", "Ladder 1 on scene, venting the roof. All civilians are confirmed out of the structure."),
    ("heard", "f08", "Engine 8, fire's knocked down, starting overhaul. Requesting salvage crew."),

    # Block 4 — Pursuit
    ("heard", "l07", "All units Adam-West, pursuit in progress. Black Honda northbound on I-35 from Ben White, unit failed to stop for a felony warrant stop. All units stay clear of the corridor."),
    ("heard", "l08", "Pursuit is at I-35 and Rundberg, vehicle now exiting onto Rundberg Lane westbound, speeds around 65."),
    ("heard", "l09", "Vehicle has stopped at 1200 Rundberg, subject fleeing on foot, setting up perimeter. Air support requested."),
    ("heard", "l10", "Suspect in custody at 1240 Rundberg Lane, no injuries. Units can stand down on perimeter."),

    # Block 5 — Agent / voice
    ("agent", "a01", "Yes sir."),
    ("agent", "a02", "Ready. Go ahead."),
    ("agent", "a03", "Tonight in Austin expect clear skies with a low around 58 degrees. Winds will be light out of the southeast at 5 to 10 miles per hour. No precipitation expected. A warm front moves in Thursday bringing a chance of storms by evening, but tonight looks clear."),
    ("agent", "a04", "Roger. Returning to monitor."),

    # Block 6 — Sitrep
    ("summary", "s01",
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


def apply_radio_effect(wav_in: str, mp3_out: str):
    """
    Apply scanner radio effect:
    - Bandpass 300-3000 Hz (voice frequency range)
    - Slight overdrive / saturation
    - Light noise floor
    - Mono, 22050 Hz
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", wav_in,
        "-af",
        (
            "highpass=f=300,"
            "lowpass=f=3200,"
            "volume=2.5,"
            "acompressor=threshold=-20dB:ratio=4:attack=5:release=50,"
            "anlmdn=s=0.003,"          # light noise reduction → adds slight artifact
            "volume=1.8"
        ),
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

    for kind, fname, text in LINES:
        mp3_out = OUT_DIR / f"{fname}.mp3"
        if mp3_out.exists():
            print(f"  {fname}.mp3 already exists, skipping")
            manifest[fname] = {"kind": kind, "file": f"demo_audio/{fname}.mp3"}
            continue

        print(f"  [{kind}] {fname}: {text[:60]}...")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = tmp.name

        try:
            length = 1.05 if kind == "summary" else 1.0
            ok = run_piper(text, tmp_wav, length_scale=length)
            if not ok:
                print(f"    FAILED (piper)")
                continue

            if kind == "heard":
                ok = apply_radio_effect(tmp_wav, str(mp3_out))
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
