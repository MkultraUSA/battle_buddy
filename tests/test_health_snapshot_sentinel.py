"""A -1 from the health snapshot must mean one thing only: that metric failed.

`get_health_snapshot()` reported `active_incidents: -1`, `calls_24h: -1` and
`total_calls: -1` on a perfectly healthy database. The cause was one query
-- `WHERE status=active`, which is not valid SQLite because the literal needs
quotes -- inside a `try` that wrapped all four counters. One bad statement
discarded three good numbers.

The damage is not the wrong value. It is that the sentinel became ambiguous: a
reader could not tell a healthy count from a failed query, so a snapshot that
looked alarming turned out to be cosmetic, and a real failure would have looked
identical. `-1` now means only "this one metric could not be read".

These tests pin that, using a real sqlite file rather than a mock, because the
defect was a SQL error and a mock would not have caught it.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_db(path: Path, *, with_incidents: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE calls (ts REAL, category TEXT)")
    # Real epoch seconds, not a round number: the 24h window compares against
    # time.time(), so ts=1e9 (2001) would fall outside it and test a different
    # code path than the one being pinned.
    now = time.time()
    for i in range(5):
        conn.execute("INSERT INTO calls (ts, category) VALUES (?, ?)", (now, f"c{i}"))
    # ~1.5 MB of payload, so size_mb rounds to something clearly non-zero and
    # "size was reported" cannot be confused with "size read as 0".
    conn.execute("CREATE TABLE ballast (blob TEXT)")
    conn.executemany(
        "INSERT INTO ballast VALUES (?)", [("x" * 300_000,) for _ in range(5)]
    )
    if with_incidents:
        conn.execute("CREATE TABLE incidents (status TEXT, itype TEXT)")
        conn.execute("INSERT INTO incidents VALUES ('active', 'FIRE DISPATCH')")
        conn.execute("INSERT INTO incidents VALUES ('cleared', 'SHOOTING')")
    conn.commit()
    conn.close()


def _snapshot(db_path: Path) -> dict:
    from modules.maintenance import get_health_snapshot

    return get_health_snapshot(str(db_path))


def test_counts_are_real_numbers_not_sentinels(tmp_path):
    """The reported case: healthy DB, all three counters came back -1."""
    db = tmp_path / "calls.db"
    _make_db(db)

    snap = _snapshot(db)
    db_stats = snap["db"]

    assert db_stats["total_calls"] == 5, (
        f"total_calls was {db_stats['total_calls']}; -1 here means the query "
        "failed, not that the table is empty"
    )
    assert db_stats["active_incidents"] == 1
    assert db_stats["size_mb"] > 0


def test_status_literal_is_quoted(tmp_path):
    """`status=active` is not valid SQLite. This is the exact regression.

    Unquoted, sqlite treats `active` as a column name and raises
    "no such column". With the quotes it is a string literal.
    """
    db = tmp_path / "calls.db"
    _make_db(db)

    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        conn.execute("SELECT COUNT(*) FROM incidents WHERE status=active").fetchone()
    assert conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE status = 'active'"
    ).fetchone()[0] == 1
    conn.close()

    assert _snapshot(db)["db"]["active_incidents"] == 1


def test_one_failing_metric_does_not_zero_the_others(tmp_path):
    """A failure in one query must not take the healthy ones down with it.

    This is the property the old code lacked: the counters shared an `except`,
    so one bad statement silently blanked all of them.
    """
    db = tmp_path / "calls.db"
    _make_db(db, with_incidents=False)  # no incidents table at all

    db_stats = _snapshot(db)["db"]

    assert db_stats["total_calls"] == 5, "calls table is fine and must still report"
    assert db_stats["calls_24h"] == 5, "calls table is fine and must still report"
    assert db_stats["active_incidents"] == -1, (
        "only the incidents metric failed, so only it may be -1"
    )


def test_missing_database_file_reads_as_failure_not_zero(tmp_path):
    """A genuinely absent database must read as failure, not as a healthy zero.

    Note `size_mb` is 0.0 rather than -1: `sqlite3.connect()` creates the file
    if it does not exist, so by the time getsize runs there is an empty
    database on disk. The counts still correctly report -1 because the tables
    are not there. Pinned as-is so the behaviour is recorded rather than
    assumed -- a health check that creates the database it is checking is odd,
    but it is not what this change set out to fix, and changing it would widen
    the diff beyond the sentinel bug.
    """
    missing = tmp_path / "does-not-exist.db"
    assert not missing.exists()

    snap = _snapshot(missing)["db"]

    assert snap["total_calls"] == -1
    assert snap["calls_24h"] == -1
    assert snap["active_incidents"] == -1
    assert snap["size_mb"] == 0.0


def test_size_is_rounded_not_raw_float(tmp_path):
    db = tmp_path / "calls.db"
    _make_db(db)

    size = _snapshot(db)["db"]["size_mb"]

    assert isinstance(size, float)
    assert round(size, 1) == size, "size_mb should be reported to one decimal"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))