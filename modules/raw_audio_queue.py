import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

RAW_AUDIO_QUEUE_DIR = Path(
    os.environ.get("BB_RAW_AUDIO_QUEUE_DIR", "/opt/battlebuddy/raw_audio_queue")
)


def _pending_dir() -> Path:
    path = _queue_dir("pending")
    return path


def _queue_dir(name: str) -> Path:
    path = RAW_AUDIO_QUEUE_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def enqueue_raw_audio(
    *,
    ts: float,
    tgid: int,
    tag: str,
    category: str,
    node: str,
    duration: float,
    wav_bytes: bytes,
    default_lat: Optional[float] = None,
    default_lon: Optional[float] = None,
) -> str:
    """Persist a received radio clip before it enters the lossy STT path."""
    pending = _pending_dir()
    item_id = f"{int(ts * 1000)}-{tgid}-{uuid.uuid4().hex[:10]}"
    wav_name = f"{item_id}.wav"
    meta_name = f"{item_id}.json"

    wav_tmp = pending / f".{wav_name}.tmp"
    meta_tmp = pending / f".{meta_name}.tmp"
    wav_path = pending / wav_name
    meta_path = pending / meta_name

    wav_tmp.write_bytes(wav_bytes)
    os.replace(wav_tmp, wav_path)

    metadata = {
        "id": item_id,
        "created_ts": time.time(),
        "ts": float(ts),
        "tgid": int(tgid),
        "tag": tag,
        "category": category,
        "node": node,
        "duration": float(duration or 0.0),
        "wav_file": wav_name,
        "default_lat": default_lat,
        "default_lon": default_lon,
    }
    meta_tmp.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(meta_tmp, meta_path)
    return item_id


def remove_queued_audio(item_id: Optional[str]) -> None:
    if not item_id:
        return
    pending = _pending_dir()
    for suffix in (".json", ".wav"):
        try:
            (pending / f"{item_id}{suffix}").unlink()
        except FileNotFoundError:
            pass


def load_queued_audio_metadata(item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    meta_path = _pending_dir() / f"{item_id}.json"
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def claim_queued_audio(
    *,
    worker_id: str,
    lease_seconds: int = 900,
    preferred_nodes: Optional[list[str]] = None,
    skip_ids: Optional[set[str]] = None,
) -> Optional[dict]:
    skip_ids = skip_ids or set()
    preferred_nodes = preferred_nodes or []
    pending = _pending_dir()
    now = time.time()
    candidates = []

    for meta_path in pending.glob("*.json"):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        item_id = metadata.get("id") or meta_path.stem
        if item_id in skip_ids:
            continue
        if float(metadata.get("next_attempt_ts") or 0) > now:
            continue
        lease_until = float(metadata.get("lease_until_ts") or 0)
        if lease_until > now:
            continue

        wav_file = metadata.get("wav_file") or f"{meta_path.stem}.wav"
        wav_path = pending / wav_file
        if not wav_path.exists():
            continue

        node = str(metadata.get("node") or "")
        created_ts = float(metadata.get("created_ts") or meta_path.stat().st_mtime)
        try:
            priority = preferred_nodes.index(node)
        except ValueError:
            priority = len(preferred_nodes)

        candidates.append((priority, created_ts, item_id, metadata, wav_path, meta_path))

    candidates.sort(key=lambda item: (item[0], item[1]))
    if not candidates:
        return None

    _, _, item_id, metadata, wav_path, meta_path = candidates[0]
    metadata["lease_worker_id"] = worker_id
    metadata["lease_claimed_ts"] = now
    metadata["lease_until_ts"] = now + max(60, lease_seconds)
    meta_tmp = pending / f".{item_id}.json.tmp"
    meta_tmp.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(meta_tmp, meta_path)

    item = dict(metadata)
    item["id"] = item_id
    item["wav_bytes"] = wav_path.read_bytes()
    return item


def iter_pending_raw_audio(skip_ids: Optional[set[str]] = None, limit: int = 1) -> list[dict]:
    skip_ids = skip_ids or set()
    pending = _pending_dir()
    now = time.time()
    items = []

    for meta_path in sorted(pending.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        item_id = metadata.get("id") or meta_path.stem
        if item_id in skip_ids:
            continue
        if float(metadata.get("next_attempt_ts") or 0) > now:
            continue

        wav_file = metadata.get("wav_file") or f"{meta_path.stem}.wav"
        wav_path = pending / wav_file
        try:
            wav_bytes = wav_path.read_bytes()
        except OSError:
            continue

        item = dict(metadata)
        item["id"] = item_id
        item["wav_bytes"] = wav_bytes
        items.append(item)
        if len(items) >= limit:
            break

    return items


def iter_pending_raw_audio_prioritized(
    skip_ids: Optional[set[str]] = None,
    limit: int = 1,
    preferred_nodes: Optional[list[str]] = None,
) -> list[dict]:
    skip_ids = skip_ids or set()
    preferred_nodes = preferred_nodes or []
    pending = _pending_dir()
    now = time.time()
    candidates = []

    for meta_path in pending.glob("*.json"):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        item_id = metadata.get("id") or meta_path.stem
        if item_id in skip_ids:
            continue
        if float(metadata.get("next_attempt_ts") or 0) > now:
            continue

        wav_file = metadata.get("wav_file") or f"{meta_path.stem}.wav"
        wav_path = pending / wav_file
        if not wav_path.exists():
            continue

        node = str(metadata.get("node") or "")
        created_ts = float(metadata.get("created_ts") or meta_path.stat().st_mtime)
        try:
            priority = preferred_nodes.index(node)
        except ValueError:
            priority = len(preferred_nodes)

        candidates.append((priority, created_ts, item_id, metadata, wav_path))

    candidates.sort(key=lambda item: (item[0], item[1]))

    items = []
    for _, _, item_id, metadata, wav_path in candidates[:limit]:
        try:
            wav_bytes = wav_path.read_bytes()
        except OSError:
            continue
        item = dict(metadata)
        item["id"] = item_id
        item["wav_bytes"] = wav_bytes
        items.append(item)

    return items


def mark_queued_audio_attempt(
    item_id: Optional[str],
    *,
    status: str,
    retry_delay_seconds: int = 120,
    max_attempts: int = 3,
) -> None:
    if not item_id:
        return

    pending = _pending_dir()
    meta_path = pending / f"{item_id}.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return

    attempts = int(metadata.get("attempts") or 0) + 1
    metadata["attempts"] = attempts
    metadata["last_attempt_ts"] = time.time()
    metadata["last_status"] = status
    metadata["next_attempt_ts"] = time.time() + retry_delay_seconds

    if attempts >= max_attempts:
        failed = _queue_dir("failed")
        wav_file = metadata.get("wav_file") or f"{item_id}.wav"
        meta_tmp = pending / f".{item_id}.json.tmp"
        meta_tmp.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(meta_tmp, failed / f"{item_id}.json")
        try:
            os.replace(pending / wav_file, failed / wav_file)
        except OSError:
            pass
        try:
            meta_path.unlink()
        except FileNotFoundError:
            pass
        return

    meta_tmp = pending / f".{item_id}.json.tmp"
    meta_tmp.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(meta_tmp, meta_path)


def release_queued_audio_claim(
    item_id: Optional[str],
    *,
    status: str,
    retry_delay_seconds: int = 120,
) -> None:
    if not item_id:
        return

    pending = _pending_dir()
    meta_path = pending / f"{item_id}.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return

    metadata["lease_worker_id"] = None
    metadata["lease_claimed_ts"] = None
    metadata["lease_until_ts"] = 0
    metadata["last_status"] = status
    metadata["next_attempt_ts"] = time.time() + retry_delay_seconds
    meta_tmp = pending / f".{item_id}.json.tmp"
    meta_tmp.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(meta_tmp, meta_path)


def get_raw_audio_queue_stats(now: Optional[float] = None) -> dict:
    now = now or time.time()
    pending = _pending_dir()
    failed = _queue_dir("failed")
    count = 0
    bytes_total = 0
    oldest_ts = None

    for meta_path in pending.glob("*.json"):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}

        count += 1
        created_ts = float(metadata.get("created_ts") or meta_path.stat().st_mtime)
        oldest_ts = created_ts if oldest_ts is None else min(oldest_ts, created_ts)

        wav_file = metadata.get("wav_file") or f"{meta_path.stem}.wav"
        try:
            bytes_total += (pending / wav_file).stat().st_size
        except OSError:
            pass

    return {
        "pending": count,
        "bytes": bytes_total,
        "oldest_age_seconds": max(0.0, now - oldest_ts) if oldest_ts else 0.0,
        "failed": len(list(failed.glob("*.json"))),
    }
