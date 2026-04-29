# Contributing

Thanks for helping improve Battle Buddy. This project touches radio monitoring, incident data, credentials, and public-facing dashboards, so contributions should be careful, testable, and security-aware.

## Development Setup

```bash
git clone https://github.com/MkultraUSA/battle_buddy.git
cd battle_buddy
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
cp config.env.example config.env
```

Edit `config.env` locally. Never commit it.

## Branch Workflow

1. Create a topic branch.
2. Make small, focused changes.
3. Run linting and tests.
4. Open a pull request with a clear summary and test notes.

```bash
git checkout -b feature/my-change
python -m ruff check .
python -m pytest
```

## Code Style

- Prefer small functions with clear names.
- Keep production credentials in environment variables.
- Avoid adding new hardcoded deployment paths unless they are documented examples.
- Keep live infrastructure values in local config, not committed source.
- Preserve working deployment behavior when refactoring.

## Testing Expectations

Tests should be safe to run without:

- Live radio streams
- Real API keys
- Real Nextcloud credentials
- Production SQLite databases
- Internet access

Good test targets include config parsing, incident classification helpers, GeoJSON generation, talkgroup parsing, and log parsing.

## Security Expectations

Before opening a PR, check for accidental secrets:

```bash
git grep -n -i -E "api[_-]?key|secret|token|password|passwd|bearer|private[_-]?key|client[_-]?secret|anthropic|openai|groq|openrouter|broadcastify"
```

Run Gitleaks if available:

```bash
gitleaks detect --source . --verbose
```

Do not include real operational logs, audio captures, database files, private keys, service account files, or `.env`/`config.env` files.

## Pull Request Checklist

```text
[ ] The change is focused and documented
[ ] No secrets or private deployment data are included
[ ] README/INSTALL docs were updated if behavior changed
[ ] Tests were added or updated where practical
[ ] `python -m ruff check .` was run
[ ] `python -m pytest` was run
```
