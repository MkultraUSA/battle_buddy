# Battle Buddy — Changelog

---

## [0.8.1] — 2026-03-12

### Added
- **Broadcastify Calls listener** (`calls_listener.py`) — polls the Calls API
  for individual pre-segmented call audio files instead of a continuous stream
  - Default playlist: Austin-Travis County Fire/EMS (`ecbfd29b-...`)
  - Works without authentication (public playlist, premium account recommended)
  - Falls back to Broadcastify login using credentials from `config.env` if API
    returns 403
  - Downloads each call as m4a, transcribes with faster-whisper (small model)
  - Writes to `radio_calls_YYYYMMDD.log` in identical format to the MP3 listener
    so `radio_parser.py` processes it unchanged
  - Sends `HEARD:` and `TALKGROUP:` messages to display pipe
  - Cursor-based deduplication — each call processed exactly once
- **`battle-buddy-calls.service`** — systemd service for the calls listener
- **`run_parser.sh`** — now processes `radio_calls_YYYYMMDD.log` alongside law log

---

## [0.8.0] — 2026-03-12

### Added
- **IPN poller** (`ipn_poller.py`) — polls the Broadcastify Incident Page Network
  proxy for Travis County (ctid=2749) and loads confirmed incidents into the DB
  - Endpoint: `broadcastify.com/scripts/ajax/ipnProxy.php?ctid=2749`
  - IPN incidents carry dispatcher-confirmed incident type, city, frequency, and
    timestamp (data is delayed up to 2 hours for the public)
  - Geocodes to city centroid via Nominatim (Austin Metro bounded)
  - Deduplicates via `ipn_id` — each IPN incident is only imported once
  - Inserted with `stream='ipn'` so heatmap and sitreps can distinguish IPN
    incidents from radio-parser incidents
  - Severity auto-classified from incident type keywords
  - Runs as part of the 30-minute parser pipeline (`run_parser.sh`)

### Changed
- `battle_buddy_db.py` — added `ipn_id TEXT UNIQUE` column to `incidents` table
  with automatic migration for existing databases
- `run_parser.sh` — now calls `ipn_poller.py` before regenerating the heatmap

---

## [0.7.8] — 2026-03-09

### Fixed
- **Two simultaneous voices** — root cause was two voice listener instances running at the
  same time: the systemd service plus a manually-started `--debug` session from earlier in
  the evening. Both responded to the wake word and both called aplay independently, producing
  overlapping audio. Killed the stale debug instance; single-instance operation confirmed.
- **Buffer contamination (definitive fix)** — replaced read-based drain strategies with
  `_flush_stream()`: calls `stream.abort()` then `stream.start()` to atomically clear the
  sounddevice ring buffer. Read-draining (both fixed-time and silence-threshold variants)
  failed because ambient room noise was always above `SILENCE_RMS`, causing drain loops to
  time out without actually flushing stale audio. `abort()`/`start()` resets the buffer
  completely regardless of ambient noise level.

---

## [0.7.7] — 2026-03-09

### Fixed
- **Buffer drain now waits for actual silence** — replaced fixed-time `_drain_stream(seconds)`
  with `_drain_until_quiet(max_sec=8.0)`. After mic unmute, audio chunks are read and discarded
  until 1.5s of silence is detected (RMS below threshold) or 8 seconds max. This correctly handles
  long sitrep audio still sitting in the PipeWire buffer regardless of how long the TTS was.
  Previously, a 2-second fixed drain was often not enough — the user's weather question was
  landing in the same `record_utterance()` capture window as leftover sitrep text, causing
  Whisper to transcribe a combined blob that sent garbage context to Claude.

---

## [0.7.6] — 2026-03-09

### Fixed
- **Stale audio buffer contamination in Ask Claude mode** — after Battle Buddy finishes speaking
  ("Ready. Go ahead." or any TTS response), `record_utterance()` was immediately reading stale
  audio still buffered in the PipeWire stream (residual sitrep text, room reverb). The fix passes
  the live `stream` handle into every `speak()` call in the ASK state, triggering `_drain_stream()`
  after unmuting. This discards buffered audio before the next question is recorded, preventing
  Whisper from transcribing TTS playback as a user utterance.
- All `speak()` call sites now pass `stream` consistently — wake response, COMMAND prompts,
  ASK mode responses, and "Leave Claude" exit — ensuring the drain runs after every TTS event.

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

## [0.7.3] — 2026-03-09

### Fixed
- **Sitrep mic feedback (root cause)** — `run_sitrep_blocking()` now mutes the mic before
  launching `battle_buddy_summary.py --speak` and unmutes with an 800ms settle afterward.
  Previously the mic stayed live during the entire sitrep reading, causing Whisper to
  transcribe the spoken sitrep and feed it back as a command or Claude question.
  (Confirmed in logs: sitrep text appearing verbatim as a transcribed "retry command".)
- **Web search tool loop** — removed the empty `tool_result content: ""` that was being sent
  back to the API. Anthropic's `web_search_20250305` is server-side: Anthropic executes the
  search and injects results automatically on the next API call. Sending empty tool_results
  caused Claude to answer from contaminated context rather than actual search results.

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
