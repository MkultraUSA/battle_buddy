# ⚔ Battle Buddy — AI Situational Awareness System

An open-source AI agent that listens to room audio and radio traffic, transcribes it in real time, builds a situational picture, and delivers verbal intelligence reports. Designed to augment soldiers in the field by acting as a persistent sensor and analyst.

## Status
🚧 Active development — v0.2.0

## Overview

Battle Buddy runs on a local device (tested on Intel NUC / Ubuntu 24.04) and requires no cloud connectivity for core functions. It is designed to work in low-connectivity environments.

### What it does
- Listens continuously to room audio and/or radio traffic (GMRS, etc.)
- Transcribes speech in real time using OpenAI Whisper (runs locally)
- Displays transcriptions on a dedicated screen in real time
- Builds a running situational picture of enemy contact reports
- Delivers periodic verbal intelligence summaries via text-to-speech
- Integrates with OpenClaw + Claude for reasoning and report generation

### Display
- 📻 **White bold text** — heard audio / radio traffic
- 🤖 **Green monospace text** — agent speech / responses  
- 📋 **Gold italic text** — intelligence summaries
- Maximized windowed by default, F11 for fullscreen ops mode

---

## Hardware

| Component | Notes |
|-----------|-------|
| Intel NUC (or similar) | Tested on NUC running Ubuntu 24.04 LTS |
| Blue USB Microphone | Room audio capture |
| GMRS Radio (hand talkie) | Placed near mic for radio traffic capture |
| RTL-SDR dongle + antenna | Future: direct SDR integration |
| Dedicated display | Heads-up situational awareness screen |

---

## Software Stack

| Component | Purpose |
|-----------|---------|
| OpenClaw | AI agent framework / orchestration |
| Claude Sonnet (Anthropic) | Reasoning, summarization, report generation |
| OpenAI Whisper | Local speech-to-text transcription |
| Piper TTS | Local text-to-speech (agent voice) — coming soon |
| PipeWire | Audio routing |
| Python / tkinter | Display application |

---

## Installation

### Prerequisites
- Ubuntu 24.04 LTS
- Python 3.12+
- Node.js 22+
- An Anthropic API key

### 1. Clone the repository
```bash
git clone https://github.com/YOUR_USERNAME/battle-buddy.git
cd battle-buddy
```

### 2. Install Python dependencies
```bash
pip3 install -r requirements.txt --break-system-packages
```

### 3. Install system dependencies
```bash
sudo apt install python3-tk ffmpeg -y
```

### 4. Install OpenClaw
```bash
mkdir -p ~/.npm-global
npm config set prefix '~/.npm-global'
echo 'export PATH=~/.npm-global/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
npm install -g openclaw
openclaw onboard
```

### 5. Run the display
```bash
chmod +x battle_buddy_display.py
./battle_buddy_display.py
```

For fullscreen ops mode:
```bash
./battle_buddy_display.py --fullscreen
```

Demo mode (no audio required):
```bash
./battle_buddy_display.py --demo
```

---

## Usage

### Sending messages to the display
Any component can send messages to the display via the named pipe:

```bash
echo "HEARD: Alpha team, two vehicles eastbound Route 7" > /tmp/battle_buddy_display.pipe
echo "AGENT: Logging contact. Two vehicles Route 7 eastbound." > /tmp/battle_buddy_display.pipe
echo "SUMMARY: 2 dark vehicles eastbound Route 7, reported 14:32" > /tmp/battle_buddy_display.pipe
echo "STATUS: Listening for radio traffic..." > /tmp/battle_buddy_display.pipe
echo "CLEAR" > /tmp/battle_buddy_display.pipe
```

### Keyboard shortcuts
| Key | Action |
|-----|--------|
| F11 | Toggle fullscreen |
| ESC | Exit fullscreen |
| Q | Quit |

---

## Roadmap

- [x] Display app with heard/agent/summary text types
- [x] Named pipe message interface
- [x] Windowed and fullscreen modes
- [x] OpenClaw installed and configured
- [ ] Whisper audio listener pipeline
- [ ] Piper TTS agent voice output
- [ ] GMRS radio → Whisper integration
- [ ] RTL-SDR direct integration
- [ ] OpenClaw summarization skill wired in
- [ ] Contact report database (enemy positions, vehicles, equipment)
- [ ] Home Assistant integration
- [ ] Nextcloud Talk remote command interface

---

## Project Structure

```
battle-buddy/
├── README.md                    # This file
├── requirements.txt             # Python dependencies
├── battle_buddy_display.py      # Heads-up display application (v0.2.0)
├── battle_buddy_listener.py     # Whisper audio pipeline (coming soon)
└── battle_buddy_agent.py        # OpenClaw/Claude integration (coming soon)
```

---

## Security Notes

- API keys are stored in `~/.openclaw/openclaw.json` — never commit this file
- OpenClaw gateway is bound to loopback only by default — do not expose to public internet
- Vet any OpenClaw community skills carefully before installing
- This system has broad audio and system access — treat it as privileged infrastructure

---

## License
MIT

## Author
kevcloud

## Contributing
This is an early-stage project. Issues and pull requests welcome.
