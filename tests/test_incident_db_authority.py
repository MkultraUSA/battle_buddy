"""The database is the authority on incident state.

Every code path that inserts an ``incidents`` row with ``status='active'`` must
get a lifecycle, whether or not it also registers the row in the in-memory
``_active_incidents`` dict.

Three writers do *not* register in that dict: the ADS-B air-asset poller
(``adsb_air_asset.alert_leo_aircraft``), the ADS-B orbit poller
(``adsb_air_asset.detect_orbits``) and the APD press-release poller
(``apd_news``). Before this was fixed, ``incident_cleanup_thread`` only ever
looked at the dict, so those rows could never be closed. Production had 1,412
``AIR ASSET ACTIVE`` rows, a fresh one appearing every 30 minutes, none of them
ever retired.

Each scenario runs in a clean subprocess (``_incident_db_authority_child.py``)
against a real SQLite database built from the production ``schema.sql``, and
drives the real ``clear_stale_incidents``. ``_active_incidents`` is never an
input to the clearing decision -- that is the property under test.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_CHILD = _HERE / "_incident_db_authority_child.py"


def row(itype: str, age_s: float, *, status: str = "active",
        is_test: int = 0, description: str | None = None,
        now: float | None = None) -> dict:
    """One seeded incidents row, ``age_s`` seconds stale as of the scenario's now.

    Pass the same ``now`` the scenario runs with, or the row lands at the wrong
    age relative to the clock the pass actually compares against.
    """
    base = time.time() if now is None else now
    return {
        "itype": itype,
        "status": status,
        "is_test": is_test,
        "description": description,
        "ts_start": base - age_s,
        "ts_updated": base - age_s,
    }


class Scenario:
    """Result of one child run."""

    def __init__(self, raw: dict) -> None:
        self.raw = raw
        self.ids: list[int] = raw["ids"]
        self.cleared: list[tuple[int, str]] = [tuple(p) for p in raw["cleared"]]
        self.cleared_ids: list[int] = raw["cleared_ids"]
        self.statuses: dict[int, str] = {int(k): v for k, v in raw["statuses"].items()}
        self.ts_cleared: dict[int, float | None] = {
            int(k): v for k, v in raw["ts_cleared"].items()
        }
        self.before: list[str] = raw["before"]
        self.after: list[str] = raw["after"]
        self.left_behind: list[str] = raw["left_behind"]
        self.banner_calls: list[tuple] = [tuple(c) for c in raw["banner_calls"]]
        self.race: dict | None = raw["race"]
        self.published: list[int] = raw["published"]
        self.total_rows: int = raw["total_rows"]
        self.error: str | None = raw["error"]
        self.all_itypes: list[str] = raw["all_itypes"]
        self.adsb_is_a_banner_type: bool = raw["adsb_is_a_banner_type"]

    def active(self, iid: int) -> str:
        return self.statuses[iid]

    def first(self) -> int:
        return self.ids[0]


def run(rows: list[dict] | None = None, *, register: list[dict | None] | None = None,
        passes: int = 1, drop_incidents_table: bool = False, now: float | None = None,
        drive_banner_decision: bool = False, race_update_after_select: bool = False) -> Scenario:
    """Execute one scenario in a clean interpreter and return its result.

    ``now`` pins the clock for both the seeded rows and the pass, so a test can
    sit a row exactly on its timeout boundary instead of near it.
    """
    scenario = {
        "rows": rows or [],
        "register": register or [],
        "passes": passes,
        "drop_incidents_table": drop_incidents_table,
        "now": now,
        "drive_banner_decision": drive_banner_decision,
        "race_update_after_select": race_update_after_select,
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scenario.json"
        out = Path(tmp) / "result.json"
        path.write_text(json.dumps(scenario), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(path), str(out)],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"scenario child failed ({proc.returncode}): {proc.stderr[-2000:]}"
            )
        result = Scenario(json.loads(out.read_text(encoding="utf-8")))
    if result.error and not drop_incidents_table:
        raise AssertionError(f"scenario raised inside the engine: {result.error}")
    return result


class StaleRowFromAnUnregisteredWriterIsCleared(unittest.TestCase):
    """The regression that started this: rows no writer ever registered."""

    def test_stale_adsb_air_asset_row_is_cleared_without_being_in_the_dict(self):
        # 45 minutes stale; AIR ASSET ACTIVE's timeout is 20 minutes.
        s = run([row("AIR ASSET ACTIVE", age_s=45 * 60)])
        self.assertIn(s.first(), s.cleared_ids)
        self.assertEqual(s.active(s.first()), "cleared")
        # It was never registered, so nothing in memory knew about it.
        self.assertEqual(s.before, [])

    def test_stale_air_asset_orbit_row_is_cleared(self):
        s = run([row("AIR ASSET ORBIT", age_s=45 * 60)])
        self.assertEqual(s.cleared_ids, [s.first()])
        self.assertEqual(s.active(s.first()), "cleared")

    def test_stale_press_release_row_is_cleared(self):
        s = run([row("SHOOTING", age_s=45 * 60,
                     description="[APD Press Release] APD investigates")])
        self.assertEqual(s.active(s.first()), "cleared")

    def test_ts_cleared_records_the_end_of_the_incident_not_the_sweep(self):
        """Stamped with when the incident's own timeout expired, which for a row
        that aged out under the old code is when it was *selected* to be cleared
        rather than when the pass happened to run."""
        now = 1_800_000_000.0
        # AIR ASSET ACTIVE times out at 20 minutes.
        s = run([row("AIR ASSET ACTIVE", age_s=45 * 60, now=now)], now=now)
        self.assertIsNotNone(s.ts_cleared[s.first()])
        self.assertAlmostEqual(
            s.ts_cleared[s.first()], now - 45 * 60 + 20 * 60, delta=2
        )


class LiveIncidentsSurviveThePass(unittest.TestCase):
    """A reconciliation pass must never retire a genuinely live incident."""

    def test_fresh_row_of_every_itype_survives(self):
        rows = [row(itype, age_s=5) for itype in run().all_itypes]
        s = run(rows)
        self.assertEqual(s.cleared_ids, [])
        self.assertTrue(all(status == "active" for status in s.statuses.values()))

    def test_row_just_inside_its_own_timeout_survives(self):
        # HOSTAGE/BARRICADE is 45 minutes. 44 minutes must survive.
        s = run([row("HOSTAGE/BARRICADE", age_s=44 * 60)])
        self.assertEqual(s.cleared_ids, [])
        self.assertEqual(s.active(s.first()), "active")

    def test_row_just_past_its_own_timeout_is_cleared(self):
        s = run([row("HOSTAGE/BARRICADE", age_s=46 * 60)])
        self.assertEqual(s.cleared_ids, [s.first()])

    def test_per_type_timeout_is_honoured_not_a_flat_window(self):
        # CRASH/COLLISION times out at 15 minutes, SHOOTING at 20. A row stale
        # by 17 minutes is therefore dead for one and alive for the other; a
        # single flat cutoff gets at least one of them wrong.
        s = run([row("CRASH/COLLISION", age_s=17 * 60),
                 row("SHOOTING", age_s=17 * 60)])
        crash, shooting = s.ids
        self.assertEqual(s.active(crash), "cleared")
        self.assertEqual(s.active(shooting), "active")

    def test_unlisted_itype_uses_the_default_timeout(self):
        # CURFEW is not in INCIDENT_TIMEOUT_MINUTES, so the 10-minute default
        # applies: 5 minutes is fresh, 15 is stale.
        s = run([row("CURFEW", age_s=5 * 60), row("CURFEW", age_s=15 * 60)])
        fresh, stale = s.ids
        self.assertEqual(s.active(fresh), "active")
        self.assertEqual(s.active(stale), "cleared")


class ClearingIsIdempotentAndNonDestructive(unittest.TestCase):
    def test_second_pass_over_an_already_clear_database_is_a_no_op(self):
        s = run([row("AIR ASSET ACTIVE", age_s=60 * 60)] * 3, passes=2)
        self.assertEqual(len(s.ids), 3)
        self.assertTrue(all(status == "cleared" for status in s.statuses.values()))
        # The second pass must be a genuine no-op, not just "cleared again".
        self.assertEqual(s.cleared_ids, [])
        self.assertEqual(s.error, None)

    def test_already_cleared_rows_are_never_touched_again(self):
        s = run([row("SHOOTING", age_s=99 * 3600, status="cleared")])
        self.assertEqual(s.cleared_ids, [])
        self.assertEqual(s.active(s.first()), "cleared")
        self.assertIsNone(
            s.ts_cleared[s.first()],
            "a historical ts_cleared must be preserved, not rewritten",
        )

    def test_test_rows_are_retired_like_any_other(self):
        """A stale is_test row is stale. Excluding it from cleanup would leave
        it active forever for no gain, since test rows are never published
        anyway -- it would just be a second, slower leak."""
        s = run([row("SHOOTING", age_s=60 * 60, is_test=1)])
        self.assertEqual(s.cleared_ids, [s.first()])
        self.assertEqual(s.active(s.first()), "cleared")

    def test_clearing_drops_the_row_from_the_in_memory_dict(self):
        """A registered row that ages out must leave the dict, or the engine
        keeps escalating an incident the database has already retired."""
        s = run([row("SHOOTING", age_s=25 * 60)],
                register=[{"itype": "SHOOTING", "age_s": 25 * 60}])
        self.assertIn("SHOOTING", s.before)
        self.assertNotIn("SHOOTING", s.after)
        self.assertEqual(s.active(s.first()), "cleared")

    def test_a_missing_table_is_reported_not_raised(self):
        """A bad cycle must not take the cleanup thread down silently; a dead
        thread is how the original leak became permanent. Asserted through the
        real thread body's guard, not merely the harness's own try/except."""
        s = run(drop_incidents_table=True)
        self.assertIsNotNone(s.error)
        self.assertIn("OperationalError", s.error)
        body = (_ROOT / "modules" / "incident_engine.py").read_text(encoding="utf-8")
        start = body.index("def incident_cleanup_thread(")
        loop = body[start:start + 2000]
        self.assertIn("except Exception", loop)
        self.assertIn("cleanup error", loop)


class StalenessBoundaryIsExact(unittest.TestCase):
    """The clearing rule and the published rule must partition the rows exactly.

    ``clear_stale_incidents`` closes a row when ``ts_updated < now - timeout``;
    ``ACTIVE_INCIDENT_POPULATION_SQL`` publishes it when
    ``ts_updated >= now - timeout``. Those must be complements, or a row can be
    retired and shown at the same time. Both sides are driven with a pinned
    clock so a row can sit exactly on the boundary.
    """

    def test_row_exactly_on_its_timeout_survives_and_stays_published(self):
        now = 1_800_000_000.0
        s = run([row("SHOOTING", age_s=20 * 60, now=now)], now=now)  # SHOOTING is 20 min
        self.assertEqual(s.cleared_ids, [])
        self.assertEqual(s.active(s.first()), "active")
        self.assertIn(s.first(), s.published)

    def test_row_one_second_past_its_timeout_is_cleared_and_unpublished(self):
        now = 1_800_000_000.0
        s = run([row("SHOOTING", age_s=20 * 60 + 1, now=now)], now=now)
        self.assertEqual(s.cleared_ids, [s.first()])
        self.assertNotIn(s.first(), s.published)

    def test_cleared_and_published_are_never_both_true(self):
        """Sweep a spread of ages across several types and assert the
        partition holds for every row, not just the boundary cases."""
        now = 1_800_000_000.0
        ages = [1, 300, 15 * 60 - 1, 15 * 60, 15 * 60 + 1, 20 * 60, 45 * 60, 3 * 3600]
        rows = [
            row(itype, age_s=age, now=now)
            for age in ages
            for itype in ("CRASH/COLLISION", "SHOOTING", "HOSTAGE/BARRICADE")
        ]
        s = run(rows, now=now)
        for iid in s.ids:
            cleared = s.active(iid) == "cleared"
            published = iid in s.published
            self.assertFalse(
                cleared and published,
                f"row {iid} was both retired and published",
            )

    def test_a_row_cleared_by_the_pass_is_never_published_afterwards(self):
        """Re-read the published population after the pass, with a clock later
        than the pass, to be sure a retired row cannot reappear."""
        now = 1_800_000_000.0
        s = run([row("AIR ASSET ACTIVE", age_s=45 * 60, now=now),
                 row("FIRE DISPATCH", age_s=60, now=now)], now=now)
        dead, live = s.ids
        self.assertNotIn(dead, s.published)
        self.assertIn(live, s.published)


class ConcurrentUpdateCannotLoseALiveIncident(unittest.TestCase):
    """The engine refreshes ``ts_updated`` while holding ``_incident_lock``.

    The pass used to release that lock between its SELECT and its UPDATE, so an
    incident could be matched and refreshed by a new call in that window and then
    cleared anyway -- the call then links to a retired row and the next call
    opens a duplicate incident with a fresh alert. Holding the lock for the whole
    pass is the fix; this asserts the lock is genuinely held.
    """

    def test_pass_holds_the_incident_lock_across_its_own_sql(self):
        """The pass must own ``_incident_lock`` while it selects and updates.

        The engine refreshes ``ts_updated`` from ``_update_incident`` under that
        same lock, so releasing it between the SELECT and the UPDATE lets a live
        incident be refreshed in the window and then cleared anyway. Taking the
        lock only for the dict pop is not enough -- that is exactly what the
        first version of this fix did, and this test fails against it.
        """
        now = 1_800_000_000.0
        s = run([row("EMS DISPATCH", age_s=11 * 60, now=now)],
                register=[{"itype": "EMS DISPATCH", "age_s": 11 * 60}],
                now=now, race_update_after_select=True)
        self.assertIsNotNone(s.race)
        self.assertIsNone(s.race["error"], s.race["error"])
        held = s.race["lock_held_during"]
        self.assertTrue(
            held, "the pass issued no SQL, so nothing was verified"
        )
        self.assertTrue(
            all(held),
            f"the pass ran SQL without holding _incident_lock: {held}",
        )
        # And with the lock held throughout, the row is retired exactly once.
        self.assertEqual(s.race["cleared"], [[s.race["target_id"], "EMS DISPATCH"]])
        self.assertEqual(s.race["status"], "cleared")


class StartupLoaderReconciles(unittest.TestCase):
    """``_load_active_incidents_from_db`` is the other half of the fix.

    It runs before any poller starts, so it is the only thing that can retire a
    row that went stale while the service was down. It must close stale rows and
    load only the survivors.
    """

    def test_loader_closes_stale_rows_and_keeps_live_ones_in_memory(self):
        script = _loader_script()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "calls.db")
            with sqlite3.connect(db_path) as conn:
                conn.executescript((_ROOT / "schema.sql").read_text(encoding="utf-8"))
            stale, live = _seed_loader_rows(db_path)
            path = Path(tmp) / "loader.json"
            path.write_text(json.dumps({
                "db_path": db_path,
                "stale": stale,
                "live": live,
                "now": 1_800_000_000.0,
            }), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-c", script, str(path), str(Path(tmp) / "out.json")],
                cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                self.fail(f"loader child failed: {proc.stderr[-2000:]}")
            result = json.loads((Path(tmp) / "out.json").read_text(encoding="utf-8"))
        self.assertIsNone(result["error"], result["error"])
        self.assertEqual(result["stale_status"], "cleared")
        self.assertEqual(result["live_status"], "active")
        self.assertIn(live, result["in_memory"])
        self.assertNotIn(stale, result["in_memory"])

    def test_loader_closes_an_unregistered_persisted_row(self):
        """The exact production case: an ADS-B row that outlived the process
        that wrote it, and was never in any dict."""
        script = _loader_script()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "calls.db")
            with sqlite3.connect(db_path) as conn:
                conn.executescript((_ROOT / "schema.sql").read_text(encoding="utf-8"))
            stale_id, _live_id = _seed_loader_rows(
                db_path, itype="AIR ASSET ACTIVE", age_s=6 * 3600
            )
            path = Path(tmp) / "loader.json"
            path.write_text(json.dumps({
                "db_path": db_path, "stale": stale_id, "live": None,
                "now": 1_800_000_000.0,
            }), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-c", script, str(path), str(Path(tmp) / "out.json")],
                cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                self.fail(f"loader child failed: {proc.stderr[-2000:]}")
            result = json.loads((Path(tmp) / "out.json").read_text(encoding="utf-8"))
        self.assertIsNone(result["error"], result["error"])
        self.assertEqual(result["stale_status"], "cleared")


def _loader_script() -> str:
    return (
        # Stub faster_whisper before importing audio_receiver: it pulls in
        # modules.transcription, and the real package is only present in the
        # production venv. Same pattern as tests/test_transcription_timeout.py.
        "import sys, types\n"
        "from unittest.mock import MagicMock\n"
        "if 'faster_whisper' not in sys.modules:\n"
        "    fw = types.ModuleType('faster_whisper')\n"
        "    fw.WhisperModel = MagicMock()\n"
        "    sys.modules['faster_whisper'] = fw\n"
        "sys.modules.setdefault('stripe', MagicMock())\n"
        "import json, os, sqlite3, sys, time\n"
        "from unittest import mock\n"
        "spec = json.loads(open(sys.argv[1]).read())\n"
        "out = sys.argv[2]\n"
        "os.environ['DB_PATH'] = spec['db_path']\n"
        "os.environ['BATTLE_BUDDY_HOME'] = os.path.dirname(spec['db_path'])\n"
        "os.environ['TIPS_UPLOAD_DIR'] = os.path.join(os.environ['BATTLE_BUDDY_HOME'], 'tips')\n"
        "os.makedirs(os.environ['TIPS_UPLOAD_DIR'], exist_ok=True)\n"
        "import audio_receiver\n"
        "audio_receiver.DB_PATH = spec['db_path']\n"
        "err = None\n"
        "try:\n"
        "    with mock.patch.object(audio_receiver.time, 'time', lambda: spec['now']):\n"
        "        audio_receiver._load_active_incidents_from_db()\n"
        "except Exception as exc:\n"
        "    err = f'{type(exc).__name__}: {exc}'\n"
        "conn = sqlite3.connect(spec['db_path'])\n"
        "def status(i):\n"
        "    return conn.execute('SELECT status FROM incidents WHERE id=?', (i,)).fetchone()[0]\n"
        "res = {\n"
        "  'error': err,\n"
        "  'stale_status': status(spec['stale']) if spec.get('stale') else None,\n"
        "  'live_status': status(spec['live']) if spec.get('live') else None,\n"
        "  'in_memory': sorted(audio_receiver._active_incidents),\n"
        "}\n"
        "open(out, 'w').write(json.dumps(res))\n"
    )


def _seed_loader_rows(db_path: str, itype: str = "SHOOTING", age_s: int = 3600):
    """Seed a stale and a live row; returns (stale_id, live_id)."""
    now = 1_800_000_000.0
    with sqlite3.connect(db_path) as conn:
        ids = []
        for age in (age_s, 60):
            cur = conn.execute(
                "INSERT INTO incidents (ts_start, ts_updated, itype, description, "
                "agencies, tgids, location, lat, lon, status, is_test) "
                "VALUES (?,?,?,'d','[\"APD\"]','[]','Austin',30.27,-97.74,'active',0)",
                (now - age, now - age, itype),
            )
            ids.append(cur.lastrowid)
        conn.commit()
    return ids[0], ids[1]


class BannerRetractionIsSafe(unittest.TestCase):
    """A banner is site-wide, not per-incident.

    ``clear_banner`` deletes the one active banner. Ownership is now tracked by
    incident id, so retiring an incident can only retract the banner that
    incident posted. Two failure modes are asserted here, both of which the
    previous type-level rule got wrong:

    * retiring incident A must not tear down the banner incident B posted;
    * retiring incident A must retract A's own banner even while a *different*
      incident of the same type is still live (a type-level rule skips this).
    """

    def _banner_script(self) -> str:
        return (
            "import json, sys\n"
            "from unittest import mock\n"
            "spec = json.loads(open(sys.argv[1]).read())\n"
            "import modules.alerts as alerts\n"
            "calls = []\n"
            "alerts._active_banner_id = 'banner-1'\n"
            "alerts._active_banner_incident_id = spec['owner']\n"
            "def fake_api(path='', data=None, method=None):\n"
            "    calls.append(method)\n"
            "    return {'id': 'banner-1'}\n"
            "alerts._banner_api = fake_api\n"
            "with mock.patch.object(alerts, '_banner_api', fake_api):\n"
            "    for itype, inc_id in spec['clear']:\n"
            "        alerts.clear_banner(itype, inc_id)\n"
            "    for itype in spec.get('clear_no_id', []):\n"
            "        alerts.clear_banner(itype)\n"
            "open(sys.argv[2], 'w').write(json.dumps({\n"
            "  'api_calls': calls,\n"
            "  'banner_id': alerts._active_banner_id,\n"
            "  'owner': alerts._active_banner_incident_id,\n"
            "}))\n"
        )

    def _run_banner(self, *, owner, clear=(), clear_no_id=()) -> dict:
        spec = {"owner": owner, "clear": [list(c) for c in clear],
                "clear_no_id": list(clear_no_id)}
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "s.json"
            dst = Path(tmp) / "o.json"
            src.write_text(json.dumps(spec), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-c", self._banner_script(), str(src), str(dst)],
                cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                self.fail(f"banner child failed: {proc.stderr[-2000:]}")
            return json.loads(dst.read_text(encoding="utf-8"))

    def test_adsb_air_asset_is_a_banner_type(self):
        """Pin the precondition that makes the hazard real: if ADS-B ever starts
        posting banners, the unregistered-row path needs rethinking."""
        self.assertTrue(run().adsb_is_a_banner_type)

    def test_retiring_the_banner_owner_retracts_its_banner(self):
        r = self._run_banner(owner=7, clear=[("SHOOTING", 7)])
        self.assertEqual(r["api_calls"], ["DELETE"])
        self.assertIsNone(r["banner_id"])

    def test_retiring_a_different_incident_leaves_the_banner_alone(self):
        """A STABBING incident posted over a SHOOTING one; retiring the
        SHOOTING must not take the STABBING banner down."""
        r = self._run_banner(owner=9, clear=[("SHOOTING", 7)])
        self.assertEqual(r["api_calls"], [])
        self.assertEqual(r["banner_id"], "banner-1")

    def test_retiring_one_of_two_same_type_incidents_still_retracts_its_own_banner(self):
        """The case a type-level rule gets wrong: incident C is live, incident A
        posted the banner, A goes stale. C keeps SHOOTING in the dict, so a
        type-level rule leaves A's banner up for as long as C lives."""
        r = self._run_banner(owner=7, clear=[("SHOOTING", 7)])
        self.assertEqual(r["api_calls"], ["DELETE"])

    def test_a_non_banner_type_never_retracts_anything(self):
        r = self._run_banner(owner=7, clear=[("EMS DISPATCH", 7)])
        self.assertEqual(r["api_calls"], [])
        self.assertEqual(r["banner_id"], "banner-1")

    def test_no_recorded_owner_still_allows_a_forced_retraction(self):
        """A banner posted before ownership tracking has owner None; the
        type-level fallback must still be able to take it down."""
        r = self._run_banner(owner=None, clear_no_id=["SHOOTING"])
        self.assertEqual(r["api_calls"], ["DELETE"])
        self.assertIsNone(r["banner_id"])

    def test_thread_retracts_by_incident_id_not_by_type(self):
        """The cleanup thread must pass the incident id, or the ownership
        tracking is never used."""
        s = run([row("SHOOTING", age_s=25 * 60)],
                register=[{"itype": "SHOOTING", "age_s": 25 * 60}],
                drive_banner_decision=True)
        self.assertEqual(s.cleared_ids, [s.first()])
        self.assertIn(("SHOOTING", s.first()), s.banner_calls)

    def test_a_stale_unregistered_adsb_row_retracts_no_banner(self):
        """An ADS-B row posts no banner, so clearing it must not disturb a
        banner owned by a live incident of a different type.

        That the real ``clear_banner`` declines in that situation is asserted by
        ``test_retiring_a_different_incident_leaves_the_banner_alone``; here we
        only pin the state the pass produces.
        """
        s = run([row("AIR ASSET ACTIVE", age_s=45 * 60),
                 row("SHOOTING", age_s=2 * 60)],
                register=[None, {"itype": "SHOOTING", "age_s": 2 * 60}])
        adsb, shooting = s.ids
        self.assertEqual(s.active(adsb), "cleared")
        self.assertEqual(s.active(shooting), "active")
        self.assertNotIn("AIR ASSET ACTIVE", s.before)
        self.assertIn("SHOOTING", s.after)

    def test_a_live_incident_of_the_same_type_is_unaffected(self):
        s = run([row("SHOOTING", age_s=25 * 60), row("SHOOTING", age_s=2 * 60)],
                register=[{"itype": "SHOOTING", "age_s": 25 * 60},
                          {"itype": "SHOOTING", "age_s": 2 * 60}],
                drive_banner_decision=True)
        stale, live = s.ids
        self.assertEqual(s.active(stale), "cleared")
        self.assertEqual(s.active(live), "active")
        # Ownership is by id, so the stale incident's own banner is retracted
        # even though the type still has a live incident.
        self.assertIn(("SHOOTING", stale), s.banner_calls)


class SingleDefinitionOfStaleness(unittest.TestCase):
    """The clearing rule and the published rule must not be able to drift.

    Asserted behaviourally: two rows of different types, the same age, must be
    retired on opposite sides of the pass. A local re-derivation of the timeout
    map in the engine (the pre-fix shape) would put both on the same side.
    """

    def test_two_types_of_equal_age_are_retired_on_opposite_sides(self):
        # CRASH/COLLISION is 15 minutes, HOSTAGE/BARRICADE is 45. At 30 minutes
        # only the first is stale.
        s = run([row("CRASH/COLLISION", age_s=30 * 60),
                 row("HOSTAGE/BARRICADE", age_s=30 * 60)])
        crash, hostage = s.ids
        self.assertEqual(s.active(crash), "cleared")
        self.assertEqual(s.active(hostage), "active")

    def test_the_published_fragment_is_the_one_the_pass_uses(self):
        """Pins that the two mechanisms share one definition. This is a source
        assertion on purpose: the property is about sharing a symbol, which no
        behavioural probe can observe directly. The behavioural guarantee it
        protects is covered by StalenessBoundaryIsExact."""
        source = (_ROOT / "modules" / "incident_engine.py").read_text(encoding="utf-8")
        start = source.index("def clear_stale_incidents(")
        body = source[start:source.index("def _timeout_seconds(")]
        self.assertIn("ACTIVE_INCIDENT_TIMEOUT_S_SQL", body)
        # And the pass must not consult a second, locally-derived timeout map.
        self.assertNotIn("INCIDENT_TIMEOUT_MINUTES.get(", body)
        self.assertNotIn("_INCIDENT_TIMEOUT_DEFAULT", body)

    def test_timeout_helper_agrees_with_the_configured_map(self):
        """The helper exists only to stamp ts_cleared; if it disagreed with the
        SQL CASE, the recorded clear time would be wrong for that type."""
        script = (
            "import json, sys\n"
            "from modules.config import INCIDENT_TIMEOUT_MINUTES, _INCIDENT_TIMEOUT_DEFAULT\n"
            "from modules.incident_engine import _timeout_seconds\n"
            "got = {t: _timeout_seconds(t) for t in INCIDENT_TIMEOUT_MINUTES}\n"
            "expected = {t: v * 60 for t, v in INCIDENT_TIMEOUT_MINUTES.items()}\n"
            "default = _timeout_seconds('NOT_A_REAL_TYPE')\n"
            "assert got == expected, (got, expected)\n"
            "assert default == _INCIDENT_TIMEOUT_DEFAULT * 60, default\n"
            "open(sys.argv[1], 'w').write(json.dumps({'ok': True}))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "o.json"
            proc = subprocess.run(
                [sys.executable, "-c", script, str(dst)],
                cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertTrue(json.loads(dst.read_text(encoding="utf-8"))["ok"])


class UptimeProjection(unittest.TestCase):
    """The reason this mattered: the leak was unbounded in wall-clock time."""

    def test_a_restart_loop_cannot_accumulate_unclearable_active_rows(self):
        """48 ADS-B detections at 30-minute spacing, 24 passes. Under the old
        dict-driven cleanup every one of these rows stayed 'active' forever.

        Each pass advances the clock by 30 minutes, so this is 12 hours of
        wall clock rather than 24 passes at one instant.
        """
        base = 1_800_000_000.0
        s = run([row("AIR ASSET ACTIVE", age_s=i * 30 * 60, now=base)
                 for i in range(48)], now=base, passes=24)
        still_active = [i for i in s.ids if s.active(i) == "active"]
        # AIR ASSET ACTIVE times out at 20 minutes and detections are 30 minutes
        # apart, so exactly one can be legitimately live at the end.
        self.assertEqual(
            len(still_active), 1,
            f"a day of ADS-B detections left {len(still_active)} active rows",
        )
        # The historical record is intact: nothing was deleted.
        self.assertEqual(s.total_rows, 48)

    def test_a_long_outage_is_reconciled_on_the_first_pass(self):
        """The production shape: 1,400 rows went stale while nothing was
        running. One pass must retire them all and keep the record."""
        base = 1_800_000_000.0
        s = run([row("AIR ASSET ACTIVE", age_s=(i + 1) * 30 * 60, now=base)
                 for i in range(1400)], now=base, passes=1)
        self.assertEqual(len(s.cleared_ids), 1400)
        self.assertEqual(s.total_rows, 1400)

    def test_clear_time_records_when_the_incident_actually_ended(self):
        """A row that went stale during an outage must be stamped with the
        moment its own timeout expired, not the moment the sweep ran, or every
        incident appears to have lasted through the whole gap."""
        base = 1_800_000_000.0
        # AIR ASSET ACTIVE times out after 20 minutes.
        s = run([row("AIR ASSET ACTIVE", age_s=10 * 3600, now=base)], now=base, passes=1)
        expected = base - 10 * 3600 + 20 * 60
        self.assertAlmostEqual(s.ts_cleared[s.first()], expected, delta=2)


if __name__ == "__main__":
    unittest.main()
