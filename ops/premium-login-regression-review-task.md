# Bot Review Task: Premium Login Regression

## Review checklist
1. Confirm `modules/premium.py` premium lookup is case-insensitive.
2. Confirm session issuance stores canonical `premium_users.username` when present.
3. Confirm no secrets/env values were added.
4. Confirm regression test exists and fails before/fixes after.
5. Confirm no unrelated refactor drift in touched files.

## Expected review output
- `APPROVE` or `REQUEST_CHANGES`
- If requesting changes: include file:line and exact correction.
