# Changes

- PR #25: https://github.com/MkultraUSA/battle_buddy/pull/25
  - Branch: hermes/fix-missing-improvements
  - Summary: Improve performance and modularity: geocoding, audio handling, and utils.
  - Files: audio_receiver.py, modules/config.py, modules/utils.py

## How to Validate

1. Check out the branch:
   git checkout hermes/fix-missing-improvements

2. Run the existing test suite and confirm all tests pass:
   python -m pytest

3. Exercise the affected modules manually:
   - Verify geocoding returns correct coordinates for a known address.
   - Start audio_receiver.py and confirm audio is received and processed without errors.
3. Exercise the affected modules manually and verify with expected output:
   - Run python3 -m pytest -k "test_geocoding or test_audio"
     Expected Output: ALL PASSED (e.g., "5 passed in 0.12s")
   - Start: python3 audio_receiver.py --test-mode
     Expected Output: Log message "System ready: listening for events" within 5 seconds.

## Rollback

If the changes introduced by PR #25 need to be reverted:

git revert <squash-sha> && systemctl restart battlebuddy && journalctl -u battlebuddy -n 30 --no-pager
