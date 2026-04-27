import os
import tempfile
import threading
import time
from typing import Optional

from faster_whisper import WhisperModel as _FasterWhisperModel

_fw_model            = None
_fw_model_lock       = threading.Lock()
_MAX_PROCESS_THREADS = 20
_BROADCASTIFY_MAX    = 15
_process_sem         = threading.Semaphore(_MAX_PROCESS_THREADS)
_broadcastify_sem    = threading.Semaphore(_BROADCASTIFY_MAX)

# Timeout in seconds before a hung transcription thread is abandoned.
TRANSCRIPTION_TIMEOUT = 300


def _get_fw_model() -> _FasterWhisperModel:
    global _fw_model
    if _fw_model is None:
        print("[whisper] loading faster-whisper large-v3-turbo int8...", flush=True)
        _fw_model = _FasterWhisperModel("distil-large-v3", device="cpu", compute_type="int8",
                                        cpu_threads=2, num_workers=1)
        print("[whisper] model ready", flush=True)
    return _fw_model


class TranscriptionWatchdog:
    """Monitors transcription activity and detects hangs.

    Tracks the timestamp of the last successfully completed transcription.
    Call ``is_hung()`` to check whether more than ``timeout`` seconds have
    elapsed since the last activity while a transcription is in-flight.

    Usage::

        watchdog = TranscriptionWatchdog(timeout=300)
        watchdog.mark_start()
        # ... transcription running ...
        watchdog.mark_done()

        if watchdog.is_hung():
            # handle hang
    """

    def __init__(self, timeout: int = TRANSCRIPTION_TIMEOUT) -> None:
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_processed_time: Optional[float] = None
        self._in_progress: bool = False

    def mark_start(self) -> None:
        """Record that a transcription has started."""
        with self._lock:
            self._in_progress = True
            if self._last_processed_time is None:
                self._last_processed_time = time.monotonic()

    def mark_done(self) -> None:
        """Record that a transcription completed successfully."""
        with self._lock:
            self._in_progress = False
            self._last_processed_time = time.monotonic()

    def is_hung(self) -> bool:
        """Return True if a transcription has been running longer than ``timeout`` seconds."""
        with self._lock:
            if not self._in_progress:
                return False
            if self._last_processed_time is None:
                return False
            elapsed = time.monotonic() - self._last_processed_time
            return elapsed > self.timeout

    def reset(self) -> None:
        """Reset watchdog state (e.g. after restarting the transcription thread)."""
        with self._lock:
            self._in_progress = False
            self._last_processed_time = None


# Module-level singleton watchdog (used by transcribe_with_timeout).
_watchdog = TranscriptionWatchdog(timeout=TRANSCRIPTION_TIMEOUT)


def transcribe_with_timeout(wav_bytes: bytes, timeout: int = TRANSCRIPTION_TIMEOUT) -> str:
    """Transcribe ``wav_bytes`` in a worker thread and abort if it takes longer than ``timeout`` seconds.

    Returns the transcription text, or an empty string if the operation times out or fails.
    The module-level ``_watchdog`` is updated so callers can monitor health.
    """
    result_container: list[str] = []
    exception_container: list[Exception] = []

    def _worker() -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp = f.name
        try:
            acquired = _fw_model_lock.acquire(timeout=90)
            if not acquired:
                print("[whisper] TIMEOUT waiting for model lock — dropping call", flush=True)
                result_container.append("")
                return
            lock_held = True
            try:
                model = _get_fw_model()
                segments, _ = model.transcribe(tmp, language="en", beam_size=1, vad_filter=True)
                result_container.append(" ".join(s.text for s in segments).strip())
            finally:
                if lock_held:
                    try:
                        _fw_model_lock.release()
                    except RuntimeError:
                        pass
        except Exception as e:
            print(f"[whisper] error: {e}", flush=True)
            exception_container.append(e)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    _watchdog.mark_start()
    thread = threading.Thread(target=_worker, name="whisper-worker", daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        # Thread is still running after timeout — abandon it and signal hang.
        print(
            f"[whisper] transcription thread hung after {timeout}s — abandoning",
            flush=True,
        )
        _watchdog.reset()
        return ""

    _watchdog.mark_done()

    if exception_container:
        return ""
    return result_container[0] if result_container else ""


def transcribe(wav_bytes: bytes) -> str:
    """Transcribe audio bytes using Whisper.

    This is the public API used by the rest of the application.
    Internally delegates to ``transcribe_with_timeout`` to guard against hangs.
    """
    return transcribe_with_timeout(wav_bytes, timeout=TRANSCRIPTION_TIMEOUT)
