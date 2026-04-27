"""
audio_dedup.py — Audio deduplication for Battle Buddy.

Tracks SHA-256 hashes of raw WAV bytes to prevent processing the same
clip twice within the TTL window.  Thread-safe; uses a module-level lock.

Public API
----------
is_duplicate(audio_hash: str) -> bool
    Returns True if the hash was already seen within the TTL window.
    **Does not** register the hash — call mark_seen() separately.

mark_seen(audio_hash: str) -> None
    Record that this hash was seen right now.

is_duplicate_and_mark(audio_hash: str) -> bool
    Atomic check-and-mark: returns True if duplicate, otherwise marks and
    returns False.  Preferred over calling is_duplicate + mark_seen
    separately because it holds the lock for the entire operation.
"""

import threading
import time

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_seen_hashes: dict = {}   # hash -> float (epoch timestamp)
_seen_lock = threading.Lock()
_DEDUP_TTL = 300          # seconds — evict entries older than this


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _evict_expired(now: float) -> None:
    """Remove entries older than _DEDUP_TTL.  Must be called with _seen_lock held."""
    expired = [k for k, v in _seen_hashes.items() if now - v >= _DEDUP_TTL]
    for k in expired:
        del _seen_hashes[k]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_duplicate(audio_hash: str) -> bool:
    """Return True if *audio_hash* was already seen within the TTL window.

    Does **not** register the hash.  Use :func:`mark_seen` afterwards if you
    want to record it, or use :func:`is_duplicate_and_mark` for an atomic
    check-and-mark.
    """
    now = time.time()
    with _seen_lock:
        _evict_expired(now)
        return audio_hash in _seen_hashes


def mark_seen(audio_hash: str) -> None:
    """Record *audio_hash* as seen at the current time."""
    now = time.time()
    with _seen_lock:
        _evict_expired(now)
        _seen_hashes[audio_hash] = now


def is_duplicate_and_mark(audio_hash: str) -> bool:
    """Atomically check and mark *audio_hash*.

    Returns True if the hash was already in the cache (i.e. it is a
    duplicate — the hash is **not** re-registered).
    Returns False if the hash was new; in that case it is registered now.
    """
    now = time.time()
    with _seen_lock:
        _evict_expired(now)
        if audio_hash in _seen_hashes:
            return True
        _seen_hashes[audio_hash] = now
        return False
