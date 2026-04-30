# Public Release / Portfolio Checklist

Use this checklist before making the repository public, sharing it with a hiring manager, or cutting a release.

## Documentation

```text
[ ] README explains the project in under 60 seconds
[ ] README includes a screenshot or visual example
[ ] README explains what technologies and skills the project demonstrates
[ ] INSTALL.md allows a new technical user to run it
[ ] SECURITY.md explains secret handling and reporting
[ ] CONTRIBUTING.md explains the development workflow
[ ] Architecture documentation exists
[ ] License in README matches LICENSE file
```

## Security

```text
[ ] No real Broadcastify credentials
[ ] No OpenRouter/Groq/Anthropic/OpenAI/API keys
[ ] No Nextcloud app passwords
[ ] No `.env` or `config.env` committed
[ ] No private SSH keys, PEM files, or cert keys
[ ] No live radio logs containing sensitive personal information
[ ] No generated audio artifacts with sensitive content
[ ] No production database files
[ ] Gitleaks or equivalent secret scan passes
[ ] Git history has been checked for old secrets
```

## Reproducibility

```text
[ ] Python virtual environment instructions are documented
[ ] No `--break-system-packages` install path is recommended
[ ] requirements.txt is present
[ ] requirements-dev.txt is present
[ ] config.env.example has placeholders only
[ ] systemd service example uses environment file and venv path
[ ] Nginx/reverse proxy example is documented if applicable
```

## Code Quality

```text
[ ] Tests pass
[ ] Ruff/lint passes or known issues are documented
[ ] Tests do not require real credentials or live radio access
[ ] Generated logs/audio/databases are ignored
[ ] Large demo media files are not committed unless intentionally needed
```

## Hiring-Manager Readiness

```text
[ ] The repo makes the maintainer look security-aware
[ ] The repo clearly shows Linux/Python/system integration skills
[ ] The README does not overclaim accuracy or official status
[ ] Public-safety confidence levels are explained
[ ] Known limitations are documented honestly
[ ] Roadmap items are clearly marked as future work
```
