import io
import os
import tempfile
import threading
import time
import wave
from collections import deque
from typing import Optional

from faster_whisper import WhisperModel as _FasterWhisperModel

_fw_model            = None
_fw_model_lock       = threading.Lock()
_MAX_PROCESS_THREADS = 8
_BROADCASTIFY_MAX    = 4
_process_sem         = threading.Semaphore(_MAX_PROCESS_THREADS)
_broadcastify_sem    = threading.Semaphore(_BROADCASTIFY_MAX)


_METRICS_RETENTION_SECONDS = 86400
_metrics_lock = threading.Lock()
_metrics_events = deque()
_metrics_in_progress = 0
_metrics_totals = {
    "started": 0,
    "success": 0,
    "empty": 0,
    "timeout": 0,
    "exception": 0,
    "lock_timeout": 0,
}


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate()
            return (wf.getnframes() / rate) if rate else 0.0
    except Exception:
        return 0.0


def _prune_metrics(now: Optional[float] = None) -> None:
    now = now or time.time()
    cutoff = now - _METRICS_RETENTION_SECONDS
    while _metrics_events and _metrics_events[0]["ts"] < cutoff:
        _metrics_events.popleft()


def _record_transcription_start() -> float:
    global _metrics_in_progress
    started = time.monotonic()
    with _metrics_lock:
        _metrics_totals["started"] += 1
        _metrics_in_progress += 1
    return started


def _record_transcription_done(
    status: str,
    started_monotonic: float,
    audio_seconds: float,
    transcript_chars: int = 0,
) -> None:
    global _metrics_in_progress
    latency_seconds = max(0.0, time.monotonic() - started_monotonic)
    now = time.time()
    with _metrics_lock:
        _metrics_totals[status] = _metrics_totals.get(status, 0) + 1
        _metrics_in_progress = max(0, _metrics_in_progress - 1)
        _metrics_events.append(
            {
                "ts": now,
                "status": status,
                "latency_seconds": latency_seconds,
                "audio_seconds": float(audio_seconds or 0.0),
                "transcript_chars": int(transcript_chars or 0),
            }
        )
        _prune_metrics(now)


def get_transcription_observability(now: Optional[float] = None) -> dict:
    """Return rolling STT-stage metrics for Prometheus collection."""
    now = now or time.time()
    windows = {
        "15m": now - (15 * 60),
        "1h": now - 3600,
        "24h": now - 86400,
    }
    with _metrics_lock:
        _prune_metrics(now)
        events = list(_metrics_events)
        totals = dict(_metrics_totals)
        in_progress = _metrics_in_progress

    by_window = {}
    for label, cutoff in windows.items():
        subset = [event for event in events if event["ts"] >= cutoff]
        latencies = sorted(event["latency_seconds"] for event in subset)
        count = len(subset)
        status_counts = {
            "success": 0,
            "empty": 0,
            "timeout": 0,
            "exception": 0,
            "lock_timeout": 0,
        }
        for event in subset:
            status_counts[event["status"]] = status_counts.get(event["status"], 0) + 1
        by_window[label] = {
            "completed": count,
            "success": status_counts.get("success", 0),
            "empty": status_counts.get("empty", 0),
            "timeout": status_counts.get("timeout", 0),
            "exception": status_counts.get("exception", 0),
            "lock_timeout": status_counts.get("lock_timeout", 0),
            "audio_seconds": sum(event["audio_seconds"] for event in subset),
            "transcript_chars": sum(event["transcript_chars"] for event in subset),
            "avg_latency_seconds": (sum(latencies) / count) if count else 0.0,
            "p95_latency_seconds": latencies[int((count - 1) * 0.95)] if count else 0.0,
            "success_ratio": (status_counts.get("success", 0) / count) if count else 0.0,
        }

    return {
        "provider": "local_faster_whisper",
        "model": "distil-large-v3",
        "in_progress": in_progress,
        "totals": totals,
        "windows": by_window,
    }


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
    status_container: list[str] = []
    accuracy_container: list[float] = []
    audio_seconds = _wav_duration_seconds(wav_bytes)
    metrics_started = _record_transcription_start()

    def _worker() -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp = f.name
        try:
            acquired = _fw_model_lock.acquire(timeout=TRANSCRIPTION_TIMEOUT)
            if not acquired:
                print("[whisper] TIMEOUT waiting for model lock - dropping call", flush=True)
                status_container.append("lock_timeout")
                result_container.append("")
                return
            lock_held = True
            try:
                model = _get_fw_model()
                segments, _ = model.transcribe(tmp, language="en", beam_size=1, vad_filter=True)
                seg_texts = []
                seg_logprobs = []
                for s in segments:
                    seg_texts.append(s.text)
                    seg_logprobs.append(s.avg_logprob)
                text = " ".join(seg_texts).strip()
                status_container.append("success" if text else "empty")
                result_container.append(text)
                if seg_logprobs:
                    accuracy_container.append(sum(seg_logprobs) / len(seg_logprobs))
                else:
                    accuracy_container.append(0.0)
            finally:
                if lock_held:
                    try:
                        _fw_model_lock.release()
                    except RuntimeError:
                        pass
        except Exception as e:
            print(f"[whisper] error: {e}", flush=True)
            status_container.append("exception")
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
        _record_transcription_done("timeout", metrics_started, audio_seconds)
        return "", 0.0

    _watchdog.mark_done()

    status = status_container[0] if status_container else "empty"
    transcript = result_container[0] if result_container else ""
    accuracy = accuracy_container[0] if accuracy_container else 0.0
    _record_transcription_done(status, metrics_started, audio_seconds, len(transcript))

    if exception_container:
        return "", 0.0
    return transcript, accuracy


def transcribe(wav_bytes: bytes) -> tuple[str, float]:
    """Transcribe audio bytes using Whisper.

    This is the public API used by the rest of the application.
    Internally delegates to ``transcribe_with_timeout`` to guard against hangs.

    Returns (transcript_text, avg_logprob) where avg_logprob is Whisper's
    per-segment confidence averaged across all segments (higher = more confident).
    """
    return transcribe_with_timeout(wav_bytes, timeout=TRANSCRIPTION_TIMEOUT)
