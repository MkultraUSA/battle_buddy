#!/usr/bin/env python3
"""
Battle Buddy — Broadcastify Calls Listener  v1.0

Polls the Broadcastify Calls API for a playlist (Fire/EMS or any public
playlist), downloads each new individual call audio file, transcribes with
faster-whisper, and writes to the radio log in the same format as the
MP3 stream listener so radio_parser.py can process it unchanged.

Requires a Broadcastify premium account for full playlist access.
Public playlists (like Fire/EMS) may work without authentication.

Usage:
    python3 calls_listener.py [--playlist UUID] [--stream calls] [--model small]

Default playlist: ecbfd29b-59dd-11f0-9e04-0e98d5b32039 (Austin-Travis County Fire/EMS)

Designed to run as a systemd service:
    battle-buddy-calls.service
"""

import argparse
import datetime
import json
import os
import random
import signal
import string
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "1.0"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PLAYLIST = "ecbfd29b-59dd-11f0-9e04-0e98d5b32039"  # Austin Fire/EMS
DEFAULT_STREAM   = "calls"
DEFAULT_MODEL    = "small"
DEFAULT_PIPE     = "/tmp/battle_buddy_display.pipe"
POLL_INTERVAL    = 5.0       # seconds between API polls
LOG_DIR          = os.path.expanduser("~/battle_buddy/logs")

CALLS_API    = "https://www.broadcastify.com/calls/apis/live-calls"
CDN_BASE     = "https://calls.broadcastify.com"
CDN_AI_BASE  = "https://calls-ai-1.broadcastify.com"

HEADERS = {
    "User-Agent":       "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0",
    "Accept":           "application/json, text/javascript, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer":          "https://www.broadcastify.com/calls/playlists/",
    "Content-Type":     "application/x-www-form-urlencoded",
}

running = True
model   = None


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

def on_quit(signum, frame):
    global running
    print("\n[Battle Buddy Calls] Shutting down...", flush=True)
    running = False


signal.signal(signal.SIGINT,  on_quit)
signal.signal(signal.SIGTERM, on_quit)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def send_to_display(pipe_path: str, message: str):
    try:
        if os.path.exists(pipe_path):
            with open(pipe_path, "w") as p:
                p.write(message + "\n")
    except Exception as e:
        print(f"[Display] pipe error: {e}", flush=True)


def get_log_path(stream_name: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    date_str = datetime.datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"radio_{stream_name}_{date_str}.log")


def log_entry(log_path: str, kind: str, text: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] [{kind}] {text}\n")
    except Exception as e:
        print(f"[Log] write error: {e}", flush=True)


def random_session_key() -> str:
    """Generate a client-side session key matching Broadcastify JS format."""
    chars = string.hexdigits[:16]
    part1 = "".join(random.choices(chars, k=8))
    part2 = "".join(random.choices(chars, k=4))
    return f"{part1}-{part2}"


def build_audio_url(call: dict) -> str:
    """Construct the CDN audio URL from a call record."""
    h       = call.get("hash", "")
    sys_id  = call.get("systemId") or call.get("sid", "0")
    fname   = call.get("filename", "")
    enc     = call.get("enc", "m4a")
    base    = CDN_AI_BASE if call.get("transcribe") == 1 else CDN_BASE
    if h:
        return f"{base}/{h}/{sys_id}/{fname}.{enc}"
    return f"{CDN_BASE}/{sys_id}/{fname}.{enc}"


# ---------------------------------------------------------------------------
# Broadcastify Calls API
# ---------------------------------------------------------------------------

def poll_calls(playlist_uuid: str, pos: int, session_key: str,
               do_init: bool = False, cookie: str = "") -> dict | None:
    """POST to the live-calls API and return the parsed JSON response."""
    body = urllib.parse.urlencode({
        "playlist_uuid": playlist_uuid,
        "pos":           pos,
        "sessionKey":    session_key,
        "groups":        "",
        "systemId":      "0",
        "sid":           "0",
        **({"doInit": "1"} if do_init else {}),
    }).encode()

    hdrs = dict(HEADERS)
    if cookie:
        hdrs["Cookie"] = cookie

    req = urllib.request.Request(CALLS_API, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[calls] API HTTP {e.code}", flush=True)
    except Exception as e:
        print(f"[calls] API error: {e}", flush=True)
    return None


def download_audio(url: str, dest: str) -> bool:
    """Download a call audio file to dest path. Returns True on success."""
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            Path(dest).write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"[calls] download error {url}: {e}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

def load_model(model_size: str):
    global model
    print(f"[Whisper] Loading {model_size} model...", flush=True)
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        print("[Whisper] Model ready.", flush=True)
    except ImportError:
        print("[Whisper] faster-whisper not installed.", flush=True)
        sys.exit(1)


def transcribe(audio_path: str) -> str:
    """Transcribe an audio file. Returns text or empty string."""
    if model is None:
        return ""
    try:
        segs, _ = model.transcribe(
            audio_path,
            language="en",
            vad_filter=False,
            beam_size=3,
        )
        return " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:
        print(f"[Whisper] transcribe error: {e}", flush=True)
        return ""


# ---------------------------------------------------------------------------
# Broadcastify login (fallback if playlist requires auth)
# ---------------------------------------------------------------------------

def broadcastify_login(username: str, password: str) -> str:
    """
    Log in to Broadcastify and return the session cookie string.
    Returns empty string on failure.
    """
    if not username or not password:
        return ""
    body = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "action":   "auth",
        "redirect": "https://www.broadcastify.com/",
    }).encode()
    req = urllib.request.Request(
        "https://www.broadcastify.com/login/",
        data=body,
        headers={
            "User-Agent":   HEADERS["User-Agent"],
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer":      "https://www.broadcastify.com/login/",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        resp = opener.open(req, timeout=15)
        # Extract Set-Cookie headers from the redirect chain
        cookies = []
        for handler in opener.handlers:
            if hasattr(handler, "cookiejar"):
                for c in handler.cookiejar:
                    cookies.append(f"{c.name}={c.value}")
        if cookies:
            print(f"[auth] Logged in, {len(cookies)} cookie(s)", flush=True)
            return "; ".join(cookies)
        print("[auth] Login appeared to succeed but no cookies returned", flush=True)
    except Exception as e:
        print(f"[auth] Login error: {e}", flush=True)
    return ""


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=f"Battle Buddy Calls Listener v{VERSION}")
    ap.add_argument("--playlist", default=DEFAULT_PLAYLIST,
                    help="Broadcastify Calls playlist UUID")
    ap.add_argument("--stream",   default=DEFAULT_STREAM,
                    help="Stream name for log file (default: calls)")
    ap.add_argument("--model",    default=DEFAULT_MODEL,
                    help="Whisper model size (default: small)")
    ap.add_argument("--pipe",     default=DEFAULT_PIPE,
                    help="Display pipe path")
    ap.add_argument("--no-display", action="store_true",
                    help="Suppress display pipe output")
    args = ap.parse_args()

    username = os.environ.get("BROADCASTIFY_USER", "")
    password = os.environ.get("BROADCASTIFY_PASS", "")

    print(f"[calls] Battle Buddy Calls Listener v{VERSION}", flush=True)
    print(f"[calls] Playlist : {args.playlist}", flush=True)
    print(f"[calls] Stream   : {args.stream}", flush=True)
    print(f"[calls] Model    : {args.model}", flush=True)

    load_model(args.model)

    log_path    = get_log_path(args.stream)
    session_key = random_session_key()
    seen_ids    = set()
    pos         = 0
    cookie      = ""
    first_poll  = True

    pipe_path   = args.pipe if not args.no_display else ""

    print(f"[calls] Log      : {log_path}", flush=True)
    print(f"[calls] Polling every {POLL_INTERVAL}s...", flush=True)

    while running:
        data = poll_calls(args.playlist, pos, session_key,
                          do_init=first_poll, cookie=cookie)

        if data is None:
            # API returned error — try logging in if we haven't
            if not cookie and username:
                print("[calls] Attempting Broadcastify login...", flush=True)
                cookie = broadcastify_login(username, password)
            time.sleep(POLL_INTERVAL * 2)
            continue

        first_poll = False
        calls      = data.get("calls") or []
        new_pos    = data.get("lastPos", pos)

        for call in calls:
            call_id = call.get("id", "")
            if call_id in seen_ids:
                continue
            seen_ids.add(call_id)

            talkgroup = call.get("display") or call.get("descr") or "Unknown"
            grouping  = call.get("grouping", "")
            ts_unix   = call.get("ts", 0)
            ts_str    = datetime.datetime.fromtimestamp(ts_unix).strftime(
                "%Y-%m-%d %H:%M:%S") if ts_unix else ""

            tg_label = f"{grouping} — {talkgroup}" if grouping else talkgroup

            # Notify display of talkgroup
            if pipe_path:
                send_to_display(pipe_path, f"TALKGROUP: {tg_label}")

            # Log talkgroup change
            log_entry(log_path, "TALKGROUP", tg_label)

            # Download and transcribe
            audio_url = build_audio_url(call)
            enc       = call.get("enc", "m4a")

            with tempfile.NamedTemporaryFile(suffix=f".{enc}", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                ok = download_audio(audio_url, tmp_path)
                if not ok:
                    continue

                text = transcribe(tmp_path)
                if not text:
                    print(f"[{args.stream}:{call_id}] No speech detected.", flush=True)
                    continue

                label = f"[{talkgroup}] {text}"
                print(f"[{args.stream}:{call_id}] {label}", flush=True)

                # Write to radio log (same format as MP3 listener)
                log_entry(log_path, "HEARD",
                          f"{text} | TALKGROUP: {tg_label}")

                # Send to display
                if pipe_path:
                    send_to_display(pipe_path, f"HEARD: [{talkgroup}] {text}")

            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        # Advance cursor
        if new_pos and new_pos != pos:
            pos = new_pos + 1

        time.sleep(POLL_INTERVAL)

    print("[calls] Stopped.", flush=True)


if __name__ == "__main__":
    main()
