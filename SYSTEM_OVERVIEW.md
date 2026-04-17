# Battle Buddy — System Overview

## What It Is

Battle Buddy is a real-time AI-powered emergency scanner for Austin, TX. It listens to the Greater Austin Trunked Radio System (GATRRS), transcribes radio calls using Whisper, analyzes them with an LLM (Groq), detects incidents, and alerts a team via Nextcloud Talk — all for ~$6/month with no per-call AI cost.

---

## Infrastructure

| Component | Location | Role |
|-----------|----------|------|
| Raspberry Pi 5 | Home LAN (192.168.1.158) | SDR receiver, OP25 decoder, Groq relay |
| DigitalOcean VM | 147.93.134.105 | Flask app, Whisper, incident tracking, Nextcloud |
| RTL-SDR Blog V4 | Attached to Pi | Receives 851 MHz P25 RF signal |
| Bluetooth speaker | Near Pi | Audio monitor (J-170, MAC 0E:DD:F1:51:4A:01) |

**Public URL:** kevcloud.ddns.net (No-IP DDNS → nginx → VM)

---

## Radio Source

- **System:** GATRRS (Greater Austin Trunked Radio System)
- **Control channel:** 851.3875 MHz (WPQY813)
- **Protocol:** P25 Phase 1, CQPSK modulation
- **Coverage:** APD, AFD, TCFD, TCSO, EMS, and dozens of other Austin-area agencies

---

## Data Flow (End to End)

```
RF signal (851 MHz)
  → RTL-SDR Blog V4 (Pi 5)
  → OP25 / gr-op25_repeater (Pi 5)
      - Decodes P25 trunking control channel
      - Follows voice grants to voice frequencies
      - Outputs audio via PipeWire → Bluetooth monitor source
  → call_recorder.py (Pi 5)
      - Captures audio from PipeWire monitor via parec
      - Tracks active TGID by tailing op25-multi_rx journal logs
      - Encodes WAV, POSTs to VM /receive endpoint
  → audio_receiver.py (VM)
      - Whisper transcribes audio (model: small, runs on CPU)
      - Tags call with TGID, agency name (from gatrrs-tags.tsv)
      - Groq LLM analyzes transcript for incident type, severity, summary
      - Incident tracker maintains active incident list (15-min window)
      - Posts to Nextcloud Talk rooms
      - Updates Nextcloud Deck board (high-priority incidents)
      - Serves public splash page at /splash
```

---

## Pi 5 Services

| Service | Type | What It Does |
|---------|------|-------------|
| `op25-multi_rx` | system | OP25 P25 decoder — the core SDR process |
| `call_recorder` | user | Captures audio, tracks TGID, posts WAV to VM |
| `op25-collector` | user | Polls OP25 HTTP API every 5s, logs all active talkgroups to activity.db |
| `groq-relay` | user | HTTP proxy — forwards Groq API calls from VM through Pi's residential IP |
| `bb_command_poller` | cron (1 min) | Polls VM for commands (future: remote control of OP25) |

---

## VM Services

| Service | What It Does |
|---------|-------------|
| `battlebuddy` | Main Flask app (audio_receiver.py) — Whisper, Groq, incident tracking, Talk bot, API |
| `nginx` | Reverse proxy — routes HTTPS traffic to Flask (port 9001) and Nextcloud (PHP-FPM) |
| `nextcloud` | Team collaboration — Talk rooms, Deck board, file storage |

---

## Talk Rooms

| Room | Purpose |
|------|---------|
| `general` | All calls, TGID auto-ID confirmations |
| `apd` | APD-tagged calls only |
| `fire-ems` | AFD / EMS / TCFD calls |
| `incidents` | High-priority incident alerts only |
| `battlebuddy-bot` | Bot commands and system messages |

---

## Bot Commands (in Talk)

| Command | What It Does |
|---------|-------------|
| `!sitrep` | Summary of active incidents |
| `!incidents` | List all tracked incidents |
| `!unknowns` | List TGIDs pending identification |
| `!addtag <tgid> <name>` | Confirm a TGID name, writes to tags TSV |

---

## Key Features

### Groq Relay (Cloudflare Bypass)
Groq's free API blocks DigitalOcean IP ranges via Cloudflare. The Pi runs a lightweight HTTP proxy (`groq_relay.py`) on port 9002. The VM sends Groq requests to the Pi, which forwards them from its residential IP where Cloudflare doesn't block them.

### TGID Auto-Identification
When a call arrives on an unknown talkgroup ("TGID 12345"), a background thread sends the transcript and context to Groq asking it to identify the likely agency. After 3 agreeing high/medium confidence guesses, the system auto-confirms and posts to general. Users can confirm manually with `!addtag`.

### TGID Tracking (The Clever Part)
OP25's HTTP API is polled every 5 seconds by the collector, but the active talkgroup changes faster than that. `call_recorder.py` instead tails the `op25-multi_rx` systemd journal in real time, parsing `voice update: tg(XXXX)` lines with a regex to know the exact TGID before the first audio byte arrives.

### Watchdog / Auto-Restart
`audio_receiver.py` tracks when the last audio was received. After 5 minutes of silence it sends an alert; after 10 minutes it SSHes to the Pi and restarts `op25-multi_rx` and `call_recorder` automatically. This handles the recurring OP25 audio silence bug where trunking decodes fine but audio routing breaks.

### Talkgroup Activity Log (collector)
`op25-collector` independently logs all active talkgroups to `activity.db` on the Pi every 5 seconds, regardless of whether audio was captured. This provides a complete record of radio activity even during audio failures, and catches talkgroups that never produce audible audio.

---

## Cost Model

| Item | Cost |
|------|------|
| DigitalOcean droplet | ~$6/month |
| Groq LLM inference | $0 (free tier, routed via Pi) |
| Whisper transcription | $0 (runs locally on VM CPU) |
| Pi 5 + RTL-SDR | One-time ~$100 |
| **Total ongoing** | **~$6/month** |

---

## Key Files

| Path | What It Is |
|------|-----------|
| `/opt/battlebuddy/audio_receiver.py` | Main VM application |
| `/home/pi/op25_data/call_recorder.py` | Pi audio capture + TGID tracker |
| `/home/pi/op25_data/collector.py` | Pi talkgroup activity logger |
| `/home/pi/op25_data/groq_relay.py` | Pi Groq API proxy |
| `/home/pi/op25/op25/gr-op25_repeater/apps/cfg.json` | OP25 decoder config |
| `/home/pi/op25_data/gatrrs-tags.tsv` | TGID → agency name lookup table |
| `/home/pi/op25_data/activity.db` | Talkgroup activity log (SQLite) |
| `/opt/battlebuddy/calls.db` | Call records + incident tracking (SQLite) |
