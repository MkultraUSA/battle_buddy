import os
import threading
import tempfile
from faster_whisper import WhisperModel as _FasterWhisperModel

_fw_model = None
_fw_model_lock = threading.Lock()
_MAX_PROCESS_THREADS = 20
_process_sem = threading.Semaphore(_MAX_PROCESS_THREADS)

def _get_fw_model() -> _FasterWhisperModel:
    global _fw_model
    if _fw_model is None:
        print("[whisper] loading faster-whisper large-v3-turbo int8...", flush=True)
        _fw_model = _FasterWhisperModel("distil-large-v3", device="cpu", compute_type="int8",
                                        cpu_threads=2, num_workers=1)
        print("[whisper] model ready", flush=True)
    return _fw_model

def transcribe(wav_bytes: bytes) -> str:
    """Transcribe audio locally with faster-whisper. No external API needed."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp = f.name
    try:
        acquired = _fw_model_lock.acquire(timeout=90)
        if not acquired:
            print("[whisper] TIMEOUT waiting for model lock — dropping call", flush=True)
            return ""
        try:
            model = _get_fw_model()
            segments, _ = model.transcribe(tmp, language="en", beam_size=1,
                                           vad_filter=True)
            return " ".join(s.text for s in segments).strip()
        finally:
            _fw_model_lock.release()
    except Exception as e:
        print(f"[whisper] error: {e}", flush=True)
        return ""
    finally:
        os.unlink(tmp)
