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

## Checks that need something other than pytest

Two checks in this repo are not pytest tests, and it is worth knowing which is
which before assuming a green suite covered them.

**`scripts/check_camera_js.mjs` — runs in CI, via pytest.** The browser's camera
frame gate lives in `static/js/public_map.js`, and pytest can only read it as
text. This lifts the real functions out of the real file and feeds them hostile
URLs and a hostile camera name. It has no npm dependencies.

```bash
node scripts/check_camera_js.mjs            # exit 0 = every property held
```

`tests/test_camera_js_gate.py` shells out to it, so it is counted, reported and
alerted on like any other test. If `node` is missing it skips locally and **fails
in CI** — a check that cannot run must never be reported as one that did.

**`scripts/map_interaction_check.mjs` — manual, needs a browser.** Drives
headless Chrome over the DevTools Protocol to click a real camera dot on a real
page and confirm the popup opens and the city's frame decodes. No npm
dependencies; it finds Chrome itself or honours `CHROME_BIN`.

```bash
node scripts/map_interaction_check.mjs                              # production
node scripts/map_interaction_check.mjs http://127.0.0.1:8080/       # local
```

Exit 0 passed, 1 a property failed, 2 it could not run at all (no Chrome). That
third code is deliberate: the expensive half of this check cannot run in CI, and
this script is the only thing standing between "the dots are drawn" and "the
dots can be clicked". It is how the swallowed-click defect in #182 was found —
820 dots drew perfectly, the console was clean, and no popup had ever opened.

## CI pipeline

Four GitHub Actions workflows run on every push and PR to `main`:

- `.github/workflows/tests.yml` — installs deps and runs `pytest`, uploads
  `coverage.xml` as a build artifact.
- `.github/workflows/lint.yml` — runs `ruff check .` for style and import
  hygiene.
- `.github/workflows/secrets-scan.yml` — runs gitleaks to block accidental
  credential commits.
- `.github/workflows/python-syntax.yml` — runs `py_compile` over the tree as
  a fast smoke check.

A green badge for each workflow appears at the top of the README.
