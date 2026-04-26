import threading
import time

_seen_hashes: dict = {}  # hash -> timestamp
_seen_lock = threading.Lock()
_DEDUP_TTL = 300  # seconds

def is_duplicate(audio_hash: str) -> bool:
    with _seen_lock:
        now = time.time()
        if audio_hash in _seen_hashes:
            if now - _seen_hashes[audio_hash] < _DEDUP_TTL:
                return True
        _seen_hashes[audio_hash] = now
        return False
