# Battle Buddy — VM Status & Next Steps
**VM:** 147.93.134.105 (Ubuntu 24.04)
**Date:** 2026-03-27
**Role:** Brain / Transcription / Dashboard (replacing lost NUC)

---

## What Is Running

### `/opt/battlebuddy/audio_receiver.py`
- Flask server on port 9001
- Accepts WAV audio POSTs from Pi 1 (`call_recorder.py`)
- Runs OpenAI Whisper (`base` model) to transcribe each call
- Extracts location keywords from transcript (streets, landmarks, county names)
- Falls back to talkgroup-based coordinates if no location found
- Stores everything in `/opt/battlebuddy/calls.db` (SQLite)
- Serves web UI at `http://147.93.134.105:9001/`
  - Live feed tab: all incoming calls with transcript
  - Map tab: Leaflet map, each call plotted as a colored dot by agency
  - Sitrep tab: auto-generated situation report for last 30/60/180/360 min

### Systemd service
- `systemctl status battlebuddy` — starts on boot, auto-restarts

### Pi 1 connection
- `call_recorder.py` on Pi 1 (192.168.1.103 / radiodesk.ddns.net) is running
- It records per-call audio via PulseAudio monitor, segments by silence + TGID
- POSTs each call as base64 WAV + metadata JSON to `http://147.93.134.105:9001/receive`

---

## What Is NOT Done — Priority Work

### 1. Incident-Based Channel Focus (THE BIG ONE)
The previous NUC had logic to:
- **Categorize active incidents** by type (structure fire, MCI, officer down, airport emergency, etc.)
- **Dynamically instruct Pi 1 to hold/focus** on the talkgroups handling that incident
- Example: if ABIA talkgroups go active, Pi 1 should lock onto those channels and prioritize recording them

**How to implement:**
- `audio_receiver.py` receives transcripts — scan them for incident keywords
- When a high-priority incident is detected, POST a hold command to Pi 1's OP25 API:
  ```
  POST http://192.168.1.103:8080/
  Body: [{"command": "hold", "arg1": <tgid>, "arg2": 0}]
  ```
- When incident clears (no activity for N minutes), release with:
  ```
  [{"command": "skip", "arg1": 0, "arg2": 0}]
  ```
- **IMPORTANT:** The first implementation of hold/skip didn't work right and needed adjustments. The user remembers this but not the specifics. Test carefully — do not assume the hold command works as documented on first try.

### Incident keyword categories to implement:
| Incident Type | Keywords | Talkgroups to Focus |
|---------------|----------|---------------------|
| Airport emergency | "airport", "abia", "aircraft", "runway", "crash" | 1471-1481 |
| Structure fire | "structure", "working fire", "fully involved" | AFD: 1121,1122,1147,1155,1371,1377,1378 |
| MCI / Mass casualty | "mass casualty", "mci", "multiple patients" | AFD + TCEMS |
| Officer down | "officer down", "shots fired", "10-99" | APD: 960-1027 |
| Hazmat | "hazmat", "chemical", "spill", "leak" | AFD channels |

### 2. Incident Tracker
- Group related calls into a single "incident" record
- Track incident start time, talkgroups involved, transcript summary
- Show incident timeline on the map (not just individual call dots)

### 3. Sitrep Quality
- Currently just a text dump of recent calls grouped by agency
- Should summarize by incident, not just by agency
- Should highlight escalating situations

### 4. No-IP / DDNS
- `kevcloud.ddns.net` needs to be configured in No-IP account (was missing from account listing)
- DUC client is compiled and installed at `/usr/local/bin/noip2` but not configured
- Once hostname exists in No-IP dashboard: run `/usr/local/bin/noip2 -C` to configure

### 5. Pi 1 collector.py hold logic
- `/home/pi/op25_data/collector.py` on Pi 1 has a basic hold: fires when 3+ fire/EMS channels active
- No release mechanism — once held, stays held forever
- This needs to be coordinated with the VM's incident detection so they don't fight each other

---

## File Locations
| File | Purpose |
|------|---------|
| `/opt/battlebuddy/audio_receiver.py` | Main brain script |
| `/opt/battlebuddy/calls.db` | SQLite — all transcribed calls |
| `/opt/battlebuddy/venv/` | Python virtualenv (Flask, Whisper, Torch) |
| `/etc/systemd/system/battlebuddy.service` | Systemd unit |
| `journalctl -u battlebuddy -f` | Live logs |

## Pi 1 Files (192.168.1.103)
| File | Purpose |
|------|---------|
| `/home/pi/op25_data/call_recorder.py` | Records + ships audio to this VM |
| `/home/pi/op25_data/collector.py` | Metadata collector + basic event detection |
| `/home/pi/op25_data/activity.db` | SQLite — all talkgroup activity |
| `/home/pi/op25/op25/gr-op25_repeater/apps/cfg.json` | OP25 config |

