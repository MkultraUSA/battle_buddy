# Battle Buddy

Battle Buddy is a local-first civic awareness system for monitoring public-safety radio activity, transcribing audio, detecting incidents, and publishing map-ready situational data.

The project demonstrates practical Linux service deployment, Python audio processing, local AI transcription, LLM-assisted incident classification, SQLite-backed event storage, GeoJSON/map output, Nextcloud integration, and operational monitoring.

![Battle Buddy dashboard](static/bgbattlebuddy.png)

**Status:** Active development — v2.1.0

> Safety note: this repository is designed to use local environment variables for credentials. Do not commit live API keys, stream credentials, databases, logs, audio recordings, certificates, or private deployment files.

---

## What This Project Demonstrates

- Python service development for long-running Linux systems
- systemd-based deployment and watchdog-style resilience
- OP25 / P25 radio monitoring pipeline integration
- Local speech-to-text with `faster-whisper`
- LLM-assisted incident classification and talkgroup identification
- SQLite storage for calls, incidents, subscriptions, and metadata
- GeoJSON/map-ready incident publishing
- Nextcloud Talk and Deck integrations
- Nginx/static web publishing patterns
- Operational logging, health checks, and troubleshooting workflows
- Security-aware configuration using environment variables

---

## What It Does

- Decodes live P25 trunked radio traffic from a supported public-safety radio system using OP25 and an RTL-SDR
- Captures audio per call and transcribes each call locally using `faster-whisper`
- Classifies incident type and severity using an LLM provider configured by environment variables
- Stores call and incident records in SQLite
- Publishes active incidents to API endpoints and map-ready data
- Posts alerts to Nextcloud Talk rooms sorted by beat/agency category
- Creates Nextcloud Deck cards for high-priority incidents
- Tracks escalation patterns such as multi-agency convergence, air assets, and high-priority call types
- Supports slash-style Talk bot commands such as `!sitrep`, `!incidents`, `!status`, `!unknowns`, `!addtag`, and `!query`

---

## Architecture

```text
RTL-SDR / P25 radio source
        |
        v
OP25 decoder on Raspberry Pi
        |
        v
Call recorder / uploader
        |
        v
Battle Buddy Flask service
        |
        +--> faster-whisper transcription
        +--> LLM incident classification
        +--> unknown talkgroup identification
        +--> SQLite call and incident storage
        +--> Nextcloud Talk / Deck alerts
        +--> GeoJSON / map / public API output
        |
        v
Nginx / Nextcloud / dashboard consumers
```

More detail is available in [`docs/architecture.md`](docs/architecture.md).

---

## Hardware Used in the Reference Deployment

| Component | Purpose |
| --- | --- |
| Raspberry Pi 5 | Runs OP25 and call capture/uploader components |
| RTL-SDR Blog V4 or compatible SDR | Receives the public-safety radio system |
| Linux VPS or local server | Runs the Flask application, transcription, database, and web/API services |
| Nextcloud server | Receives Talk alerts, Deck cards, and shared reports |

This is a reference deployment, not a hard requirement. Paths, URLs, hosts, and credentials should be configured locally through environment variables.

---

## Software Stack

| Component | Purpose |
| --- | --- |
| Python 3.10+ | Application runtime |
| Flask | HTTP/API service |
| OP25 | P25 trunked radio decoding |
| faster-whisper | Local speech-to-text |
| SQLite | Lightweight local storage |
| Nextcloud Talk | Team notifications and bot commands |
| Nextcloud Deck | Incident card workflow |
| Nginx | Reverse proxy / HTTPS termination |
| systemd | Service supervision |

---

## Quick Start for Development

```bash
git clone https://github.com/MkultraUSA/battle_buddy.git
cd battle_buddy
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config.env.example config.env
```

Edit `config.env` locally. Do not commit it.

```bash
set -a
source config.env
set +a
python audio_receiver.py --port 9001 --model base
```

For production/service setup, see [`INSTALL.md`](INSTALL.md).

---

## Configuration

Battle Buddy should be configured with environment variables, not hardcoded secrets. Use [`config.env.example`](config.env.example) as the safe template.

Important variables include:

```bash
BATTLE_BUDDY_HOME=/opt/battlebuddy
BATTLE_BUDDY_LOG_DIR=/var/log/battlebuddy
BATTLE_BUDDY_DATA_DIR=/var/lib/battlebuddy
BATTLE_BUDDY_WEB_DIR=/var/www/battlebuddy

OPENROUTER_API_KEY=your_openrouter_key_here
GROQ_API_KEY=your_groq_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here
TALK_PASS=your_nextcloud_app_password_here
NC_USER=your_nextcloud_user_here
NC_PASS=your_nextcloud_app_password_here
```

Do not place real credentials directly in URLs or committed files.

---

## API Endpoints

| Endpoint | Method | Description |
| --- | --- | --- |
| `/receive` | POST | Receive audio from the capture node |
| `/api/calls` | GET | Recent calls |
| `/api/incidents` | GET | Incident list |
| `/api/incidents/active` | GET | Active incidents |
| `/api/sitrep?minutes=60` | GET | Situation report |
| `/api/tgid_guesses` | GET | Unknown talkgroup identification guesses |
| `/api/tgid_guesses/confirm` | POST | Confirm a talkgroup name |
| `/test_call` | POST | Inject a synthetic test call |
| `/bot/talk` | POST | Nextcloud Talk bot webhook |
| `/pi/commands` | GET | Command queue for capture node polling |
| `/metrics` | GET | Prometheus-compatible metrics, when enabled |

---

## Talk Bot Commands

| Command | Description |
| --- | --- |
| `!sitrep [minutes]` | Situation report, default 60 minutes |
| `!incidents` | List active incidents |
| `!status` | System status and call volume |
| `!query <question>` | Natural-language query against recent incidents/transcripts |
| `!unknowns` | List unidentified talkgroups with AI guesses |
| `!addtag <tgid> <name>` | Confirm a talkgroup name |
| `!subscribe [beat]` | Subscribe to DM alerts |
| `!unsubscribe [beat]` | Stop DM alerts |
| `!help` | Command help |

---

## Security and Privacy

Before making this repository public or cutting a release, run a secret scan and inspect Git history.

Recommended checks:

```bash
git grep -n -i -E "api[_-]?key|secret|token|password|passwd|bearer|private[_-]?key|client[_-]?secret|anthropic|openai|groq|openrouter|broadcastify"
git log --all -p | grep -i -E "api[_-]?key|secret|token|password|passwd|bearer|private[_-]?key|client[_-]?secret|anthropic|openai|groq|openrouter|broadcastify" | head -200
gitleaks detect --source . --verbose
```

See [`SECURITY.md`](SECURITY.md) for the full checklist.

---

## Development Commands

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest
```

The test suite should avoid live radio streams, real API keys, real Nextcloud credentials, or production databases.

---

## Project Structure

```text
battle_buddy/
  audio_receiver.py
  modules/
  docs/
  tests/
  static/
  user_recordings/
  README.md
  INSTALL.md
  SECURITY.md
  CONTRIBUTING.md
  config.env.example
  requirements.txt
  requirements-dev.txt
  pyproject.toml
```

Long term, the application can be migrated toward a `src/battle_buddy/` package layout, but the current priority is preserving working deployment behavior while improving security, documentation, and testability.

---

## Release Checklist

See [`docs/release-checklist.md`](docs/release-checklist.md).

---

## License

MIT License. See [`LICENSE`](LICENSE).
