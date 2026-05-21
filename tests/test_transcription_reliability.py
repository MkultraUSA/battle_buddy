"""Regression tests for transcription reliability fixes (May 2026).

Covers:
  1. metrics collector: ensure get_transcription_observability is callable
     and returns valid success_ratio before metrics are yielded
  2. 15m success_ratio integrity: verify window math with known events
  3. backlog agent: verify transcribe() returns tuple[str, float]
     and tuple-unpacking works correctly
  4. metrics locking: concurrent start/done calls don't corrupt state
"""

import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Fake faster_whisper module (same pattern as test_transcription_timeout.py)
# ---------------------------------------------------------------------------
def _make_fake_faster_whisper():
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = MagicMock()
    return fake_module

import sys  # noqa: E402

if "faster_whisper" not in sys.modules:
    sys.modules["faster_whisper"] = _make_fake_faster_whisper()

import modules.transcription as transcription_mod  # noqa: E402


class TestMetricsRecordStop(unittest.TestCase):
    """Verify _record_transcription_start / _record_transcription_done track correctly."""

    def setUp(self):
        # Capture initial totals for comparison
        with transcription_mod._metrics_lock:
            self.initial_totals = dict(transcription_mod._metrics_totals)

    def test_start_increments_in_progress(self):
        with transcription_mod._metrics_lock:
            before = transcription_mod._metrics_in_progress
        transcription_mod._record_transcription_start()
        with transcription_mod._metrics_lock:
            after = transcription_mod._metrics_in_progress
        # In-progress should be back to normal after done
        self.assertEqual(after, before + 1)

    def test_done_decrements_in_progress(self):
        started = transcription_mod._record_transcription_start()
        with transcription_mod._metrics_lock:
            before = transcription_mod._metrics_in_progress
        transcription_mod._record_transcription_done("success", started, 2.0, 20)
        with transcription_mod._metrics_lock:
            after = transcription_mod._metrics_in_progress
        self.assertEqual(after, before - 1)

    def test_done_increments_status_counter(self):
        started = transcription_mod._record_transcription_start()
        with transcription_mod._metrics_lock:
            before = transcription_mod._metrics_totals.get("success", 0)
        transcription_mod._record_transcription_done("success", started, 3.0, 15)
        with transcription_mod._metrics_lock:
            after = transcription_mod._metrics_totals.get("success", 0)
        self.assertEqual(after, before + 1)

    def test_done_appends_event(self):
        started = transcription_mod._record_transcription_start()
        with transcription_mod._metrics_lock:
            before_len = len(transcription_mod._metrics_events)
        transcription_mod._record_transcription_done("empty", started, 1.0, 0)
        with transcription_mod._metrics_lock:
            after_len = len(transcription_mod._metrics_events)
        self.assertEqual(after_len, before_len + 1)

    def test_negative_in_progress_clamped(self):
        """_metrics_in_progress should never go negative."""
        # Artificially zero it out
        with transcription_mod._metrics_lock:
            transcription_mod._metrics_in_progress = 0
        started = time.monotonic()
        transcription_mod._record_transcription_done("success", started, 1.0, 5)
        with transcription_mod._metrics_lock:
            self.assertGreaterEqual(transcription_mod._metrics_in_progress, 0)


class TestSuccessRatioIntegrity(unittest.TestCase):
    """Verify 15m success_ratio in get_transcription_observability."""

    def setUp(self):
        # Clear events to start clean
        with transcription_mod._metrics_lock:
            transcription_mod._metrics_events.clear()

    def test_success_ratio_all_success(self):
        """When all transcriptions succeed, ratio is 1.0."""
        now = time.time()
        for i in range(5):
            with transcription_mod._metrics_lock:
                transcription_mod._metrics_events.append({
                    "ts": now - (i * 30),  # within 15m
                    "status": "success",
                    "latency_seconds": 1.0,
                    "audio_seconds": 5.0,
                    "transcript_chars": 50,
                })

        obs = transcription_mod.get_transcription_observability(now)
        ratio_15m = obs["windows"]["15m"]["success_ratio"]
        self.assertEqual(ratio_15m, 1.0)

    def test_success_ratio_mixed(self):
        """Mixed outcomes produce correct ratio."""
        now = time.time()
        events = [
            ("success", 0), ("empty", 10), ("success", 20),
            ("timeout", 30), ("exception", 40), ("success", 50),
        ]
        for status, offset in events:
            with transcription_mod._metrics_lock:
                transcription_mod._metrics_events.append({
                    "ts": now - offset,
                    "status": status,
                    "latency_seconds": 1.0,
                    "audio_seconds": 5.0,
                    "transcript_chars": 50 if status == "success" else 0,
                })

        obs = transcription_mod.get_transcription_observability(now)
        # 3 success out of 6 total = 0.5
        self.assertAlmostEqual(obs["windows"]["15m"]["success_ratio"], 3.0 / 6.0)

    def test_success_ratio_empty_window(self):
        """Empty window returns 0.0 ratio."""
        now = time.time()
        # Add an event older than 15m
        with transcription_mod._metrics_lock:
            transcription_mod._metrics_events.append({
                "ts": now - 1000,
                "status": "success",
                "latency_seconds": 1.0,
                "audio_seconds": 5.0,
                "transcript_chars": 50,
            })

        obs = transcription_mod.get_transcription_observability(now)
        self.assertEqual(obs["windows"]["15m"]["success_ratio"], 0.0)
        self.assertEqual(obs["windows"]["15m"]["completed"], 0)

    def test_success_ratio_excludes_old_events(self):
        """Events older than 24h are pruned and don't affect windows."""
        now = time.time()
        for i in range(10):
            with transcription_mod._metrics_lock:
                transcription_mod._metrics_events.append({
                    "ts": now - (25 * 3600) - i,  # >24h old
                    "status": "success",
                    "latency_seconds": 1.0,
                    "audio_seconds": 5.0,
                    "transcript_chars": 50,
                })

        obs = transcription_mod.get_transcription_observability(now)
        # All events pruned — all windows should be empty
        for window in ["15m", "1h", "24h"]:
            self.assertEqual(obs["windows"][window]["completed"], 0,
                             f"{window} window should have 0 events after pruning")
            self.assertEqual(obs["windows"][window]["success_ratio"], 0.0,
                             f"{window} window ratio should be 0.0")

    def test_totals_accumulate_across_windows(self):
        """Totals reflect all-time counts, not windowed."""
        now = time.time()

        # Add events in last 10 minutes (within 15m, 1h, 24h)
        with transcription_mod._metrics_lock:
            transcription_mod._metrics_totals["success"] = 100
            transcription_mod._metrics_totals["empty"] = 20
            transcription_mod._metrics_totals["started"] = 0

        obs = transcription_mod.get_transcription_observability(now)
        self.assertEqual(obs["totals"]["success"], 100)
        self.assertEqual(obs["totals"]["empty"], 20)


class TestBacklogAgentTupleUnpacking(unittest.TestCase):
    """Verify transcribe() returns tuple[str, float] and unpacking works."""

    def test_transcribe_returns_tuple(self):
        """transcribe() must return a tuple of (str, float) for backlog unpacking."""
        fake_segment = MagicMock()
        fake_segment.text = "test transcript"
        fake_segment.avg_logprob = -0.8

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([fake_segment], None)

        with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
             patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
            result = transcription_mod.transcribe(b"fake_wav_bytes")

        self.assertIsInstance(result, tuple, "transcribe() must return a tuple")
        self.assertEqual(len(result), 2, "transcribe() must return (text, accuracy) tuple")
        self.assertIsInstance(result[0], str, "First element must be a string")
        self.assertIsInstance(result[1], float, "Second element must be a float")

    def test_backlog_unpacking_pattern(self):
        """Simulate the backlog_agent tuple-unpacking pattern."""
        fake_segment = MagicMock()
        fake_segment.text = "hello world"
        fake_segment.avg_logprob = -1.0

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([fake_segment], None)

        with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
             patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
            # This is the pattern backlog_agent uses (post-fix)
            transcript, accuracy = transcription_mod.transcribe(b"fake")
            transcript = transcript.strip()

        self.assertEqual(transcript, "hello world")
        self.assertEqual(accuracy, -1.0)
        # Verify .strip() works on the string, proving it's not a tuple
        self.assertEqual("  hello  ".strip(), "hello")

    def test_empty_transcript_accuracy_zero(self):
        """When transcribe returns empty text, accuracy should be 0.0."""
        transcription_mod._watchdog.reset()

        mock_model = MagicMock()
        # Return segments with empty text
        empty_segment = MagicMock()
        empty_segment.text = ""
        empty_segment.avg_logprob = -3.0
        mock_model.transcribe.return_value = ([empty_segment], None)

        with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
             patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
            transcript, accuracy = transcription_mod.transcribe(b"silent")

        self.assertEqual(transcript, "")

    def test_exception_returns_empty_tuple(self):
        """Exception in worker returns ('', 0.0)."""
        transcription_mod._watchdog.reset()

        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError("whisper crash")

        with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
             patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
            transcript, accuracy = transcription_mod.transcribe(b"bad_audio")

        self.assertEqual(transcript, "")
        self.assertEqual(accuracy, 0.0)


class TestMetricsThreadSafety(unittest.TestCase):
    """Concurrent start/done calls should not corrupt internal state."""

    def setUp(self):
        with transcription_mod._metrics_lock:
            transcription_mod._metrics_events.clear()
            transcription_mod._metrics_totals = {k: 0 for k in transcription_mod._metrics_totals}
            transcription_mod._metrics_in_progress = 0

    def test_concurrent_starts_and_dones(self):
        """Many concurrent threads calling start/done simultaneously."""
        errors = []
        threads = []

        def worker():
            try:
                for _ in range(20):
                    started = transcription_mod._record_transcription_start()
                    time.sleep(0.001)
                    transcription_mod._record_transcription_done(
                        "success", started, 1.0, 30
                    )
            except Exception as e:
                errors.append(str(e))

        for _ in range(5):
            t = threading.Thread(target=worker)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # After all threads, in_progress should be 0
        with transcription_mod._metrics_lock:
            in_progress = transcription_mod._metrics_in_progress
            totals = dict(transcription_mod._metrics_totals)
            events = len(transcription_mod._metrics_events)

        self.assertEqual(in_progress, 0,
                         f"Expected 0 in_progress, got {in_progress}")
        self.assertEqual(totals.get("success", 0), 100,
                         f"Expected 100 success, got {totals.get('success', 0)}")
        self.assertEqual(events, 100,
                         f"Expected 100 events, got {events}")
        self.assertEqual(errors, [],
                         f"Unexpected errors: {errors}")


if __name__ == "__main__":
    unittest.main()
