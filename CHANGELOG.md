# Battle Buddy — Changelog

---

## [0.6.0] — 2026-03-09

### Added
- **Wake word listener** (`battle_buddy_voice.py`) — always-on hands-free trigger
  - Wake phrase: "Hey Battle Buddy" detected via faster-whisper tiny model (local, no cloud)
  - Two-tone chime acknowledgment (`chime.wav`) plays on detection
  - Automatically generates and speaks the 4-hour sitrep on trigger
  - Sliding 3-second audio window re-evaluated every 0.5 seconds for responsiveness
  - Silence gate (RMS threshold) skips quiet chunks to save CPU
  - 30-second cooldown prevents re-triggering while sitrep is active
  - Blue Microphone input volume set to 85% on service start
- **Voice systemd user service** (`~/.config/systemd/user/battle-buddy-voice.service`)
  - Runs under user session for PipeWire audio access
  - Auto-starts on login, auto-restarts on failure
  - Sets mic volume via `ExecStartPre`
- `sounddevice` added to Python dependencies (mic capture via PipeWire)

### Changed
- README updated to v0.6.0; wake word marked complete in roadmap
- Project structure updated with voice listener files

---

## [0.5.0] — 2026-03-01 (approx.)

### Features at this version
- Broadcastify stream listener (`battle_buddy_listener.py` / `battle_buddy_listener_v1.2.py`)
  - Captures MP3 stream in 15-second chunks via ffmpeg
  - Transcribes with faster-whisper (small model, CPU, int8)
  - ICY metadata polling every 30 seconds for live talkgroup name
  - Silence filtering at -40 dB before transcription
- Heads-up display (`battle_buddy_display.py`) via tkinter
  - Named pipe interface (`/tmp/battle_buddy_display.pipe`)
  - Message types: HEARD (white bold), AGENT (green mono), SUMMARY (gold italic),
    STATUS (grey bar), TALKGROUP (cyan indicator), CLEAR
  - Windowed and fullscreen modes (F11 toggle)
  - Demo mode (`--demo`) for testing without audio
- Incident pipeline
  - `radio_parser.py` (v1.4) — Claude Haiku extracts and classifies 37 incident types
  - `battle_buddy_db.py` — SQLite incident and transcription database
  - `incident_to_geojson.py` — exports incidents to GeoJSON
  - `make_heatmap.py` — generates public Leaflet heatmap
  - Cron: parser every 30 min, sitrep every 4 hours
- Sitrep generator (`battle_buddy_summary.py`)
  - Claude Sonnet reads incidents + transcriptions from DB
  - Piper TTS spoken output (`--speak`)
  - Sitrep audio saved to `logs/map/sitrep.wav` and served on public map
- Systemd service: `battle-buddy-law.service` (Travis County Law stream)
- Display autostart via `~/.config/autostart/`
- Public map at `https://kevcloud.ddns.net/map` served by nginx
- Streams configured: `law` (14439), `fire` (28517), `ems` (21284)

---

## [0.7.2] — 2026-03-09

### Fixed
- **Mic feedback loop** — microphone is now muted via `pactl` for the full duration of every
  Piper TTS playback (`speak()`) and unmuted with a 400ms settle afterward. Eliminates the
  case where Piper's output was picked up by the mic, re-transcribed by Whisper, and
  incorrectly triggered commands mid-sitrep or mid-response.
- **Sitrep command tightened** — changed trigger from `"sitrep"` (single word, easily matched
  by ambient transcription) to `"Give Sitrep"` / `"Give me sitrep"` / `"Give me a sitrep"`.
  The two-word phrase is far less likely to appear in random radio traffic or TTS playback.

---

## [0.7.1] — 2026-03-09

### Added
- **Web search in Ask Claude mode** — Claude can now search the internet to answer questions
  about current events, weather, news, research topics, or anything else
- `strip_citations()` — removes citation markers (`[1]`, `[[1]](url)`) and bare URLs from
  Claude's response before it is spoken, so Piper reads cleanly
- Tool-use loop in `ask_claude()` — handles Anthropic's `web_search_20250305` built-in tool;
  Claude decides autonomously when a web search is needed
- Status bar shows **"Searching and thinking…"** during web-search turns

### Changed
- System prompt updated to inform Claude it has web search capability and to write
  responses suitable for speech (no URLs, no citation numbers)
- `ask_claude()` now passes `tools=[{"type": "web_search_20250305"}]` on every call
- `max_tokens` raised from 400 → 512 to accommodate richer research answers

---

## [0.7.0] — 2026-03-09

### Added
- **Voice command state machine** — full hands-free command interface
  - After "Hey Battle Buddy" → Battle Buddy speaks **"Yes sir"** and enters COMMAND mode
  - Command **"Sitrep"** — display freezes, scrolls clear, 4h sitrep is generated and spoken, display resumes automatically
  - Command **"Ask Claude"** — enters continuous Q&A mode backed by Claude Sonnet with live radio context (last 4h of transcriptions + incidents)
  - Command **"Leave Claude"** — exits Q&A mode, resumes live radio feed on display
  - Unrecognised command — Battle Buddy prompts once and gives a second chance before returning to standby
- **Display FREEZE / UNFREEZE** pipe messages — suppresses incoming HEARD/TALKGROUP updates during voice mode; status bar shows "VOICE MODE — Live feed paused"
- **Continuous Q&A loop** — in Ask Claude mode, each utterance (detected by 2-second silence after speech) is sent to Claude; conversation continues until "Leave Claude"
- **Radio context injection** — Claude answers use the last 4 hours of DB transcriptions and incidents as grounding context
- `record_utterance()` — adaptive speech-end detection: records until 2 seconds of silence or 20-second max

### Changed
- `battle_buddy_voice.py` fully rewritten as a state machine (LISTENING → COMMAND → ASK)
- `battle_buddy_display.py` — added FREEZE/UNFREEZE message handling
- [ ] Fire / EMS systemd services
- [ ] RTL-SDR direct SDR integration
- [ ] Web dashboard (browser-based live view)
- [ ] Home Assistant integration
- [ ] Nextcloud Talk remote command interface
