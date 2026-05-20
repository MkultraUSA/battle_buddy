# Hermes Bot Configuration - Battle Buddy

## Goal
- All code changes go through PRs.
- Deployment state must exactly match GitHub (`main`) with no server-only edits.
- Hermes bots do primary implementation and primary review.
- Codex performs a second-pass review only when requested by Kevin.

## Bot Roles

### `bb-codebot` (primary implementer)
- Trigger: GitHub issue labeled `codebot` or explicit manual dispatch.
- Work only on `feature/*` branches.
- Never commit to `main`.
- Open PR to `main` with:
  - objective
  - changed files
  - risk notes
  - test evidence
- Must run before PR open:
  - `pytest tests/ -q`
  - `ruff check .`
  - `python -m py_compile audio_receiver.py`

### `bb-reviewbot` (primary reviewer)
- Trigger: PR `opened`, `synchronize`, `reopened` targeting `main`.
- Must block approval when any required check is failing or pending.
- Must post `REQUEST_CHANGES` with file:line for:
  - secrets risk
  - `.env` edits
  - deploy/runtime regression risk
  - missing tests for changed behavior
- Approves only when checklist is fully green.

### `codex-second-pass` (secondary reviewer)
- Trigger: only after `bb-reviewbot` approves.
- Scope: bug/risk/regression-focused second pass.
- Must not merge or deploy.
- Output format:
  - findings first (severity ordered)
  - open questions
  - explicit `APPROVE` or `REQUEST_CHANGES`

## Required Branch/PR Rules
- Protect `main`.
- Require pull request before merge.
- Require `bb-reviewbot` status check.
- Require CI checks: `tests`, `lint`, `secrets-scan`, `python-syntax`.
- Dismiss stale approvals on new commits.
- Block force-pushes to `main`.

## Deploy Standard
- Production deploys only from merged GitHub commit SHA.
- Server working tree must be clean before and after deploy.
- If drift exists (dirty tree or non-matching SHA), deployment must fail and alert.
- Deploy command standard (no `git pull`):  
  `cd /opt/battlebuddy && git fetch origin main && git checkout main && git reset --hard <MERGED_SHA> && systemctl restart battlebuddy`

## No-Blocker Execution Rules
- Never stop at "cannot proceed" without emitting a concrete remediation attempt.
- If blocked by dirty server tree, runbook is:
  1. stop deploy
  2. capture `git status --short` and `git rev-parse HEAD`
  3. alert in PR + Telegram with exact drift
  4. wait for explicit human override
- If blocked by CI/lint/test failure, bot must push a fix commit to the same PR branch and re-run checks.
- If blocked by model/provider rate limit or transient API failure:
  - retry with exponential backoff (20s, 40s, 80s, max 300s)
  - switch to configured fallback model
  - continue from last completed step (no restart from scratch)
- If blocked by missing secrets/permissions, bot must print exact missing secret name and the command that failed.
- Every run must end with a machine-parseable status line:
  - `BOT_RESULT:SUCCESS PR=<url> SHA=<sha> DEPLOYED_SHA=<sha>`
  - or `BOT_RESULT:BLOCKED REASON=<reason> STEP=<step>`

## Rate Limits (Mandatory)
- Run bots in low-throughput mode by default.
- Only one active task per bot at a time.
- Do not run `bb-codebot` and `bb-reviewbot` concurrently on the same repo.
- Add jittered delay between model/API requests: 8-12 seconds.
- On `429` or provider `5xx`: exponential backoff (20s, 40s, 80s, max 300s).
- Respect daily request budgets and auto-pause before hard provider limits.
- Prefer batching context into fewer, larger requests over frequent small requests.
