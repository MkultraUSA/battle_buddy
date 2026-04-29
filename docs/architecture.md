# Architecture

Battle Buddy is organized as a field-oriented pipeline: radio capture happens close to the SDR hardware, while transcription, classification, storage, notifications, and web output happen on the server side.

## High-Level Flow

```text
Radio system / SDR
        |
        v
OP25 decoder
        |
        v
Call recorder / uploader
        |
        v
Battle Buddy Flask receiver
        |
        +--> audio deduplication
        +--> faster-whisper transcription
        +--> LLM incident analysis
        +--> talkgroup identification
        +--> SQLite persistence
        +--> Nextcloud Talk alerts
        +--> Nextcloud Deck cards
        +--> map/API/public dashboard output
```

## Capture Node

The capture node is intended to run near the SDR hardware. In the reference deployment this is a Raspberry Pi running OP25.

Responsibilities:

- Tune and decode the P25 system
- Capture per-call audio
- Associate audio with talkgroup metadata
- Upload call audio and metadata to the Battle Buddy receiver
- Poll for control commands, if enabled

## Server Node

The server node runs the main Flask application and database-backed incident logic.

Responsibilities:

- Receive uploaded call audio
- Transcribe audio locally
- Classify events and detect incidents
- Store calls, incidents, guesses, and subscriptions in SQLite
- Send Nextcloud Talk alerts
- Create Nextcloud Deck cards
- Serve API endpoints and public pages
- Export metrics for monitoring

## External Integrations

| Integration | Purpose | Secret Handling |
| --- | --- | --- |
| OpenRouter/Groq/Anthropic | LLM analysis and summarization | API keys from environment variables |
| Nextcloud Talk | Chat alerts and bot commands | App password from environment variables |
| Nextcloud Deck | Incident cards | App password from environment variables |
| Mailgun | Optional email alerts | API key from environment variables |
| Google APIs | Optional geocoding/search/maps | API keys from environment variables |
| FreeTAKServer | Optional CoT marker publishing | Token from environment variables |
| Stripe | Optional payment/subscription experiments | Keys from environment variables |

## Data Stores

- SQLite database for call and incident data
- Local static files for web assets
- Optional public dashboard/static map output
- Runtime logs via systemd/journal or configured log paths

Production databases, generated logs, and generated audio should not be committed to Git.

## Security Boundaries

- The repo should contain source code, safe examples, and docs only.
- `config.env` and real deployment files stay local.
- Public-facing dashboards should clearly label inferred, AI-classified, or unverified information.
- Sensitive live incident details should be handled carefully and not published blindly.
