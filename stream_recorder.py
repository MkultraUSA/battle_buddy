#!/usr/bin/env python3
"""
Battle Buddy — Broadcastify Stream Recorder
Pulls a live Broadcastify audio stream via ffmpeg, segments by silence,
and POSTs each call to the Battle Buddy /receive endpoint.
"""

import base64
import io
import json
import math
import os
import struct
import subprocess
import time
import urllib.request
import wave

STREAM_URL  = os.environ.get("STREAM_URL",  "https://broadcastify.cdnstream1.com/14439")
STREAM_USER = os.environ.get("STREAM_USER", "STREAM_USER_REMOVED")
STREAM_PASS = os.environ.get("STREAM_PASS", "NC_PASS_REMOVED")
VM_URL      = os.environ.get("BB_VM_URL",   "http://127.0.0.1:9001/receive")
STREAM_TAG  = os.environ.get("STREAM_TAG",  "Austin/Travis Scanner")

SAMPLE_RATE   = 8000    # 8kHz mono — matches Whisper pipeline
CHUNK_SECS    = 0.2     # process in 0.2s chunks
CHUNK_BYTES   = int(SAMPLE_RATE * CHUNK_SECS) * 2  # 16-bit samples
SILENCE_DB    = -38     # dB threshold below which = silence
SILENCE_SECS  = 1.8     # gap this long ends a call
SILENCE_CHUNKS= int(SILENCE_SECS / CHUNK_SECS)
MIN_CALL_SECS = 1.0     # discard shorter clips
MAX_CALL_SECS = 90      # force-flush at this length
RECONNECT_WAIT= 10      # seconds before reconnect on failure


def rms_db(data: bytes) -> float:
    if len(data) < 2:
        return -100.0
    samples = struct.unpack(f"<{len(data)//2}h", data)
    sq = sum(x * x for x in samples)
    rms = math.sqrt(sq / len(samples))
    return 20 * math.log10(rms / 32768) if rms > 0 else -100.0


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def post_call(pcm: bytes, duration: float):
    payload = json.dumps({
        "audio_b64": base64.b64encode(pcm_to_wav(pcm)).decode(),
        "tgid": 0,
        "tag":  STREAM_TAG,
        "node": "broadcastify",
    }).encode()
    try:
        req = urllib.request.Request(
            VM_URL, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.getcode()
        print(f"[{time.strftime('%H:%M:%S')}] posted dur={duration:.1f}s → {code}", flush=True)
    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')}] POST error: {exc}", flush=True)


def build_ffmpeg_cmd() -> list:
    auth_url = STREAM_URL.replace("://", f"://{STREAM_USER}:{STREAM_PASS}@", 1)
    return [
        "ffmpeg", "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", auth_url,
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-f", "s16le",
        "pipe:1",
    ]


def capture_loop():
    call_buf     = bytearray()
    in_call      = False
    call_start   = 0.0
    silence_count= 0

    proc = subprocess.Popen(
        build_ffmpeg_cmd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    print(f"[{time.strftime('%H:%M:%S')}] stream connected", flush=True)

    try:
        while True:
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                break

            now   = time.time()
            db    = rms_db(data)
            voice = db > SILENCE_DB

            if voice:
                if not in_call:
                    in_call      = True
                    call_start   = now
                    call_buf     = bytearray()
                    silence_count= 0
                call_buf.extend(data)
                silence_count = 0
            else:
                if in_call:
                    call_buf.extend(data)
                    silence_count += 1
                    call_dur = now - call_start

                    if silence_count >= SILENCE_CHUNKS or call_dur >= MAX_CALL_SECS:
                        in_call = False
                        if call_dur >= MIN_CALL_SECS:
                            post_call(bytes(call_buf), call_dur)
                        call_buf      = bytearray()
                        silence_count = 0
    finally:
        proc.terminate()
        proc.wait()


def main():
    print(f"[stream_recorder] starting  tag={STREAM_TAG!r}", flush=True)
    while True:
        try:
            capture_loop()
        except Exception as exc:
            print(f"[stream_recorder] error: {exc}", flush=True)
        print(f"[stream_recorder] reconnecting in {RECONNECT_WAIT}s...", flush=True)
        time.sleep(RECONNECT_WAIT)


if __name__ == "__main__":
    main()
