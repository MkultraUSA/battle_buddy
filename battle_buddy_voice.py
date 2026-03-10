#!/usr/bin/env python3
"""
Battle Buddy — Voice Command Listener  v0.7.0
==============================================
State machine:
  LISTENING  →  "Hey Battle Buddy"   →  speak "Yes sir"     →  COMMAND
  COMMAND    →  "Sitrep"             →  show/read sitrep    →  LISTENING
  COMMAND    →  "Ask Claude"         →  enter Q&A mode      →  ASK
  COMMAND    →  timeout              →                      →  LISTENING
  ASK        →  (any question)       →  Claude answers      →  ASK (loop)
  ASK        →  "Leave Claude"       →  resume live feed    →  LISTENING

Usage:
    python3 battle_buddy_voice.py
    python3 battle_buddy_voice.py --debug   # print all transcriptions
"""

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import anthropic
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

# ── Config ──────────────────────────────────────────────────────────────────
_config_env = Path(__file__).parent / "config.env"
if _config_env.exists():
    for _line in _config_env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL  = "claude-sonnet-4-6"
PIPE_PATH     = "/tmp/battle_buddy_display.pipe"
PIPER_BIN     = "/home/pi/voice-claude/bin/piper"
PIPER_MODEL   = "/home/pi/voice-claude/piper-voices/en_US-lessac-medium.onnx"
SCRIPT_DIR    = Path(__file__).parent
CHIME_WAV     = SCRIPT_DIR / "chime.wav"

# Audio
SAMPLE_RATE   = 16_000
STEP_SEC      = 0.5           # sliding step for wake detection
CHUNK_SEC     = 3.0           # window size for wake detection
SILENCE_RMS   = 0.005         # below = silence
SPEECH_RMS    = 0.015         # above = speech detected
COMMAND_TIMEOUT = 12.0        # seconds to wait for a command after "Yes sir"
SILENCE_END_SEC = 2.0         # seconds of silence that ends an utterance
MAX_UTTERANCE_SEC = 20.0      # max recording length in COMMAND/ASK mode

STEP_SAMPLES  = int(SAMPLE_RATE * STEP_SEC)
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_SEC)

WAKE_PHRASES  = [
    "hey battle buddy",
    "hey, battle buddy",
    "a battle buddy",
    "battle buddy",
]

CLAUDE_SYSTEM = """You are Battle Buddy, a helpful AI assistant. \
Answer questions clearly and concisely — keep responses to 3 to 5 sentences unless the user \
asks for more detail. Use plain speech only: no markdown, no bullet points, no numbered lists, \
no asterisks, no special characters. Your responses will be read aloud immediately."""
# ────────────────────────────────────────────────────────────────────────────

_debug = False


def dprint(*args, **kwargs):
    if _debug:
        print(*args, **kwargs, flush=True)


# ── Display pipe ─────────────────────────────────────────────────────────────

def display(msg: str):
    try:
        with open(PIPE_PATH, "w") as f:
            f.write(msg + "\n")
    except OSError:
        pass


# ── Piper TTS ────────────────────────────────────────────────────────────────

def speak(text: str):
    """Synthesize text via Piper and play it."""
    print(f"[voice] speak: {text}", flush=True)
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        subprocess.run(
            [PIPER_BIN, "--model", PIPER_MODEL, "--output_file", wav_path],
            input=text.encode(),
            capture_output=True,
            timeout=30,
        )
        subprocess.run(["aplay", "-q", wav_path], timeout=60)
    except Exception as e:
        print(f"[voice] speak error: {e}", flush=True)
    finally:
        try:
            os.unlink(wav_path)
        except Exception:
            pass


def play_chime():
    try:
        subprocess.run(["aplay", "-q", str(CHIME_WAV)], stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass



# ── Claude Q&A ───────────────────────────────────────────────────────────────

def ask_claude(history: list, question: str) -> str:
    """
    Send conversation history + new question to Claude.
    history is a list of {"role": "user"|"assistant", "content": str} dicts.
    Returns plain text answer.
    """
    if not ANTHROPIC_API_KEY:
        return "No API key configured."
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        messages = history + [{"role": "user", "content": question}]
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=CLAUDE_SYSTEM,
            messages=messages,
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Error contacting Claude: {e}"


# ── Audio helpers ─────────────────────────────────────────────────────────────

def record_utterance(stream) -> np.ndarray:
    """
    Record from stream until SILENCE_END_SEC of silence after speech is heard,
    or MAX_UTTERANCE_SEC total.  Returns float32 mono array at SAMPLE_RATE.
    """
    chunks = []
    speech_detected = False
    silence_since = None
    elapsed = 0.0

    while elapsed < MAX_UTTERANCE_SEC:
        data, _ = stream.read(STEP_SAMPLES)
        chunk = data.flatten()
        chunks.append(chunk)
        elapsed += STEP_SEC
        rms = float(np.sqrt(np.mean(chunk ** 2)))

        if rms >= SPEECH_RMS:
            speech_detected = True
            silence_since = None
        elif speech_detected:
            if silence_since is None:
                silence_since = elapsed
            elif elapsed - silence_since >= SILENCE_END_SEC:
                break  # natural end of utterance

    return np.concatenate(chunks) if chunks else np.zeros(STEP_SAMPLES, dtype=np.float32)


def transcribe(model: WhisperModel, audio: np.ndarray) -> str:
    segments, _ = model.transcribe(audio, beam_size=1, language="en", vad_filter=False)
    return " ".join(s.text for s in segments).strip()


def contains(text: str, phrases) -> bool:
    t = text.lower()
    return any(p in t for p in phrases)


# ── State machine ─────────────────────────────────────────────────────────────

def run_sitrep_blocking():
    """Generate + speak 4h sitrep. Blocks until complete."""
    display("STATUS: Generating sitrep…")
    script = SCRIPT_DIR / "battle_buddy_summary.py"
    subprocess.run(
        [sys.executable, str(script), "--hours", "4", "--speak"],
        cwd=str(SCRIPT_DIR),
    )


def main():
    global _debug

    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _debug = args.debug

    print("[voice] Loading Whisper tiny model…", flush=True)
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    print("[voice] Model ready.", flush=True)

    if args.device is not None:
        dev_info = sd.query_devices(args.device)
    else:
        dev_info = sd.query_devices(kind="input")
    print(f"[voice] Mic: {dev_info['name']}", flush=True)

    display("STATUS: Voice listener active")
    print("[voice] Listening for 'Hey Battle Buddy'…", flush=True)

    # Sliding wake-word buffer
    wake_buf = np.zeros(0, dtype=np.float32)

    with sd.InputStream(
        device=args.device,
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=STEP_SAMPLES,
    ) as stream:

        state = "LISTENING"

        while True:

            # ── LISTENING ─────────────────────────────────────────────────
            if state == "LISTENING":
                step, _ = stream.read(STEP_SAMPLES)
                step = step.flatten()

                wake_buf = np.concatenate([wake_buf, step])
                if len(wake_buf) > CHUNK_SAMPLES:
                    wake_buf = wake_buf[-CHUNK_SAMPLES:]
                if len(wake_buf) < CHUNK_SAMPLES:
                    continue

                rms = float(np.sqrt(np.mean(wake_buf ** 2)))
                if rms < SILENCE_RMS:
                    continue

                text = transcribe(model, wake_buf)
                dprint(f"[wake] {text}")

                if contains(text, WAKE_PHRASES):
                    print("[voice] Wake phrase detected — entering COMMAND", flush=True)
                    play_chime()
                    time.sleep(0.3)
                    threading.Thread(target=speak, args=("Yes sir.",), daemon=True).start()
                    time.sleep(1.2)   # let "Yes sir" start before listening
                    wake_buf = np.zeros(0, dtype=np.float32)
                    state = "COMMAND"

            # ── COMMAND ───────────────────────────────────────────────────
            elif state == "COMMAND":
                display("STATUS: Listening for command…")
                print("[voice] Waiting for command (Sitrep / Ask Claude)…", flush=True)

                audio = record_utterance(stream)
                text = transcribe(model, audio)
                print(f"[voice] Command heard: '{text}'", flush=True)

                t = text.lower()

                if not t or len(t) < 2:
                    # No speech detected — timeout back to listening
                    print("[voice] No command heard, returning to LISTENING", flush=True)
                    display("STATUS: Voice listener active")
                    state = "LISTENING"

                elif contains(t, ["sitrep", "sit rep", "sit-rep"]):
                    print("[voice] Command: SITREP", flush=True)
                    display("FREEZE")
                    display("CLEAR")
                    display("STATUS: Generating sitrep…")
                    run_sitrep_blocking()
                    display("UNFREEZE")
                    display("STATUS: Voice listener active")
                    state = "LISTENING"

                elif contains(t, ["ask claude", "ask cloud", "ask clod"]):
                    print("[voice] Command: ASK CLAUDE", flush=True)
                    display("FREEZE")
                    speak("Ready. Go ahead.")
                    conversation_history = []
                    state = "ASK"

                else:
                    # Unrecognised command — prompt and stay in COMMAND briefly
                    print(f"[voice] Unrecognised command: '{text}', re-prompting", flush=True)
                    speak("Sorry, I didn't catch that. Say Sitrep or Ask Claude.")
                    # one more chance
                    audio2 = record_utterance(stream)
                    text2 = transcribe(model, audio2)
                    t2 = text2.lower()
                    print(f"[voice] Retry command: '{text2}'", flush=True)

                    if contains(t2, ["sitrep", "sit rep"]):
                        display("FREEZE")
                        display("CLEAR")
                        run_sitrep_blocking()
                        display("UNFREEZE")
                        display("STATUS: Voice listener active")
                        state = "LISTENING"
                    elif contains(t2, ["ask claude", "ask cloud"]):
                        display("FREEZE")
                        speak("Ready. Go ahead.")
                        conversation_history = []
                        state = "ASK"
                    else:
                        display("STATUS: Voice listener active")
                        state = "LISTENING"

            # ── ASK ───────────────────────────────────────────────────────
            elif state == "ASK":
                display("STATUS: Ask Claude — say 'Leave Claude' to exit")
                print("[voice] ASK mode — listening for question…", flush=True)

                audio = record_utterance(stream)
                text = transcribe(model, audio)
                print(f"[voice] Question: '{text}'", flush=True)

                if not text.strip():
                    continue  # silence, keep waiting

                if contains(text, ["leave claude", "leave clod", "leave cloud"]):
                    print("[voice] Leaving ASK mode", flush=True)
                    speak("Roger. Returning to monitor.")
                    display("UNFREEZE")
                    display("STATUS: Voice listener active")
                    state = "LISTENING"
                    continue

                # Send to Claude with full conversation history
                display(f"AGENT: Q: {text}")
                display("STATUS: Asking Claude…")
                answer = ask_claude(conversation_history, text)
                print(f"[voice] Claude: {answer}", flush=True)
                display(f"AGENT: {answer}")
                # Append to history so follow-ups have context
                conversation_history.append({"role": "user", "content": text})
                conversation_history.append({"role": "assistant", "content": answer})
                speak(answer)
                # Stay in ASK state for follow-up questions


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[voice] Stopped.", flush=True)
        display("UNFREEZE")
        display("STATUS: Voice listener stopped")
