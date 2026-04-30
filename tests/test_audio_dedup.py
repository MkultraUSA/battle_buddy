"""Unit tests for modules.audio_dedup.

Tests verify thread-safety, TTL eviction, and all three public functions:
  - is_duplicate()
  - mark_seen()
  - is_duplicate_and_mark()
"""

import threading
import time
import unittest

import modules.audio_dedup as dedup_mod


def _reset():
    """Clear module-level state between tests."""
    with dedup_mod._seen_lock:
        dedup_mod._seen_hashes.clear()


class TestIsDuplicate(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_new_hash_not_duplicate(self):
        self.assertFalse(dedup_mod.is_duplicate("aabbcc"))

    def test_after_mark_seen_is_duplicate(self):
        dedup_mod.mark_seen("aabbcc")
        self.assertTrue(dedup_mod.is_duplicate("aabbcc"))

    def test_different_hash_not_duplicate(self):
        dedup_mod.mark_seen("hash1")
        self.assertFalse(dedup_mod.is_duplicate("hash2"))

    def test_expired_hash_not_duplicate(self):
        """An entry older than TTL should be evicted and reported as not-duplicate."""
        h = "expiring_hash"
        with dedup_mod._seen_lock:
            # Plant a timestamp well before the TTL window
            dedup_mod._seen_hashes[h] = time.time() - (dedup_mod._DEDUP_TTL + 10)
        self.assertFalse(dedup_mod.is_duplicate(h))

    def test_is_duplicate_does_not_register(self):
        """is_duplicate() must not side-effect the cache."""
        self.assertFalse(dedup_mod.is_duplicate("no_register"))
        # Still not in the cache after the check
        with dedup_mod._seen_lock:
            self.assertNotIn("no_register", dedup_mod._seen_hashes)


class TestMarkSeen(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_mark_seen_registers_hash(self):
        dedup_mod.mark_seen("myHash")
        with dedup_mod._seen_lock:
            self.assertIn("myHash", dedup_mod._seen_hashes)

    def test_mark_seen_evicts_expired(self):
        old = "old_hash"
        with dedup_mod._seen_lock:
            dedup_mod._seen_hashes[old] = time.time() - (dedup_mod._DEDUP_TTL + 1)
        dedup_mod.mark_seen("new_hash")
        with dedup_mod._seen_lock:
            self.assertNotIn(old, dedup_mod._seen_hashes)
            self.assertIn("new_hash", dedup_mod._seen_hashes)


class TestIsDuplicateAndMark(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_new_hash_returns_false_and_registers(self):
        result = dedup_mod.is_duplicate_and_mark("fresh")
        self.assertFalse(result)
        with dedup_mod._seen_lock:
            self.assertIn("fresh", dedup_mod._seen_hashes)

    def test_second_call_returns_true(self):
        dedup_mod.is_duplicate_and_mark("fresh")
        result = dedup_mod.is_duplicate_and_mark("fresh")
        self.assertTrue(result)

    def test_expired_hash_not_duplicate_and_reregisters(self):
        h = "expire_me"
        with dedup_mod._seen_lock:
            dedup_mod._seen_hashes[h] = time.time() - (dedup_mod._DEDUP_TTL + 5)
        result = dedup_mod.is_duplicate_and_mark(h)
        self.assertFalse(result, "Expired entry should not count as duplicate")
        with dedup_mod._seen_lock:
            # Should be re-registered with fresh timestamp
            self.assertIn(h, dedup_mod._seen_hashes)
            self.assertGreater(dedup_mod._seen_hashes[h], time.time() - 5)

    def test_thread_safety(self):
        """Only one of N concurrent threads registering the same hash wins."""
        results = []
        barrier = threading.Barrier(10)

        def _try_register():
            barrier.wait()
            results.append(dedup_mod.is_duplicate_and_mark("race"))

        threads = [threading.Thread(target=_try_register) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread should see False (the first to register)
        false_count = results.count(False)
        true_count = results.count(True)
        self.assertEqual(false_count, 1, f"Expected exactly 1 non-duplicate, got {false_count}")
        self.assertEqual(true_count, 9, f"Expected 9 duplicates, got {true_count}")


if __name__ == "__main__":
    unittest.main()
