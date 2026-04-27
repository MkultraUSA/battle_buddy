"""Tests for transcription thread timeout and watchdog mechanisms.

These tests mock the Whisper model so no GPU/model files are required.
They verify:
  1. TranscriptionWatchdog correctly detects hangs.
  2. transcribe_with_timeout returns "" and resets the watchdog when the
     worker thread hangs beyond the timeout.
  3. transcribe_with_timeout returns the correct transcript on success.
  4. transcribe_with_timeout returns "" when the worker raises an exception.
"""

import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers to build a fake faster_whisper module so the import at the top of
# modules/transcription.py does not require the real package to be installed.
# ---------------------------------------------------------------------------

def _make_fake_faster_whisper():
    """Return a fake `faster_whisper` module with a stub WhisperModel."""
    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = MagicMock()
    return fake_module


import sys

# Inject fake module before importing transcription so the module-level import
# succeeds even when faster-whisper is not installed.
if "faster_whisper" not in sys.modules:
    sys.modules["faster_whisper"] = _make_fake_faster_whisper()

# Now safe to import
import importlib
import modules.transcription as transcription_mod


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestTranscriptionWatchdog(unittest.TestCase):
    """Tests for TranscriptionWatchdog."""

    def setUp(self):
        from modules.transcription import TranscriptionWatchdog
        self.Watchdog = TranscriptionWatchdog

    def test_not_hung_when_idle(self):
        wd = self.Watchdog(timeout=1)
        self.assertFalse(wd.is_hung(), "Watchdog should not be hung when idle")

    def test_not_hung_immediately_after_start(self):
        wd = self.Watchdog(timeout=5)
        wd.mark_start()
        self.assertFalse(wd.is_hung(), "Watchdog should not be hung immediately after start")

    def test_hung_after_timeout_exceeded(self):
        wd = self.Watchdog(timeout=0)  # 0s timeout — instantly hung
        wd.mark_start()
        time.sleep(0.01)  # tiny sleep to ensure monotonic clock advances
        self.assertTrue(wd.is_hung(), "Watchdog should be hung after timeout exceeded")

    def test_not_hung_after_mark_done(self):
        wd = self.Watchdog(timeout=0)
        wd.mark_start()
        time.sleep(0.01)
        wd.mark_done()
        self.assertFalse(wd.is_hung(), "Watchdog should not be hung after mark_done()")

    def test_reset_clears_state(self):
        wd = self.Watchdog(timeout=0)
        wd.mark_start()
        time.sleep(0.01)
        self.assertTrue(wd.is_hung())
        wd.reset()
        self.assertFalse(wd.is_hung(), "Watchdog should not be hung after reset()")


class TestTranscribeWithTimeout(unittest.TestCase):
    """Tests for transcribe_with_timeout()."""

    def _make_wav_bytes(self) -> bytes:
        """Return a minimal stub WAV payload (content doesn't matter for mocked tests)."""
        return b"RIFF\x00\x00\x00\x00WAVEfmt "

    # ------------------------------------------------------------------
    # Helpers that patch the Whisper model layer
    # ------------------------------------------------------------------

    def _patch_model(self, transcribe_side_effect=None, transcribe_return=None):
        """Patch _get_fw_model and _fw_model_lock to avoid real model loading."""
        fake_segment = MagicMock()
        fake_segment.text = "hello world"

        if transcribe_side_effect is not None:
            mock_model = MagicMock()
            mock_model.transcribe.side_effect = transcribe_side_effect
        else:
            if transcribe_return is not None:
                segments = transcribe_return
            else:
                segments = [fake_segment]
            mock_model = MagicMock()
            mock_model.transcribe.return_value = (segments, None)

        return mock_model

    # ------------------------------------------------------------------
    # Test: successful transcription
    # ------------------------------------------------------------------

    def test_success_returns_transcript(self):
        """transcribe_with_timeout returns transcript text on success."""
        fake_segment = MagicMock()
        fake_segment.text = "unit test"
        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([fake_segment], None)

        with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
             patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
            result = transcription_mod.transcribe_with_timeout(
                self._make_wav_bytes(), timeout=10
            )

        self.assertEqual(result, "unit test")

    # ------------------------------------------------------------------
    # Test: thread timeout / hang detection
    # ------------------------------------------------------------------

    def test_timeout_returns_empty_string(self):
        """transcribe_with_timeout returns '' when the worker hangs beyond timeout."""
        hang_event = threading.Event()

        def _hanging_transcribe(*args, **kwargs):
            hang_event.wait(timeout=30)  # blocks effectively forever during test
            return ([], None)

        mock_model = MagicMock()
        mock_model.transcribe.side_effect = _hanging_transcribe

        try:
            with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
                 patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
                result = transcription_mod.transcribe_with_timeout(
                    self._make_wav_bytes(), timeout=1  # short timeout for test speed
                )
        finally:
            hang_event.set()  # unblock the daemon thread so the test exits cleanly

        self.assertEqual(result, "", "Expected empty string on timeout")

    def test_timeout_resets_watchdog(self):
        """Watchdog is reset (not in-progress) after a timeout."""
        hang_event = threading.Event()

        def _hanging_transcribe(*args, **kwargs):
            hang_event.wait(timeout=30)
            return ([], None)

        mock_model = MagicMock()
        mock_model.transcribe.side_effect = _hanging_transcribe

        # Reset module-level watchdog before test
        transcription_mod._watchdog.reset()

        try:
            with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
                 patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
                transcription_mod.transcribe_with_timeout(
                    self._make_wav_bytes(), timeout=1
                )
        finally:
            hang_event.set()

        self.assertFalse(
            transcription_mod._watchdog.is_hung(),
            "Watchdog should be reset after a timeout",
        )

    # ------------------------------------------------------------------
    # Test: exception in worker
    # ------------------------------------------------------------------

    def test_exception_in_worker_returns_empty_string(self):
        """transcribe_with_timeout returns '' when the worker raises an exception."""
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError("model exploded")

        with patch.object(transcription_mod, "_get_fw_model", return_value=mock_model), \
             patch.object(transcription_mod, "_fw_model_lock", threading.Lock()):
            result = transcription_mod.transcribe_with_timeout(
                self._make_wav_bytes(), timeout=10
            )

        self.assertEqual(result, "")

    # ------------------------------------------------------------------
    # Test: public transcribe() delegates correctly
    # ------------------------------------------------------------------

    def test_transcribe_delegates_to_timeout_wrapper(self):
        """The public transcribe() function delegates to transcribe_with_timeout."""
        with patch.object(
            transcription_mod, "transcribe_with_timeout", return_value="delegated"
        ) as mock_fn:
            result = transcription_mod.transcribe(b"wav")

        mock_fn.assert_called_once_with(b"wav", timeout=transcription_mod.TRANSCRIPTION_TIMEOUT)
        self.assertEqual(result, "delegated")


if __name__ == "__main__":
    unittest.main()
