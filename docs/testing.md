# Testing

This document covers how to run the Battle Buddy test suite locally and an
overview of the CI pipeline.

## Running tests locally

From the repo root:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

## Running with coverage

```bash
pytest --cov=modules --cov-report=term-missing
```

A machine-readable `coverage.xml` is also produced (see `pyproject.toml`).

## Test files

- `tests/test_config.py` — verifies environment-driven config loading and that
  required defaults exist without secrets baked into the repo.
- `tests/test_audio_dedup.py` — exercises the audio-deduplication logic that
  prevents the same call being transcribed twice.
- `tests/test_apd_poller.py` — guards the APD press-release poller against the
  Incapsula block on austintexas.gov by asserting the Google News RSS fallback.
- `tests/test_transcription_timeout.py` — ensures faster-whisper transcription
  respects the configured timeout and fails closed.
- `tests/test_release_artifacts.py` — sanity-checks that release artifacts
  (docs, workflows, required files) are present and well-formed.

## CI pipeline

Three GitHub Actions workflows run on every push and PR to `main`:

- `.github/workflows/tests.yml` — installs deps and runs `pytest`, uploads
  `coverage.xml` as a build artifact.
- `.github/workflows/lint.yml` — runs `ruff check .` for style and import
  hygiene.
- `.github/workflows/secrets-scan.yml` — runs gitleaks to block accidental
  credential commits.
- `.github/workflows/python-syntax.yml` — runs `py_compile` over the tree as
  a fast smoke check.

A green badge for each workflow appears at the top of the README.
