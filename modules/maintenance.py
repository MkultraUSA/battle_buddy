"""
Battle Buddy Maintenance Module — Phase 3
- KG memory pruning (cap call nodes, retain incidents/agencies/talkgroups)
- Database archiving (calls older than 90 days → calls_archive.db)
- Health/observability endpoint
"""

import gc
import os
import sqlite3
import threading
import time

from modules.kg_ontology import BattleBuddyKG

# ---------------------------------------------------------------------------
# KG Pruning
# ---------------------------------------------------------------------------

# Keep only this many days of Call nodes in the in-memory graph.
# Incident, Agency, and Talkgroup nodes are always retained.
_KG_CALL_RETENTION_DAYS = 7
_KG_PRUNE_INTERVAL = 3600  # prune every hour

_kg_instance: BattleBuddyKG = None
_kg_prune_lock = threading.Lock()


def _get_kg() -> BattleBuddyKG:
    """Get the singleton KG instance (must be set by caller after init)."""
    global _kg_instance
    return _kg_instance


def set_kg_instance(kg: BattleBuddyKG) -> None:
    """Register the singleton KG instance for pruning."""
    global _kg_instance
    _kg_instance = kg


def prune_kg_calls(kg: BattleBuddyKG = None, retention_days: int = None) -> int:
    """Remove Call nodes older than retention_days from the in-memory graph.

    Incident, Agency, and Talkgroup nodes are never pruned.
    Call nodes are removed from the NetworkX graph but retained in SQLite.
    Returns number of nodes removed.
    """
    if kg is None:
        kg = _get_kg()
    if kg is None:
        return 0

    days = retention_days or _KG_CALL_RETENTION_DAYS
    cutoff = time.time() - (days * 86400)
    removed = 0

    with _kg_prune_lock:
        call_nodes = [
            nid for nid, data in kg.G.nodes(data=True)
            if data.get("label") == "Call"
            and data.get("created_at", 0) < cutoff
        ]
        if call_nodes:
            kg.G.remove_nodes_from(call_nodes)
            removed = len(call_nodes)

    if removed:
        print(f"[kg-prune] Removed {removed} stale Call nodes from in-memory graph "
              f"(retention={days}d, remaining={kg.G.number_of_nodes()} nodes)",
              flush=True)

    return removed


def _kg_prune_loop() -> None:
    """Background daemon that periodically prunes old Call nodes from the KG."""
    while True:
        try:
            time.sleep(_KG_PRUNE_INTERVAL)
            kg = _get_kg()
            if kg:
                prune_kg_calls(kg)
                gc.collect()  # encourage Python to release the freed memory
        except Exception as e:
            print(f"[kg-prune] error: {e}", flush=True)
            time.sleep(300)


# ---------------------------------------------------------------------------
# Database Archiving
# ---------------------------------------------------------------------------

_ARCHIVE_RETENTION_DAYS = 90
_ARCHIVE_INTERVAL = 86400  # daily


def archive_old_calls(db_path: str, archive_path: str = None,
                      retention_days: int = None) -> tuple[int, int]:
    """Move calls older than retention_days from db_path to archive_path.

    Returns (archived_count, remaining_count).
    """
    days = retention_days or _ARCHIVE_RETENTION_DAYS
    if archive_path is None:
        archive_path = db_path.replace(".db", "_archive.db")

    cutoff = time.time() - (days * 86400)

    # Create archive DB if needed
    aconn = sqlite3.connect(archive_path, timeout=5.0)
    aconn.execute("PRAGMA journal_mode=WAL")
    # Mirror the calls table schema
    aconn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id          INTEGER PRIMARY KEY,
            ts          REAL    NOT NULL,
            tgid        INTEGER,
            tag         TEXT,
            category    TEXT,
            node        TEXT,
            duration    REAL,
            transcript  TEXT,
            lat         REAL,
            lon         REAL,
            location    TEXT,
            coords_approx INTEGER DEFAULT 0,
            accuracy    REAL
        )
    """)
    aconn.execute("CREATE INDEX IF NOT EXISTS idx_archive_calls_ts ON calls(ts)")
    aconn.commit()

    # Count calls to archive
    mconn = sqlite3.connect(db_path, timeout=5.0)
    mconn.execute("PRAGMA journal_mode=WAL")
    count = mconn.execute(
        "SELECT COUNT(*) FROM calls WHERE ts < ?", (cutoff,)
    ).fetchone()[0]

    if count == 0:
        mconn.close()
        aconn.close()
        return 0, 0

    print(f"[archive] Moving {count} calls older than {days}d to archive...", flush=True)
    start = time.time()

    # Copy in batches to avoid long locks
    batch_size = 5000
    copied = 0
    offset_id = 0

    while True:
        rows = mconn.execute(
            "SELECT * FROM calls WHERE ts < ? AND id > ? ORDER BY id LIMIT ?",
            (cutoff, offset_id, batch_size)
        ).fetchall()

        if not rows:
            break

        aconn.executemany(
            """INSERT OR IGNORE INTO calls
               (id, ts, tgid, tag, category, node, duration, transcript,
                lat, lon, location, coords_approx, accuracy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows
        )
        aconn.commit()

        # Delete from main DB in same batch
        ids = [r[0] for r in rows]
        mconn.execute(
            f"DELETE FROM calls WHERE id IN ({','.join(['?']*len(ids))})",
            ids
        )
        mconn.commit()

        copied += len(rows)
        offset_id = rows[-1][0]
        if len(rows) < batch_size:
            break

    # Clean up
    mconn.execute("PRAGMA optimize")
    mconn.commit()
    mconn.close()

    aconn.execute("PRAGMA optimize")
    aconn.commit()
    aconn.close()

    elapsed = time.time() - start
    print(f"[archive] Done — moved {copied} calls in {elapsed:.1f}s", flush=True)

    return copied, 0


def _archive_loop(db_path: str) -> None:
    """Background daemon that periodically archives old calls."""
    while True:
        try:
            time.sleep(_ARCHIVE_INTERVAL)
            archive_old_calls(db_path)
            # VACUUM to reclaim space after deletion
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.close()
            print(f"[archive] VACUUM complete on {db_path}", flush=True)
        except Exception as e:
            print(f"[archive] error: {e}", flush=True)
            time.sleep(3600)


# ---------------------------------------------------------------------------
# Health / Observability Endpoint
# ---------------------------------------------------------------------------

def get_health_snapshot(db_path: str) -> dict:
    """Return a health snapshot for monitoring (Prometheus-compatible)."""
    import sys

    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        total_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        calls_24h = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE ts > ?",
            (time.time() - 86400,)
        ).fetchone()[0]
        active_incidents = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE status=active"
        ).fetchone()[0]
        db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
        conn.close()
    except Exception:
        total_calls = -1
        calls_24h = -1
        active_incidents = -1
        db_size_mb = -1

    kg = _get_kg()
    kg_nodes = kg.G.number_of_nodes() if kg else -1
    kg_edges = kg.G.number_of_edges() if kg else -1

    # Memory
    try:
        import psutil
        proc = psutil.Process()
        mem_mb = proc.memory_info().rss / (1024 * 1024)
    except ImportError:
        mem_mb = -1

    return {
        "status": "ok",
        "uptime_seconds": time.time() - psutil.boot_time() if "psutil" in sys.modules else -1,
        "memory_mb": round(mem_mb, 1),
        "threads": threading.active_count(),
        "db": {
            "path": db_path,
            "size_mb": round(db_size_mb, 1),
            "total_calls": total_calls,
            "calls_24h": calls_24h,
            "active_incidents": active_incidents,
        },
        "kg": {
            "nodes": kg_nodes,
            "edges": kg_edges,
        },
        "server_time": time.time(),
    }
