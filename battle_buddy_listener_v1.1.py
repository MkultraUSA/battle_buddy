#!/usr/bin/env python3
# Battle Buddy Listener v1.1.0
# Continuous audio capture and transcription pipeline
#
# Changes in v1.1:
#   - Replaced pw-record (local audio device) with ffmpeg stream capture
#   - Supports multiple simultaneous Broadcastify streams
#   - Stream URL configured via --stream argument
#   - Falls back to local audio device if --stream not specified
#   - Added --stream-name for log file naming (e.g. "law", "fire", "ems")

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

VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Broadcastify stream URLs — Premium authenticated (no ads)
# Format: https://USERNAME:PASSWORD@audio.broadcastify.com/FEEDID.mp3
# ---------------------------------------------------------------------------
STREAMS = {
    "law":  "https://Mkultra2000:Egbdf2026!@audio.broadcastify.com/14439.mp3",  # Travis County Law Enforcement
    "fire": "https://Mkultra2000:Egbdf2026!@audio.broadcastify.com/28517.mp3",  # Austin-Travis County Fire & EMS
    "ems":  "https://Mkultra2000:Egbdf2026!@audio.broadcastify.com/21284.mp3",  # Austin-Travis County EMS Official
}

DEFAULT_STREAM      = "law"
DEFAULT_MODEL       = "small"
DEFAULT_CHUNK       = 15
DEFAULT_PIPE        = "/tmp/battle_buddy_display.pipe"
DEFAULT_SILENCE_DB  = -40.0
DEFAULT_SOURCE      = 50
SAMPLE_RATE         = 16000
CHANNELS            = 1
LOG_DIR             = os.path.expanduser("~/battle_buddy/logs")

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


def get_log_path(stream_name):
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"radio_{stream_name}_{date_str}.log")


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


# ---------------------------------------------------------------------------
# STREAM RECORDING via ffmpeg
# ---------------------------------------------------------------------------

def record_chunk_stream(stream_url, duration, output_path):
    """Capture a chunk of audio from a Broadcastify stream URL via ffmpeg."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i", stream_url,
        "-t", str(duration),
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        "-f", "wav",
        output_path
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False        # IMPORTANT: shell=False avoids ! history expansion
        )
        proc.wait(timeout=duration + 15)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"[Record] ffmpeg timeout on chunk")
        return False
    except Exception as e:
        print(f"[Record] Stream error: {e}")
        return False


# ---------------------------------------------------------------------------
# LOCAL AUDIO RECORDING via pw-record (fallback)
# ---------------------------------------------------------------------------

def find_hdmi_source():
    result = subprocess.run(['pactl', 'list', 'sources', 'short'],
                            capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if 'hdmi' in line.lower() and 'monitor' in line.lower():
            parts = line.split()
            if parts:
                print(f"[Audio] Auto-detected HDMI monitor source: {parts[0]}")
                return int(parts[0])
    print("[Audio] HDMI source not found, using default 50")
    return 50


def record_chunk_local(source, duration, output_path):
    """Capture audio from local PipeWire audio device (original method)."""
    cmd = [
        "pw-record",
        "--target",   str(source),
        "--rate",     str(SAMPLE_RATE),
        "--channels", str(CHANNELS),
        "--format",   "s16",
        output_path
    ]
    try:
        proc = subprocess.Popen(cmd,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        time.sleep(duration)
        proc.terminate()
        proc.wait(timeout=3)
        return True
    except Exception as e:
        print(f"[Record] Local audio error: {e}")
        return False


# ---------------------------------------------------------------------------
# WHISPER
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# MAIN LISTEN LOOP
# ---------------------------------------------------------------------------

def listen_loop(args):
    global running, model

    # Resolve stream URL or fall back to local audio
    stream_url  = STREAMS.get(args.stream) or args.stream or None
    stream_name = args.stream if args.stream else "local"
    use_stream  = stream_url is not None

    log_path = args.log or get_log_path(stream_name)

    print(f"[Battle Buddy Listener] v{VERSION} starting...")
    if use_stream:
        print(f"  Stream : {stream_url}")
    else:
        print(f"  Source : PipeWire target {args.source}")
    print(f"  Name   : {stream_name}")
    print(f"  Model  : {args.model}")
    print(f"  Chunk  : {args.chunk}s")
    print(f"  Log    : {log_path}")
    print(f"  Display: {args.display}")
    print()

    # Local audio fallback — auto-detect HDMI source
    if not use_stream and args.source == DEFAULT_SOURCE:
        args.source = find_hdmi_source()

    model = load_model(args.model)

    send_to_display(args.display,
                    f"STATUS: Battle Buddy Listener v{VERSION} -- monitoring {stream_name}...")
    log_entry(log_path, "SYSTEM",
              f"Listener started. Model: {args.model}, "
              f"Source: {stream_url if use_stream else args.source}")

    chunk_count = 0
    skip_count  = 0

    while running:
        chunk_count += 1
        tmp_path = tempfile.mktemp(suffix=".wav", prefix=f"bb_{stream_name}_")
        try:
            send_to_display(args.display,
                            f"STATUS: [{stream_name}] Listening... (chunk {chunk_count})")

            if use_stream:
                success = record_chunk_stream(stream_url, args.chunk, tmp_path)
            else:
                success = record_chunk_local(args.source, args.chunk, tmp_path)

            if not success or not os.path.exists(tmp_path):
                print(f"[{stream_name}] Chunk {chunk_count} failed, retrying in 5s...")
                time.sleep(5)
                continue

            db = get_rms_db(tmp_path)
            if db < args.silence:
                skip_count += 1
                print(f"[{stream_name}:{chunk_count}] Silent ({db:.1f} dB) "
                      f"-- skipping ({skip_count} skipped)")
                continue

            print(f"[{stream_name}:{chunk_count}] Audio {db:.1f} dB -- transcribing...")
            send_to_display(args.display, f"STATUS: [{stream_name}] Transcribing...")

            transcript = transcribe(model, tmp_path)
            if not transcript:
                print(f"[{stream_name}:{chunk_count}] No speech detected.")
                continue

            print(f"[{stream_name}:{chunk_count}] {transcript}")
            send_to_display(args.display, f"HEARD: {transcript}")
            log_entry(log_path, "HEARD", transcript)

        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    send_to_display(args.display, f"STATUS: [{stream_name}] Listener stopped.")
    log_entry(log_path, "SYSTEM", "Listener stopped.")
    print(f"[Battle Buddy Listener] {stream_name} stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=f"Battle Buddy Listener v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Listen to Travis County Law Enforcement (default)
  cd ~/battle_buddy
  python3 battle_buddy_listener_v1.1.py --stream law

  # Listen to Fire & EMS
  python3 battle_buddy_listener_v1.1.py --stream fire

  # Listen to all three in separate screen sessions
  screen -S law  && python3 battle_buddy_listener_v1.1.py --stream law
  screen -S fire && python3 battle_buddy_listener_v1.1.py --stream fire
  screen -S ems  && python3 battle_buddy_listener_v1.1.py --stream ems

  # Use local audio device (original behavior)
  python3 battle_buddy_listener_v1.1.py --source 50
        """
    )
    parser.add_argument("--stream",  type=str,   default=DEFAULT_STREAM,
                        help="Stream name (law/fire/ems) or direct URL. Default: law")
    parser.add_argument("--source",  type=int,   default=DEFAULT_SOURCE,
                        help="PipeWire source ID (only used if --stream not set)")
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
