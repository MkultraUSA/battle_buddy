# Changes

- PR #39 (hermes/refactor-afd-open-data-thread):
  Refactor `afd_open_data_thread` from `modules/pollers.py` to
  `modules/pollers/impl/afd_news.py` as `AFDOpenDataPoller` (BasePoller subclass).
  - Created `modules/pollers/base.py` — abstract `BasePoller` class
  - Created `modules/pollers/impl/__init__.py` — empty package marker
  - Created `modules/pollers/impl/afd_news.py` — `AFDOpenDataPoller` class
  - Created `modules/pollers/__init__.py` — compatibility shim with
    `afd_open_data_thread()` deprecation wrapper
  - Removed `afd_open_data_thread`, `_afd_post_to_talk`, `_afd_issue_to_itype`,
    `_afd_active_ids`, `_afd_lock`, `AFD_OPEN_DATA_URL`, `AFD_POLL_INTERVAL`,
    and `_AFD_ITYPE_MAP` from `modules/pollers.py`
  - Updated `audio_receiver.py` to call `AFDOpenDataPoller().start()` directly
  - Preserves full backward compatibility via `afd_open_data_thread()` shim

- PR #25: https://github.com/MkultraUSA/battle_buddy/pull/25
  - Branch: hermes/fix-missing-improvements
  - Summary: Improve performance and modularity: geocoding, audio handling, and utils.
  - Files: audio_receiver.py, modules/config.py, modules/utils.py

## How to Validate

1. Check out the branch:
   git checkout hermes/fix-missing-improvements

2. Run the existing test suite and confirm all tests pass:
   python -m pytest

3. Exercise the affected modules manually and verify with expected output:
   - Run python3 -m pytest -k "test_geocoding or test_audio"
     Expected Output: ALL PASSED (e.g., "5 passed in 0.12s")
   - Start: python3 audio_receiver.py --test-mode
     Expected Output: Log message "System ready: listening for events" within 5 seconds.

## Rollback

If the changes introduced by PR #25 need to be reverted:

git revert HEAD && systemctl restart battlebuddy && journalctl -u battlebuddy -n 30 --no-pager
