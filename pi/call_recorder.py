#!/usr/bin/env python3
"""
Battle Buddy — Call Recorder v2
Captures decoded audio from OP25 via UDP 23456 (S16LE 8000Hz mono).
Tracks the active talkgroup by following the OP25 systemd journal in
real time — no HTTP polling needed, no TGID 0.

When OP25 grants a voice channel it logs:
  voice update:  tg(1487), rid(0), freq(852.637500), ...
We parse that to know exactly which talkgroup the audio belongs to.

Environment:
    BB_VM_URL    POST endpoint   (default: http://battlebuddy.example.local:9001/receive)
    AUDIO_PORT   UDP audio port  (default: 23456)
    TAGS_FILE    talkgroup TSV   (default: /home/pi/op25_data/gatrrs-tags.tsv)
"""

import base64
import io
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
import wave

VM_URL     = os.environ.get("BB_VM_URL",  "http://battlebuddy.example.local:9001/receive")
AUDIO_PORT = int(os.environ.get("AUDIO_PORT", "23456"))
TAGS_FILE  = os.environ.get("TAGS_FILE",  "/home/pi/op25_data/gatrrs-tags.tsv")

SAMPLE_RATE  = 8000
CALL_END_GAP = 2.0    # seconds of no packets = end of call
MIN_CALL_SECS = 0.5
MAX_CALL_SECS = 120

# ---------------------------------------------------------------------------
# Talkgroup tag table
# ---------------------------------------------------------------------------
_TAGS: dict[int, str] = {}

def load_tags():
    global _TAGS
    try:
        with open(TAGS_FILE) as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    try:
                        _TAGS[int(parts[0])] = parts[1]
                    except ValueError:
                        pass
        print(f"[call_recorder] loaded {len(_TAGS)} talkgroup tags", flush=True)
    except Exception as exc:
        print(f"[call_recorder] could not load tags: {exc}", flush=True)

# ---------------------------------------------------------------------------
# Real-time tgid tracker — follows OP25 journal
# ---------------------------------------------------------------------------
_current_tgid: int = 0
_current_tag:  str = ""
_tgid_lock = threading.Lock()
_VOICE_RE = re.compile(r'voice update:.*?tg\((\d+)\)')

def _journal_follower():
    """Follow op25-multi_rx journal, parse voice update lines."""
    global _current_tgid, _current_tag
    while True:
        try:
            proc = subprocess.Popen(
                ["journalctl", "-u", "op25-multi_rx", "-f",
                 "--output=cat", "--no-pager"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            for line in proc.stdout:
                m = _VOICE_RE.search(line)
                if m:
                    tgid = int(m.group(1))
                    tag  = _TAGS.get(tgid, f"TGID {tgid}")
                    with _tgid_lock:
                        _current_tgid = tgid
                        _current_tag  = tag
            proc.wait()
        except Exception as exc:
            print(f"[journal] error: {exc} — retrying", flush=True)
        time.sleep(3)


def get_current_tgid() -> tuple[int, str]:
    with _tgid_lock:
        return _current_tgid, _current_tag


# ---------------------------------------------------------------------------
# WAV + POST
# ---------------------------------------------------------------------------

def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def post_call(pcm: bytes, tgid: int, tag: str):
    wav      = pcm_to_wav(pcm)
    duration = len(pcm) / (SAMPLE_RATE * 2)
    payload  = json.dumps({
        "audio_b64": base64.b64encode(wav).decode(),
        "tgid": tgid,
        "tag":  tag,
        "node": "pi5",
    }).encode()
    try:
        req = urllib.request.Request(
            VM_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.getcode()
        print(
            f"[{time.strftime('%H:%M:%S')}] posted tgid={tgid} tag={tag!r} "
            f"dur={duration:.1f}s → {code}",
            flush=True,
        )
    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')}] POST error: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Main audio capture loop
# ---------------------------------------------------------------------------

def capture_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", AUDIO_PORT))
    sock.settimeout(0.5)

    print(f"[call_recorder] listening UDP {AUDIO_PORT}  VM={VM_URL}", flush=True)

    call_buf      = bytearray()
    in_call       = False
    call_start    = 0.0
    last_pkt      = 0.0
    call_tgid     = 0
    call_tag      = ""

    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            data = None

        now = time.time()

        if data:
            if not in_call:
                # Lock in the tgid at call start from the journal tracker
                call_tgid, call_tag = get_current_tgid()
                in_call    = True
                call_start = now
                call_buf   = bytearray()
                print(
                    f"[{time.strftime('%H:%M:%S')}] CALL START "
                    f"tgid={call_tgid} tag={call_tag!r}",
                    flush=True,
                )
            call_buf.extend(data)
            last_pkt = now

        if in_call:
            gap      = now - last_pkt
            call_dur = now - call_start

            # Check if OP25 has switched to a different tgid mid-call
            live_tgid, live_tag = get_current_tgid()
            if live_tgid and live_tgid != call_tgid and gap < 0.3:
                # OP25 tuned to a new call — flush current and start fresh
                if call_dur >= MIN_CALL_SECS:
                    post_call(bytes(call_buf), call_tgid, call_tag)
                call_tgid  = live_tgid
                call_tag   = live_tag
                call_start = now
                call_buf   = bytearray()
                print(
                    f"[{time.strftime('%H:%M:%S')}] TGID SWITCH → "
                    f"tgid={call_tgid} tag={call_tag!r}",
                    flush=True,
                )
                continue

            if gap >= CALL_END_GAP or call_dur >= MAX_CALL_SECS:
                in_call = False
                if call_dur >= MIN_CALL_SECS:
                    post_call(bytes(call_buf), call_tgid, call_tag)
                call_buf = bytearray()


if __name__ == "__main__":
    load_tags()
    threading.Thread(target=_journal_follower, daemon=True).start()
    # Brief pause so journal follower can catch up before first call
    time.sleep(1)
    capture_loop()
