# Extract modules/pollers.py

## Objective
Extract background poller threads from audio_receiver.py into a new module: modules/pollers.py to modularize the code and improve maintainability, following the project's modularization goal.

## Scope
- Move background poller threads from audio_receiver.py (approx lines 2171-4064, need to verify exact range) to modules/pollers.py.
- Ensure no circular imports.
- Update audio_receiver.py to import the new pollers.
- Pre-commit checklist MUST pass.

## Plan
1. Validate lines for poller threads.
2. Draft modules/pollers.py, ensuring necessary imports (passing shared variables as params).
3. Patch audio_receiver.py to remove the poller code and import new module.
4. Run static analysis (py_compile).
5. Delegate review.
