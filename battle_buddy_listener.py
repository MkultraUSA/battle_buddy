#!/usr/bin/env python3
# Battle Buddy Listener v0.1.0
# Continuous audio capture and transcription pipeline

import os
import sys
import time
import wave
import signal
import struct
import datetime
import argparse
import tempfile
import threading
import subprocess
import math

VERSION = "0.1.0"
DEFAULT_SOURCE     = 50
DEFAULT_MODEL      = "small"
DEFAULT_CHUNK      = 15
DEFAULT_PIPE       = "/tmp/battle_buddy_display.pipe"
DEFAULT_SILENCE_DB = -40.0
SAMPLE_RATE        = 16000
CHANNELS           = 1
LOG_DIR            = os.path.expanduser("~/battle_buddy/logs")

running = True
model   = None


def on_quit(signum, frame):
    global running
    print("\n[Battle Buddy Listener] Shutting down...")
    running = False


signal.signal(signal.SIGINT,  on_quit)
signal.signal(signal.SIGTERM, on_quit)


def send_to_display(pipe_path, message):
    try:
        if os.path.exists(pipe_path):
            with open(pipe_path, "w") as p:
                p.write(message + "\n")
    except Exception as e:
        print(f"[Display] Could not send to pipe: {e}")


def get_log_path():
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"radio_{date_str}.log")


def log_entry(log_path, kind, text):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] [{kind}] {text}\n")
    except Exception as e:
        print(f"[Log] Could not write to log: {e}")


def get_rms_db(wav_path):
    try:
        with wave.open(wav_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            if not frames:
                return -100.0
            count  = len(frames) // 2
            shorts = struct.unpack(f"{count}h", frames)
            if count == 0:
                return -100.0
            rms = math.sqrt(sum(s * s for s in shorts) / count)
            if rms == 0:
                return -100.0
            return 20 * math.log10(rms / 32768.0)
    except Exception:
        return -100.0


def find_hdmi_source():
    import subprocess
    result = subprocess.run(['pactl', 'list', 'sources', 'short'], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if 'hdmi' in line.lower() and 'monitor' in line.lower():
            parts = line.split()
            if parts:
                print(f"[Audio] Auto-detected HDMI monitor source: {parts[0]}")
                return int(parts[0])
    print("[Audio] HDMI source not found, using default 50")
    return 50

def record_chunk(source, duration, output_path):
    cmd = [
        "pw-record",
        "--target", str(source),
        "--rate",   str(SAMPLE_RATE),
        "--channels", str(CHANNELS),
        "--format", "s16",
        output_path
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(duration)
        proc.terminate()
        proc.wait(timeout=3)
        return True
    except Exception as e:
        print(f"[Record] Error: {e}")
        return False


def load_model(model_size):
    print(f"[Whisper] Loading {model_size} model...")
    try:
        from faster_whisper import WhisperModel
        m = WhisperModel(model_size, device="cpu", compute_type="int8")
        print("[Whisper] Model loaded.")
        return m
    except ImportError:
        print("[Whisper] faster-whisper not installed.")
        sys.exit(1)


def transcribe(mdl, wav_path):
    try:
        segments, info = mdl.transcribe(
            wav_path,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return " ".join(parts)
    except Exception as e:
        print(f"[Whisper] Transcription error: {e}")
        return ""


def listen_loop(args):
    global running, model
    log_path = args.log or get_log_path()
    print(f"[Battle Buddy Listener] v{VERSION} starting...")
    print(f"  Source : PipeWire target {args.source}")
    print(f"  Model  : {args.model}")
    print(f"  Chunk  : {args.chunk}s")
    print(f"  Log    : {log_path}")
    print(f"  Display: {args.display}")
    print()
    if args.source == 50:
        args.source = find_hdmi_source()
    model = load_model(args.model)
    send_to_display(args.display, f"STATUS: Battle Buddy Listener v{VERSION} -- monitoring radio traffic...")
    log_entry(log_path, "SYSTEM", f"Listener started. Model: {args.model}, Source: {args.source}")
    chunk_count = 0
    skip_count  = 0
    while running:
        chunk_count += 1
        tmp_path = tempfile.mktemp(suffix=".wav", prefix="bb_chunk_")
        try:
            send_to_display(args.display, f"STATUS: Listening... (chunk {chunk_count})")
            success = record_chunk(args.source, args.chunk, tmp_path)
            if not success or not os.path.exists(tmp_path):
                print(f"[Record] Chunk {chunk_count} failed, retrying...")
                time.sleep(2)
                continue
            db = get_rms_db(tmp_path)
            if db < args.silence:
                skip_count += 1
                print(f"[Chunk {chunk_count}] Silent ({db:.1f} dB) -- skipping ({skip_count} skipped)")
                continue
            print(f"[Chunk {chunk_count}] Audio level {db:.1f} dB -- transcribing...")
            send_to_display(args.display, "STATUS: Transcribing...")
            transcript = transcribe(model, tmp_path)
            if not transcript:
                print(f"[Chunk {chunk_count}] No speech detected.")
                continue
            print(f"[Chunk {chunk_count}] {transcript}")
            send_to_display(args.display, f"HEARD: {transcript}")
            log_entry(log_path, "HEARD", transcript)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    send_to_display(args.display, "STATUS: Listener stopped.")
    log_entry(log_path, "SYSTEM", "Listener stopped.")
    print("[Battle Buddy Listener] Stopped.")


def main():
    parser = argparse.ArgumentParser(description="Battle Buddy Listener")
    parser.add_argument("--source",  type=int,   default=DEFAULT_SOURCE)
    parser.add_argument("--model",   type=str,   default=DEFAULT_MODEL)
    parser.add_argument("--chunk",   type=int,   default=DEFAULT_CHUNK)
    parser.add_argument("--log",     type=str,   default=None)
    parser.add_argument("--display", type=str,   default=DEFAULT_PIPE)
    parser.add_argument("--silence", type=float, default=DEFAULT_SILENCE_DB)
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(f"Battle Buddy Listener v{VERSION}")
        sys.exit(0)
    listen_loop(args)


if __name__ == "__main__":
    main()
