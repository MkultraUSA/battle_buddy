# Bot Task: Premium Login Regression

## Objective
Fix premium access after successful membership login when usernames differ by case.

## Scope
- `modules/premium.py` only
- add focused regression tests under `tests/`

## Required behavior
- Premium status lookup must be case-insensitive.
- Session issuance must store canonical username from `premium_users` when present.
- Existing admin behavior must remain unchanged.

## Validation
- Add/adjust tests to prove:
  - `premium_users.username = "Paul"` still grants premium when login identity is `"paul"`.
  - issued session row has canonical username and `is_premium=1`.

## Out of scope
- Stripe checkout logic
- deploy workflow changes
- frontend styling/refactors
