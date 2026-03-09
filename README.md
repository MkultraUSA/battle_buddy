# ⚔ Battle Buddy — AI Situational Awareness System

Battle Buddy is an open-source application designed to help the public stay informed about law enforcement activity and public safety events in their area. The goal of the project is to improve situational awareness by providing transparent, community-driven information about public safety activity. This project is intended for journalists, community members, and researchers interested in public safety transparency.
![Battle Buddy Screenshot](battlebuddyscreen.png)


## Status
🚧 Active development — v0.5.0

---

## What it does
- Streams live radio traffic from Broadcastify (law, fire, EMS feeds)
- Transcribes speech in real time using faster-whisper (runs fully local, no cloud)
- Tracks the active talkgroup (unit/channel) from ICY stream metadata
- Displays transcriptions and talkgroup on a dedicated heads-up display
- Logs all traffic to daily log files for post-processing
- Parses logs with Claude AI to extract and classify incidents
- Geocodes incidents and publishes a live public heatmap
- Generates spoken AI situational summaries (sitreps) via Claude + Piper TTS
- Serves sitrep audio on the public map, auto-refreshed every 4 hours

---

## Hardware

| Component | Notes |
|-----------|-------|
| Intel NUC (AMD CPU, 6 cores, 16GB RAM) | Tested on NUC running Ubuntu 24.04 LTS |
| Dedicated display | Heads-up situational awareness screen |
| Internet connection | Required for Broadcastify streams |
| RTL-SDR dongle + antenna | Future: direct SDR integration |

---

## Software Stack

| Component | Purpose |
|-----------|---------|
| faster-whisper | Local speech-to-text transcription (CPU, int8) |
| ffmpeg | Audio stream capture from Broadcastify |
| Python / tkinter | Heads-up display application |
| Claude (Anthropic) | Incident extraction, sitrep generation (Haiku + Sonnet) |
| Piper TTS | Local text-to-speech for spoken sitreps |
| SQLite | Incident and transcription database |
| nginx | Serves public heatmap and sitrep audio |

---

## Installation

### Prerequisites
- Ubuntu 24.04 LTS
- Python 3.12+
- A Broadcastify Premium account (for authenticated stream URLs)

### 1. Clone the repository
```bash
git clone https://github.com/MkultraUSA/battle_buddy.git
cd battle_buddy
```

### 2. Install Python dependencies
```bash
pip3 install -r requirements.txt --break-system-packages
```

### 3. Install system dependencies
```bash
sudo apt install python3-tk ffmpeg -y
```

### 4. Configure streams
```bash
cp config.env.example config.env
# Edit config.env with your credentials
```

Stream URLs are configured directly in `battle_buddy_listener_v1.2.py` in the `STREAMS` dict:
```python
STREAMS = {
    "law":  "https://USER:PASS@audio.broadcastify.com/14439.mp3",
    "fire": "https://USER:PASS@audio.broadcastify.com/28517.mp3",
    "ems":  "https://USER:PASS@audio.broadcastify.com/21284.mp3",
}
```

### 5. Install the systemd service
```bash
sudo cp battle-buddy-law.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable battle-buddy-law.service
sudo systemctl start battle-buddy-law.service
```

### 6. Set up display autostart
Copy the desktop entry to autostart:
```bash
cp battle-buddy-display.desktop ~/.config/autostart/
```

Or launch manually:
```bash
cd ~/battle_buddy && python3 battle_buddy_display.py
```

For fullscreen ops mode:
```bash
python3 battle_buddy_display.py --fullscreen
```

Demo mode (no audio required):
```bash
python3 battle_buddy_display.py --demo
```

---

## Display

| Color | Meaning |
|-------|---------|
| White bold | 📻 Heard / radio traffic |
| Green monospace | 🤖 Agent speech / responses |
| Gold italic | 📋 Intelligence summaries |
| Cyan (top-right) | 📡 Active talkgroup name (updates live) |

### Keyboard shortcuts
| Key | Action |
|-----|--------|
| F11 | Toggle fullscreen |
| ESC | Exit fullscreen |
| Q | Quit |

### Pipe interface
Any component can send messages to the display via the named pipe:
```bash
echo "HEARD: Alpha team, two vehicles eastbound Route 7" > /tmp/battle_buddy_display.pipe
echo "AGENT: Logging contact. Two vehicles Route 7 eastbound." > /tmp/battle_buddy_display.pipe
echo "SUMMARY: 2 dark vehicles eastbound Route 7, reported 14:32" > /tmp/battle_buddy_display.pipe
echo "STATUS: Listening for radio traffic..." > /tmp/battle_buddy_display.pipe
echo "TALKGROUP: TCSO ADAM-WEST" > /tmp/battle_buddy_display.pipe
echo "CLEAR" > /tmp/battle_buddy_display.pipe
```

---

## Log Format

Daily logs are written to `logs/radio_<stream>_<YYYYMMDD>.log`:
```
[2026-03-09 14:32:01] [TALKGROUP] TCSO ADAM-WEST
[2026-03-09 14:32:15] [HEARD] Unit 7 requesting backup on Route 7 | TALKGROUP: TCSO ADAM-WEST
[2026-03-09 14:33:01] [SYSTEM] Listener started. Model: small, Stream: law
```

---

## Incident Pipeline

```bash
# Parse today's log for incidents (auto-runs via cron every 30 min)
./run_parser.sh

# Or run manually
python3 radio_parser_v1.3.py --log logs/radio_law_20260309.log

# Generate heatmap
python3 make_heatmap.py

# Export incidents to GeoJSON
python3 incident_to_geojson_v1.1.py --log logs/incidents.log --out logs/incidents.geojson
```

Cron schedule:
```bash
# Parse logs and regenerate heatmap every 30 minutes
*/30 * * * * nice -n 15 flock -n /tmp/battle_buddy_parser.lock /home/pi/battle_buddy/run_parser.sh

# Generate 4h sitrep audio every 4 hours (skips if Ollama is busy)
15 */4 * * * nice -n 19 flock -n /tmp/battle_buddy_sitrep.lock /home/pi/battle_buddy/run_sitrep.sh
```

---

## Sitrep

Battle Buddy generates a spoken situational summary using Claude Sonnet + Piper TTS.

```bash
python3 battle_buddy_summary.py              # last 4h, display + speak
python3 battle_buddy_summary.py --hours 8    # 8h window
python3 battle_buddy_summary.py --hours 24   # full day
python3 battle_buddy_summary.py --no-display # terminal only
```

The sitrep audio is saved to `logs/map/sitrep.wav` and served on the public map page.
Desktop launcher icons are included for on-demand sitreps at 4h, 8h, 12h, and 24h windows.

---

## Project Structure

```
battle_buddy/
├── README.md                        # This file
├── requirements.txt                 # Python dependencies
├── config.env.example               # Config template (copy to config.env)
├── config.env                       # Local credentials — NOT committed
├── battle_buddy_display.py          # Heads-up display application
├── battle_buddy_listener_v1.2.py    # Broadcastify stream listener
├── battle_buddy_summary.py          # AI sitrep generator (Claude + Piper TTS)
├── battle_buddy_db.py               # Incident database
├── battle-buddy-law.service         # systemd service — Travis County Law
├── run_parser.sh                    # Cron wrapper: parse logs + regenerate map
├── run_sitrep.sh                    # Cron wrapper: generate 4h sitrep audio
├── radio_parser_v1.3.py             # Log parser / incident extractor (Claude Haiku)
├── make_heatmap.py                  # Public heatmap generator
├── incident_to_geojson_v1.1.py      # Incident → GeoJSON converter
├── incident_watcher_v1.1.py         # Incident log watcher
└── logs/                            # Daily radio logs, DB, map — NOT committed
    └── map/
        ├── index.html               # Public heatmap (served by nginx)
        ├── sitrep.wav               # Latest spoken sitrep audio
        └── sitrep.txt               # Latest sitrep text + timestamp
```

---

## Roadmap

- [x] Heads-up display with HEARD / AGENT / SUMMARY / STATUS message types
- [x] Named pipe message interface
- [x] Windowed and fullscreen modes
- [x] Broadcastify stream listener (ffmpeg + faster-whisper)
- [x] ICY talkgroup metadata — live talkgroup display
- [x] Daily log files with talkgroup context
- [x] Incident parser and GeoJSON export
- [x] Heatmap generator
- [x] systemd service (auto-start, auto-restart)
- [x] Claude AI incident extraction and sitrep generation
- [x] Piper TTS spoken sitrep output
- [x] Public heatmap with incident markers (served via nginx)
- [x] Sitrep audio on public map, auto-refreshed every 4 hours
- [x] Desktop launcher icons for on-demand sitreps
- [ ] Wake word trigger for hands-free sitrep
- [ ] Fire / EMS systemd services
- [ ] RTL-SDR direct SDR integration
- [ ] Web dashboard (browser-based live view)
- [ ] Home Assistant integration
- [ ] Nextcloud Talk remote command interface

---

## Security Notes

- `config.env` contains credentials — never commit this file (already in .gitignore)
- Stream URLs contain Broadcastify credentials — do not share or log publicly
- This system has broad audio and network access — treat it as privileged infrastructure

---

## License
MIT

## Author
kevcloud

## Contributing
This is an early-stage project. Issues and pull requests welcome.
