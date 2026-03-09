#!/usr/bin/env python3
# Battle Buddy Listener v1.2.0
# Continuous audio capture and transcription pipeline
#
# Changes in v1.1:
#   - Replaced pw-record (local audio device) with ffmpeg stream capture
#   - Supports multiple simultaneous Broadcastify streams
#   - Stream URL configured via --stream argument
#   - Falls back to local audio device if --stream not specified
#   - Added --stream-name for log file naming (e.g. "law", "fire", "ems")
#
# Changes in v1.2:
#   - Captures StreamTitle metadata from Broadcastify ICY stream
#   - Logs active talkgroup name alongside each [HEARD] entry
#   - Passes talkgroup context to log for LLM enrichment
#   - StreamTitle logged as [TALKGROUP] entry when it changes

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
import urllib.request
import urllib.parse

VERSION = "1.2.0"

# ---------------------------------------------------------------------------
# Broadcastify stream URLs — credentials loaded from environment
# Set BROADCASTIFY_USER and BROADCASTIFY_PASS in config.env
# ---------------------------------------------------------------------------
_BUSER = os.environ.get("BROADCASTIFY_USER", "")
_BPASS = os.environ.get("BROADCASTIFY_PASS", "")
_BBASE = f"https://{_BUSER}:{_BPASS}@audio.broadcastify.com"

STREAMS = {
    "law":  f"{_BBASE}/14439.mp3",  # Travis County Law Enforcement
    "fire": f"{_BBASE}/28517.mp3",  # Austin-Travis County Fire & EMS
    "ems":  f"{_BBASE}/21284.mp3",  # Austin-Travis County EMS Official
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

running        = True
model          = None
current_title  = ""   # tracks last seen StreamTitle to detect changes


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
# STREAMTITLE METADATA — fetch ICY metadata from Broadcastify
# ---------------------------------------------------------------------------

def fetch_stream_title(stream_url: str) -> str:
    """
    Fetch the current ICY StreamTitle from the Broadcastify stream.
    Returns the talkgroup name string or empty string on failure.
    StreamTitle format examples:
      "Lakeway PD 1"
      "TCSO Baker-East"
      "APD George Ops"
    """
    try:
        # Parse credentials out of URL for urllib
        parsed   = urllib.parse.urlparse(stream_url)
        clean_url = stream_url.replace(f"{parsed.username}:{parsed.password}@", "")

        req = urllib.request.Request(clean_url)
        req.add_header("Icy-MetaData", "1")

        # Add basic auth
        if parsed.username:
            import base64
            creds = base64.b64encode(
                f"{parsed.username}:{parsed.password}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {creds}")

        with urllib.request.urlopen(req, timeout=5) as resp:
            # ICY metaint tells us how often metadata is embedded in stream
            metaint = int(resp.headers.get("icy-metaint", 0))
            if metaint == 0:
                return ""
            # Read up to metaint bytes to get past audio data
            resp.read(metaint)
            # Next byte is metadata length * 16
            meta_len_byte = resp.read(1)
            if not meta_len_byte:
                return ""
            meta_len = ord(meta_len_byte) * 16
            if meta_len == 0:
                return ""
            meta = resp.read(meta_len).decode("utf-8", errors="ignore").strip("\x00")
            # Parse StreamTitle='...'
            for part in meta.split(";"):
                if part.startswith("StreamTitle="):
                    title = part[len("StreamTitle="):].strip("'\" ")
                    return title
    except Exception:
        pass
    return ""


def poll_stream_title(stream_url: str, log_path: str,
                      stream_name: str, pipe_path: str,
                      interval: float = 30.0):
    """
    Background thread — polls StreamTitle every `interval` seconds.
    Logs [TALKGROUP] entry when the title changes and notifies the display.
    """
    global current_title, running
    while running:
        title = fetch_stream_title(stream_url)
        if title and title != current_title:
            current_title = title
            print(f"[{stream_name}] Talkgroup: {title}")
            log_entry(log_path, "TALKGROUP", title)
            send_to_display(pipe_path, f"TALKGROUP: {title}")
        time.sleep(interval)


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
            shell=False
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

    stream_url  = STREAMS.get(args.stream) or args.stream or None
    stream_name = args.stream if args.stream else "local"
    use_stream  = stream_url is not None

    log_path = args.log or get_log_path(stream_name)

    print(f"[Battle Buddy Listener] v{VERSION} starting...")
    if use_stream:
        print(f"  Stream : {stream_url.split('@')[-1]}")  # hide credentials in output
    else:
        print(f"  Source : PipeWire target {args.source}")
    print(f"  Name   : {stream_name}")
    print(f"  Model  : {args.model}")
    print(f"  Chunk  : {args.chunk}s")
    print(f"  Log    : {log_path}")
    print(f"  Display: {args.display}")
    print()

    if not use_stream and args.source == DEFAULT_SOURCE:
        args.source = find_hdmi_source()

    model = load_model(args.model)

    # Start StreamTitle polling thread
    if use_stream:
        title_thread = threading.Thread(
            target=poll_stream_title,
            args=(stream_url, log_path, stream_name, args.display, 30.0),
            daemon=True
        )
        title_thread.start()
        print(f"[{stream_name}] StreamTitle polling started (every 30s)")

    send_to_display(args.display,
                    f"STATUS: Battle Buddy Listener v{VERSION} -- monitoring {stream_name}...")
    log_entry(log_path, "SYSTEM",
              f"Listener started. Model: {args.model}, Stream: {stream_name}")

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

            # Log talkgroup context with the transcript if we have one
            talkgroup_context = f" [{current_title}]" if current_title else ""
            print(f"[{stream_name}:{chunk_count}]{talkgroup_context} {transcript}")
            heard_msg = f"[{current_title}] {transcript}" if current_title else transcript
            send_to_display(args.display, f"HEARD: {heard_msg}")
            log_entry(log_path, "HEARD",
                      f"{transcript} | TALKGROUP: {current_title}" if current_title else transcript)

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
  cd ~/battle_buddy

  # Listen to Travis County Law Enforcement (default)
  python3 battle_buddy_listener_v1.2.py --stream law

  # Listen to Fire & EMS
  python3 battle_buddy_listener_v1.2.py --stream fire

  # Listen to all three in separate screen sessions
  screen -S law  && python3 battle_buddy_listener_v1.2.py --stream law
  screen -S fire && python3 battle_buddy_listener_v1.2.py --stream fire
  screen -S ems  && python3 battle_buddy_listener_v1.2.py --stream ems

  # Use local audio device (original behavior)
  python3 battle_buddy_listener_v1.2.py --source 50
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
