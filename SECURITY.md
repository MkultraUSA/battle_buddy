# Security Policy

Battle Buddy integrates radio monitoring, web services, LLM providers, Nextcloud, optional TAK/FreeTAKServer, and public dashboard output. Treat this repository as public-facing and assume every committed file may eventually be visible to employers, contributors, or customers.

## Supported Versions

This project is in active development. Security fixes should target the current `main` branch unless a release branch is created later.

## Reporting Vulnerabilities

If you find a security issue, do not open a public issue containing secrets, tokens, hostnames, private deployment details, or exploit steps. Contact the maintainer privately first.

## Secret-Handling Rules

Never commit:

- API keys for OpenRouter, Groq, Anthropic, OpenAI, Google, Mailgun, Stripe, or similar services
- Broadcastify or stream credentials
- Nextcloud usernames/passwords or app passwords
- TAK/FreeTAKServer tokens
- SSH private keys
- PEM/key/certificate private material
- `.env`, `config.env`, or other local environment files
- SQLite databases from production
- Live radio logs containing sensitive personal information
- Generated audio recordings from production
- Database dumps or backups

Use `config.env.example` for placeholders only. Put real values in `config.env` locally or in your deployment system's environment manager.

## Public Safety and Privacy

Battle Buddy may process public-safety radio traffic and incident metadata. Before publishing demo data or screenshots:

- Remove personally identifying information when possible
- Avoid publishing live tactical details that could interfere with public safety operations
- Label confidence levels clearly
- Distinguish confirmed data from inferred or AI-classified data
- Do not imply official agency affiliation unless one exists

## Pre-Release Security Checklist

Before making the repo public or cutting a release:

```text
[ ] No real Broadcastify credentials
[ ] No OpenRouter/Groq/Anthropic/OpenAI/API keys
[ ] No `.env` or `config.env` committed
[ ] No private SSH keys, PEM files, or cert keys
[ ] No live radio logs containing sensitive personal information
[ ] No generated audio artifacts with sensitive content
[ ] No database files containing production data
[ ] Gitleaks or equivalent secret scan passes
[ ] README examples use placeholders only
[ ] Git history has been inspected for old secrets
```

## Recommended Checks

Current working tree:

```bash
git grep -n -i -E "api[_-]?key|secret|token|password|passwd|bearer|private[_-]?key|client[_-]?secret|anthropic|openai|groq|openrouter|broadcastify"
```

Git history:

```bash
git log --all -p | grep -i -E "api[_-]?key|secret|token|password|passwd|bearer|private[_-]?key|client[_-]?secret|anthropic|openai|groq|openrouter|broadcastify" | head -200
```

Gitleaks:

```bash
gitleaks detect --source . --verbose
```

Suspicious files:

```bash
find . -type f \( -name "*.env" -o -name "*config*" -o -name "*.key" -o -name "*.pem" -o -name "credentials.json" -o -name "token.json" -o -name "*.db" -o -name "*.sqlite" -o -name "*.log" -o -name "*.wav" \) -print
```

## If a Secret Was Committed

Deleting the secret from the latest file is not enough. Assume the value is compromised and:

1. Revoke or rotate the secret immediately.
2. Search all branches and tags for the secret.
3. Rewrite Git history only after understanding the impact on collaborators.
4. Force-push only when you intentionally accept the consequences.
5. Ask GitHub support to clear cached views if needed.

For a private repo that will become public, history cleanup matters as much as the current file tree.
