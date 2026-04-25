# Plan for PR 4: Extract geocoding module

## Overview
Extract `extract_location` and associated geocoding logic from `audio_receiver.py` (approx lines 455-600) into a new module: `modules/geocoding.py`.

## Files
1. `modules/geocoding.py` (New):
   - Import necessary libraries (`sqlite3`, `time`, `re`, `os`).
   - Define constants previously in `audio_receiver.py` (e.g., `DB_PATH`, geocoding configuration).
   - Contains `extract_location` function (from line 455).
   - Contains `_geocode_load_db` and `_geocode_save_db` helpers.

2. `audio_receiver.py` (Modify):
   - Import `geocoding` module.
   - Remove original `extract_location` and related helper functions.
   - Update references to call `geocoding.extract_location`.

## Risks & Mitigation
- **Circular Imports**: `geocoding.py` must NOT import from `audio_receiver.py`. Ensure configuration variables (`DB_PATH`) are passed as arguments or moved to a central configuration if needed. Currently, `DB_PATH` is a global constant in `audio_receiver.py`. I will move `DB_PATH` to `modules/config.py` if necessary or pass it into the functions from `audio_receiver.py`.
- **Performance**: Ensure no additional overhead in calling across module boundaries.
- **Side effects**: None expected as functions are pure-ish with DB calls.

## Validation
- Verify `audio_receiver.py` compiles with `python3 -m py_compile audio_receiver.py`.
- Ensure functionality is preserved by calling `extract_location` in a test script.
- Rollback: `git revert HEAD` and restart service.
