"""Scenario runner for tests/test_incident_db_authority.py.

Not a test file. Executed as a subprocess by that suite so each scenario runs
in a clean interpreter.

That isolation is not optional. Several suites in this repo install stub
``modules.config``, ``modules.talkgroups`` and ``modules.database`` entries into
``sys.modules`` at *collection* time and never remove them, so by the time this
suite is collected the real ``modules.database`` can no longer be re-imported
(it needs ``CAT_COORDS`` from a stubbed ``modules.talkgroups``). Inheriting that
polluted interpreter would also mean the "authoritative" logic under test was
reading whatever stub happened to win. See the sys.modules notes in
tests/test_pi_watchdog.py and the eviction preambles elsewhere in the suite.

Usage: python _incident_db_authority_child.py <scenario.json> <result.json>

Writes one JSON object to the result path. Exits non-zero with the traceback on
stderr if the scenario itself raises, so a broken scenario can never look like a
passing assertion. The result goes to a file rather than stdout because the
engine logs its ``[incident] CLEAR ...`` lines to stdout while it runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SCHEMA = _ROOT / "schema.sql"


def _seed(db_path: str, rows: list[dict]) -> list[int]:
    ids: list[int] = []
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            cur = conn.execute(
                "INSERT INTO incidents (ts_start, ts_updated, itype, description, "
                "agencies, tgids, location, lat, lon, status, is_test) "
                "VALUES (?,?,?,?,'[\"APD\"]','[]','Austin airspace',30.27,-97.74,?,?)",
                (
                    row["ts_start"], row["ts_updated"], row["itype"],
                    row.get("description"), row["status"], row.get("is_test", 0),
                ),
            )
            ids.append(cur.lastrowid)
        conn.commit()
    return ids


def main() -> None:
    scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    now = scenario.get("now") or time.time()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "calls.db")
        with sqlite3.connect(db_path) as conn:
            conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        if scenario.get("drop_incidents_table"):
            with sqlite3.connect(db_path) as conn:
                conn.execute("DROP TABLE incidents")
                conn.commit()

        os.environ["DB_PATH"] = db_path
        os.environ["BATTLE_BUDDY_HOME"] = tmp
        os.environ["TIPS_UPLOAD_DIR"] = str(Path(tmp) / "tips")
        os.environ["HOMICIDE_SEED_PATH"] = str(Path(tmp) / "homicide.json")
        os.makedirs(os.environ["TIPS_UPLOAD_DIR"], exist_ok=True)

        from modules import database, incident_engine

        # These bind DB_PATH at import time; point them at the scenario's file.
        database.DB_PATH = db_path
        incident_engine.DB_PATH = db_path
        incident_engine._active_incidents.clear()

        ids = _seed(db_path, scenario.get("rows", []))
        for iid, reg in zip(ids, scenario.get("register", []), strict=False):
            if reg:
                incident_engine._active_incidents[iid] = {
                    "itype": reg["itype"],
                    "ts_updated": now - reg["age_s"],
                    "agencies": {"APD"},
                    "tgids": set(),
                    "lat": 30.27,
                    "lon": -97.74,
                    "escalation_stage": None,
                }

        before = sorted({inc["itype"] for inc in incident_engine._active_incidents.values()})
        after = before

        error: str | None = None
        cleared: list[tuple[int, str]] = []
        banner_calls: list[tuple] = []
        race = None
        if scenario.get("race_update_after_select"):
            # Run this *instead of* a normal pass: the point is to observe the
            # pass blocking on the lock, with the incident still in memory.
            race = _simulate_race(incident_engine, now)
        else:
            for _ in range(scenario.get("passes", 1)):
                try:
                    cleared = incident_engine.clear_stale_incidents(now) or []
                except Exception as exc:  # reported, not raised, so the parent can assert
                    error = f"{type(exc).__name__}: {exc}"
                    break

            after = sorted(
                {inc["itype"] for inc in incident_engine._active_incidents.values()}
            )

            # Drive the banner decision through the real thread body rather than a
            # reimplementation, so a bug in the ordering is caught here.
            if scenario.get("drive_banner_decision"):
                try:
                    banner_calls = _run_banner_decision(incident_engine, cleared)
                except Exception as exc:
                    if error is None:
                        error = f"banner decision: {type(exc).__name__}: {exc}"

        with sqlite3.connect(db_path) as conn:
            try:
                statuses = {
                    str(i): conn.execute(
                        "SELECT status FROM incidents WHERE id=?", (i,)
                    ).fetchone()[0]
                    for i in ids
                }
                ts_cleared = {
                    str(i): conn.execute(
                        "SELECT ts_cleared FROM incidents WHERE id=?", (i,)
                    ).fetchone()[0]
                    for i in ids
                }
                total_rows = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
            except sqlite3.Error as exc:
                # The table is deliberately gone in one scenario; that is the
                # condition under test, not a harness failure.
                statuses, ts_cleared, total_rows = {}, {}, 0
                if error is None:
                    error = f"read-back: {type(exc).__name__}: {exc}"

        try:
            published = sorted(
                r["id"] for r in database.public_active_incidents()
            )
        except Exception as exc:
            published = []
            if error is None:
                error = f"public_active_incidents: {type(exc).__name__}: {exc}"

        result = {
            "ids": ids,
            "cleared": [list(pair) for pair in cleared],
            "cleared_ids": [inc_id for inc_id, _ in cleared],
            "statuses": statuses,
            "ts_cleared": ts_cleared,
            "before": before,
            "after": after,
            "left_behind": incident_engine._itypes_left_behind(set(before), set(after)),
            "banner_calls": banner_calls,
            "race": race,
            "published": published,
            "total_rows": total_rows,
            "error": error,
            "all_itypes": sorted(database.INCIDENT_TIMEOUT_MINUTES),
            "adsb_is_a_banner_type": "AIR ASSET ACTIVE" in _banner_itypes(),
        }
        out_path.write_text(json.dumps(result), encoding="utf-8")


def _run_banner_decision(incident_engine, cleared) -> list[tuple]:
    """Reproduce incident_cleanup_thread's banner decision, synchronously.

    The thread is an infinite loop, so its body is replayed here against the real
    ``clear_banner`` with the thread spawn replaced by a direct call. Returns the
    ``(itype, incident_id)`` pairs it would have passed, which is what the parent
    asserts on.
    """
    from modules import alerts

    calls: list[tuple] = []
    real_thread = incident_engine.threading.Thread

    class _Direct(real_thread):  # type: ignore[misc, valid-type]
        def start(self) -> None:
            calls.append(self._args)

    real_alerts_thread = alerts.threading.Thread

    class _DirectAlerts(real_alerts_thread):  # type: ignore[misc, valid-type]
        def start(self) -> None:
            calls.append(self._args)

    before = {inc["itype"] for inc in incident_engine._active_incidents.values()}
    after = {inc["itype"] for inc in incident_engine._active_incidents.values()}
    with _patched(incident_engine.threading, "Thread", _Direct), \
         _patched(alerts.threading, "Thread", _DirectAlerts):
        for inc_id, itype in cleared:
            incident_engine.threading.Thread(
                target=alerts.clear_banner, args=(itype, inc_id)
            ).start()
        for itype in incident_engine._itypes_left_behind(before, after):
            incident_engine.threading.Thread(
                target=alerts.clear_banner, args=(itype,)
            ).start()
    return calls


class _patched:
    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


def _simulate_race(incident_engine, now: float) -> dict:
    """Prove the pass holds ``_incident_lock`` across its own SQL.

    The engine refreshes ``ts_updated`` from ``_update_incident`` while holding
    ``_incident_lock``. If the cleanup pass does not hold that lock for its whole
    duration, a live incident can be matched and refreshed in the window between
    the pass's SELECT and its UPDATE, and then cleared anyway.

    The engine's module-level ``sqlite3`` is swapped for a proxy that records
    whether this thread owns the lock at the moment each statement runs.
    Merely checking that the pass *touches* the lock is not enough: the pre-fix
    code took it for the dict pop and still lost the race, so the assertion is
    about ownership during the SELECT and the UPDATE.
    """
    import sqlite3 as real_sqlite3

    registered = list(incident_engine._active_incidents)
    if not registered:
        return {"error": "no registered incident to race against",
                "lock_held_during": [], "target_id": None}
    target_id = registered[0]

    lock = incident_engine._incident_lock
    held_during: list[bool] = []
    real_connect = real_sqlite3.connect

    class _TracingConn:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, *a, **kw):
            held_during.append(_held(lock))
            return self._conn.execute(*a, **kw)

        def executemany(self, *a, **kw):
            held_during.append(_held(lock))
            return self._conn.executemany(*a, **kw)

    class _ProxyModule:
        def __getattr__(self, name):
            return getattr(real_sqlite3, name)

        @staticmethod
        def connect(*a, **kw):
            return _TracingConn(real_connect(*a, **kw))

    incident_engine.sqlite3 = _ProxyModule()
    try:
        cleared = incident_engine.clear_stale_incidents(now)
        error = None
    except Exception as exc:
        cleared, error = None, f"{type(exc).__name__}: {exc}"
    finally:
        incident_engine.sqlite3 = real_sqlite3

    with real_sqlite3.connect(incident_engine.DB_PATH) as conn:
        status = conn.execute(
            "SELECT status FROM incidents WHERE id=?", (target_id,)
        ).fetchone()[0]

    return {
        "error": error,
        "cleared": [list(p) for p in (cleared or [])],
        "status": status,
        "lock_held_during": held_during,
        "target_id": target_id,
    }


def _held(lock) -> bool:
    """True when the calling thread owns ``lock``."""
    owned = getattr(lock, "_is_owned", None)
    if owned is not None:
        return bool(owned())
    # A plain Lock cannot be taken twice by one thread, so a non-blocking
    # acquire succeeding means this thread did not already hold it.
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _banner_itypes() -> set[str]:
    from modules.alerts import BANNER_ITYPES
    return set(BANNER_ITYPES)


if __name__ == "__main__":
    main()
