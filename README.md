# ⚔ Battle Buddy — AI Situational Awareness System

Battle Buddy is an open-source platform for real-time P25 trunked radio monitoring with AI-powered incident detection, automatic transcription, and team alerting. Built for Austin/Travis County GATRRS but designed to work with any P25 trunked system.

![Battle Buddy](static/bgbattlebuddy.png)

**Status:** 🟢 Live — v2.1.0 (Pi 5 + OP25 + faster-whisper + Groq + Nextcloud stack)

---

## What It Does

- Decodes live P25 trunked radio traffic from GATRRS (Austin/TX) using an RTL-SDR dongle and OP25
- Captures audio per call and transcribes each call locally using **faster-whisper** (base.en INT8) — no API cost, works offline
- Sends transcripts to **Groq** (llama-3.3-70b) for incident classification — free tier, called directly from the server
- Automatically identifies unknown talkgroups by analyzing their radio chatter with Groq
- Posts alerts to Nextcloud Talk rooms sorted by agency beat (APD, fire/EMS, incidents, general)
- Displays active incidents on a live map with geocoded locations
- Detects multi-agency convergence, air asset deployment, APD surges, and DPS Capitol activations
- Escalation tracking: welfare → disturbance → pursuit → weapons → SWAT → K9 → air
- Nextcloud Deck integration — creates cards for high-priority incidents
- Watchdog auto-restarts OP25 via SSH if audio goes silent
- Talk bot with slash commands: `!sitrep`, `!incidents`, `!status`, `!unknowns`, `!addtag`

---

## Architecture

```
RTL-SDR Blog V4 (Pi 5)
  → OP25 / gr-op25_repeater (Pi 5)
      - Decodes P25 trunking, follows voice grants, outputs PCM via UDP
  → call_recorder.py (Pi 5)
      - Captures UDP audio per call, tracks TGID via systemd journal
      - Encodes WAV, POSTs to VM /receive
  → audio_receiver.py (VM :9001, Flask)
      - faster-whisper transcription (local, offline-capable)
      - Groq LLM incident analysis (direct HTTPS to api.groq.com)
      - Groq TGID auto-identification (unknown channels)
      - Nextcloud Talk / Deck / Banner alerts
      - SQLite call + incident storage
      - Serves map UI, sitrep, public splash page
```

> **Note on Groq relay:** A `groq_relay.py` proxy still runs on the Pi (port 9002) as a backup path. It was originally required because DigitalOcean datacenter IPs were blocked by Cloudflare. The VM is now hosted on Contabo, which is not blocked — Groq API calls go directly from the server. The Pi relay is retained as a fallback.

---

## Hardware

| Component | Notes |
|-----------|-------|
| Raspberry Pi 5 | Runs OP25, call_recorder, op25-collector — Debian Trixie |
| RTL-SDR Blog V4 | USB SDR dongle, tuned to GATRRS control channel 851.3875 MHz |
| Contabo VPS | Ubuntu 24.04, 8 cores, 24GB RAM — runs Flask brain + faster-whisper + Nextcloud |

> **Bluetooth not used.** Audio is captured directly from OP25's UDP output. Bluetooth disconnections caused system crashes and have been permanently removed from the pipeline.

---

## Software Stack

| Component | Purpose |
|-----------|---------|
| OP25 (boatbod/op25) | P25 trunk decoder — decodes GATRRS, outputs PCM audio via UDP |
| call_recorder.py | Pi-side: captures UDP audio per call, tracks TGID via journalctl |
| **faster-whisper** (base.en INT8) | Local speech-to-text on VM — 4–8× faster than openai-whisper, works offline |
| Groq llama-3.3-70b | Incident analysis and TGID identification — free tier, direct API |
| groq_relay.py | Pi 5 backup proxy for Groq (port 9002) — not required on Contabo |
| Flask (audio_receiver.py) | VM brain: receives calls, runs transcription + Groq, stores to SQLite |
| Nextcloud Talk | 5 chat rooms (incidents, apd, fire-ems, general, + catch-all) |
| Nextcloud Deck | Kanban board — auto-creates cards for high-priority incidents |
| SQLite | calls.db — calls, incidents, escalations, TGID guesses, subscriptions |
| nginx | Reverse proxy — HTTPS on kevcloud.ddns.net → Flask + Nextcloud |

---

## Pi Services

All Pi services are hardened with `Restart=always` and `StartLimitIntervalSec=0` (retries forever). User services require `loginctl enable-linger pi` (already set) to start without an interactive login.

| Service | Type | Description |
|---------|------|-------------|
| `op25-multi_rx.service` | system | OP25 P25 decoder — `ExecStartPre` kills any stale process holding port 8080 before start |
| `op25-collector.service` | user | Polls OP25 HTTP API, logs talkgroup activity to activity.db |
| `call_recorder.service` | user | Captures UDP audio, posts WAV calls to VM |
| `groq-relay.service` | user | Backup HTTP proxy for Groq API (port 9002) |
| `bb_command_poller.sh` | cron (1min) | Polls VM for restart/control commands |

**Startup ordering:** `op25-collector` and `call_recorder` both declare `After=op25-multi_rx.service` — they wait for the decoder to be running before starting.

---

## VM Services

| Service | Description |
|---------|-------------|
| `battlebuddy.service` | Flask app on port 9001 — the system brain |
| `nginx.service` | Reverse proxy, SSL termination |
| `snap.nextcloud.*` | Nextcloud (Apache, MySQL, PHP-FPM, Redis) |

**battlebuddy.service** is hardened with `Restart=always`, `StartLimitIntervalSec=0`, and `After=network-online.target` (waits for actual network connectivity, not just stack init).

---

## System Resilience

This system is designed for unattended and field operation. Every failure mode we've hit in production has been addressed:

| Failure | Fix |
|---------|-----|
| Pi reboot leaves stale process holding OP25 port 8080 | `ExecStartPre=-/bin/bash -c 'fuser -k 8080/tcp'` in op25-multi_rx.service |
| systemd stops retrying after 5 rapid failures | `StartLimitIntervalSec=0` on all services |
| User services don't start on Pi reboot | `loginctl enable-linger pi` (set permanently) |
| VM starts before network is up | `After=network-online.target` on battlebuddy.service |
| OP25 audio silence (soft bug) | 10-min watchdog auto-SSHes to Pi and restarts op25 + recorder |
| VM RAM exhaustion under load | Swapped from openai-whisper small → faster-whisper base.en INT8 (8GB → 1GB RAM) |
| No swap on production VM | 8GB swapfile added, `vm.swappiness=10` |

---

## Health Check

A full health check script is available on the VM:

```bash
bash /opt/battlebuddy/healthcheck.sh
```

Checks: system RAM/swap/disk/load, battlebuddy service + CPU/RAM, Pi intel vs broadcastify call freshness, **AI pipeline** (faster-whisper cached + Groq API live test), Pi service status + OP25 port, Nextcloud stack, nginx, SSL cert expiry.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/receive` | POST | Receive audio from Pi (WAV base64 + TGID metadata) |
| `/api/calls` | GET | Last 200 calls |
| `/api/incidents` | GET | All incidents |
| `/api/incidents/active` | GET | Active incidents (updated within 30m) |
| `/api/sitrep?minutes=60` | GET | AI situation report |
| `/api/tgid_guesses` | GET | Unknown TGID identification guesses |
| `/api/tgid_guesses/confirm` | POST | Confirm a TGID name, write to tags file |
| `/test_call` | POST | Inject a synthetic call (bypass transcription) |
| `/bot/talk` | POST | Nextcloud Talk bot webhook |
| `/pi/commands` | GET | Command queue for Pi to poll |

---

## Incident Types

`OFFICER DOWN` · `SHOOTING` · `STABBING` · `AIRCRAFT EMERGENCY` · `MASS CASUALTY` · `STRUCTURE FIRE` · `HAZMAT` · `HOSTAGE/BARRICADE` · `CRASH/COLLISION` · `FIRE DISPATCH` · `TRANSIT INCIDENT` · `AIRPORT EMERGENCY` · `MULTI-AGENCY RESPONSE` · `APD SURGE` · `AIR ASSET ACTIVE` · `DPS CAPITOL ACTIVATION`

---

## Talk Bot Commands

| Command | Description |
|---------|-------------|
| `!sitrep [minutes]` | AI situation report (default 60m) |
| `!incidents` | List active incidents |
| `!status` | System status — call volume, hold state, transcription engine |
| `!unknowns` | List unidentified talkgroups with Groq guesses |
| `!addtag <tgid> <name>` | Confirm a talkgroup name and write to tags file |
| `!subscribe [beat]` | Subscribe to 🔴 DM alerts (beats: all, apd, fire-ems, general) |
| `!unsubscribe [beat]` | Stop DM alerts |

---

## Unknown TGID Auto-Identification

When a call arrives on an unlabeled talkgroup, Battle Buddy sends the transcript to Groq with a specialized prompt asking which Austin/Travis County agency it belongs to. Guesses are stored with confidence levels (HIGH/MED/LOW). After 3 agreeing HIGH/MED guesses, the system auto-confirms and posts to the general Talk room. Users can confirm or override via `!addtag` or the REST API.

---

## Transcription

Battle Buddy uses **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** (base.en, INT8 quantized) for local speech-to-text. Key properties:

- **Offline-capable** — model is cached to disk, no internet required for transcription
- **4–8× faster** than openai-whisper at equivalent quality
- **~1GB RAM** vs ~8GB for openai-whisper small
- VAD filter enabled — skips silent segments automatically
- `cpu_threads=2` — limits CPU impact, leaves headroom for other services

The model is pre-downloaded to `~/.cache/huggingface/hub/` and loads in under 2 seconds at startup.

---

## Development Notes

- Pi scripts are maintained at `/home/pi/op25_data/` and synced with `scp` when updated.
- The VM is on Contabo (147.93.134.105). Groq API (`api.groq.com`) is directly reachable — no Pi relay required. If ever moved back to a blocked datacenter, point `GROQ_API_BASE` to the Pi relay at `http://192.168.1.158:9002`.
- `GROQ_API_KEY` and credentials are set inline in the Python config (not environment variables) — review before open-sourcing any sensitive deployment.
- Always run `bash /opt/battlebuddy/healthcheck.sh` before and after making changes.

---

## Talkgroup Coverage

GATRRS (Greater Austin-Travis County Trunked Radio System), System ID 2, licensed WPQY813. Control channel 851.3875 MHz. ~561 labeled talkgroups covering APD, AFD, TCEMS, TCFD, TCSO, UTPD, DPS, ABIA, Cap Metro, TXDOT, and surrounding counties.

---

## Cost Model

| Item | Cost |
|------|------|
| Contabo VPS (8 cores, 24GB RAM) | ~$14/month |
| Groq LLM inference | $0 (free tier) |
| faster-whisper transcription | $0 (runs locally) |
| Pi 5 + RTL-SDR | One-time ~$100 |
| **Total ongoing** | **~$14/month** |

---

## Key Files

| Path | What It Is |
|------|-----------|
| `/opt/battlebuddy/audio_receiver.py` | Main VM application |
| `/opt/battlebuddy/healthcheck.sh` | Full system health check |
| `/opt/battlebuddy/calls.db` | Call records + incident tracking (SQLite) |
| `/home/pi/op25_data/call_recorder.py` | Pi audio capture + TGID tracker |
| `/home/pi/op25_data/collector.py` | Pi talkgroup activity logger |
| `/home/pi/op25_data/groq_relay.py` | Pi Groq API backup proxy |
| `/home/pi/op25/op25/gr-op25_repeater/apps/cfg.json` | OP25 decoder config |
| `/home/pi/op25_data/gatrrs-tags.tsv` | TGID → agency name lookup table |
| `/home/pi/op25_data/activity.db` | Talkgroup activity log (SQLite) |

---

## License

MIT — open for community use and contribution.
