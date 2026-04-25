# REFACTOR_SPEC.md

## Objective
Extract incident detection and ATAK integration from audio_receiver.py into a new module: modules/incident_engine.py.

## Scope
- Move code from audio_receiver.py lines 1197-1669 (as specified in Task 7) to modules/incident_engine.py.
- Ensure no circular imports (the new module must not import from audio_receiver.py).
- Update audio_receiver.py to import the new class/functions.

## Risks
1. Circular imports (critical).
2. Missing dependencies (imports within the moved code).
3. Incorrect identification of range (ensure logic is self-contained).

## Plan
1. Validate lines 1197-1669.
2. Draft modules/incident_engine.py.
3. Patch audio_receiver.py to remove code and import new module.
4. Run static analysis (py_compile).
