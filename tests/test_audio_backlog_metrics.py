import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_CHILD = textwrap.dedent(
    """
    import json
    import os
    import sys
    from unittest import mock

    sys.modules["stripe"] = mock.MagicMock()

    import audio_receiver
    from modules import raw_audio_queue
    from modules.database import init_db

    # The /metrics collector reads the incidents table and aborts the whole
    # response if it is missing ("[metrics] collector error: no such table:
    # incidents"), so an uninitialised DB silently yields NO metrics at all.
    # That is exactly the failure mode an end-to-end scrape must not be fooled
    # by, so create the schema rather than assert against an empty body.
    init_db()

    # init_db() alone does NOT produce a schema the collector can read. The
    # incidents queries filter on `is_test`, which is added by a migration, so a
    # from-scratch database yields "[metrics] collector error: no such column:
    # is_test" and an EMPTY body under HTTP 200. Bring the test schema up to
    # production shape.
    import sqlite3 as _sqlite3
    with _sqlite3.connect(os.environ["DB_PATH"]) as _c:
        _cols = {r[1] for r in _c.execute("PRAGMA table_info(incidents)")}
        if "is_test" not in _cols:
            _c.execute("ALTER TABLE incidents ADD COLUMN is_test INTEGER DEFAULT 0")

    # Seed the durable queue rather than an in-memory deque: there is no longer
    # an in-process queue, and seeding real items is what makes the depth
    # assertions mean something.
    # Only seed when asked. These tests include cases that deliberately leave
    # the queue root missing to prove the metrics code does not create it as a
    # side effect, and seeding would create it.
    _depth = int(os.environ["TEST_MEMORY_DEPTH"])
    if _depth:
        _q = raw_audio_queue.RAW_AUDIO_QUEUE_DIR / "pending"
        _q.mkdir(parents=True, exist_ok=True)
        for _i in range(_depth):
            (_q / f"seed-{_i}.json").write_text(json.dumps({
                "id": f"seed-{_i}", "created_ts": 1.0, "node": "pi5",
            }), encoding="utf-8")

    if os.environ["TEST_SCAN_MODE"] == "unreadable":
        with mock.patch.object(
            raw_audio_queue.Path,
            "glob",
            side_effect=PermissionError("denied"),
        ):
            state = audio_receiver._get_backlog_metric_state()
    else:
        state = audio_receiver._get_backlog_metric_state()
    # The real /metrics body, not just the helper's spec list. ops_verify gates
    # on battlebuddy_backlog_queue_depth and a Grafana panel graphs it, so a
    # regression that leaves the gauge reading a constant 0 would silently
    # disable the alert while every unit test on _get_backlog_metric_state still
    # passed. Only an end-to-end scrape can catch that.
    resp = audio_receiver.app.test_client().get("/metrics")
    scraped = {}
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("battlebuddy_backlog_"):
            scraped[line.split(" ")[0]] = float(line.rsplit(" ", 1)[1])
    print(json.dumps({
        "state": state,
        "metrics": audio_receiver._backlog_file_metric_specs(state),
        "scraped": scraped,
        "scrape_status": resp.status_code,
        "ingest_help": chr(10).join(
            l for l in resp.get_data(as_text=True).splitlines()
            if l.startswith("# HELP battlebuddy_ingest_outcomes")
        ),
    }))
    """
)
_POLLER_HEALTH_CHILD = textwrap.dedent(
    """
    import json
    import os
    import sys
    from unittest import mock

    sys.modules["stripe"] = mock.MagicMock()

    import audio_receiver
    from modules.pollers.base import BasePoller

    class Poller(BasePoller):
        NAME = "metric-healthy"

        def __init__(self):
            super().__init__(interval=10)

        def run(self):
            pass

    class FailingPoller(Poller):
        NAME = "metric-failing"

    healthy = Poller()
    failing = FailingPoller()
    healthy._record_failure()
    with mock.patch("modules.pollers.base.time.time", return_value=100.0):
        healthy._record_success()
    for _ in range(3):
        failing._record_failure()

    if os.environ["TEST_HEALTH_MODE"] == "failure":
        with mock.patch(
            "modules.pollers.base.get_poller_health",
            side_effect=RuntimeError("credential-like-secret"),
        ):
            body, status, _headers = audio_receiver.prometheus_metrics()
    else:
        with mock.patch("modules.pollers.base.time.time", return_value=130.0):
            body, status, _headers = audio_receiver.prometheus_metrics()

    body = body.decode()
    samples = {}
    for metric in (
        "battlebuddy_poller_consecutive_failures",
        "battlebuddy_poller_last_success_age_seconds",
        "battlebuddy_poller_active",
    ):
        for name in ("metric-healthy", "metric-failing", "reddit-intel"):
            prefix = metric + '{poller="' + name + '"}'
            for line in body.splitlines():
                if line.startswith(prefix):
                    samples[metric + "|" + name] = float(line.rsplit(" ", 1)[1])
                    break
    print(json.dumps({"status": status, "samples": samples, "body": body}))
    """
)


class AudioBacklogMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "raw_audio_queue"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, memory_depth, scan_mode="normal"):
        env = os.environ.copy()
        env.update({
            "BATTLE_BUDDY_DATA_DIR": str(self.base),
            "BATTLE_BUDDY_HOME": str(self.base),
            "BB_RAW_AUDIO_QUEUE_DIR": str(self.root),
            "DB_PATH": str(self.base / "calls.db"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SMOKE_TEST_BASE_URL": "",
            "TEST_MEMORY_DEPTH": str(memory_depth),
            "TEST_SCAN_MODE": scan_mode,
        })
        result = subprocess.run(
            [sys.executable, "-c", _CHILD],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            check=True,
            text=True,
            timeout=120,
        )
        return json.loads(result.stdout.splitlines()[-1])

    @staticmethod
    def _metric_map(payload):
        return {name: {"help": help_text, "value": value} for name, help_text, value in payload["metrics"]}

    def _run_poller_health(self, mode):
        env = os.environ.copy()
        env.update({
            "BATTLE_BUDDY_DATA_DIR": str(self.base),
            "BATTLE_BUDDY_HOME": str(self.base),
            "DB_PATH": str(self.base / "calls.db"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SMOKE_TEST_BASE_URL": "",
            "TEST_HEALTH_MODE": mode,
        })
        result = subprocess.run(
            [sys.executable, "-c", _POLLER_HEALTH_CHILD],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            check=True,
            text=True,
            timeout=120,
        )
        return json.loads(result.stdout.splitlines()[-1])

    def test_poller_health_metrics_report_failure_and_reset(self):
        payload = self._run_poller_health("normal")
        samples = payload["samples"]

        self.assertEqual(payload["status"], 200)
        self.assertEqual(samples["battlebuddy_poller_consecutive_failures|metric-healthy"], 0.0)
        self.assertEqual(samples["battlebuddy_poller_last_success_age_seconds|metric-healthy"], 30.0)
        self.assertEqual(samples["battlebuddy_poller_active|metric-healthy"], 0.0)
        self.assertEqual(samples["battlebuddy_poller_consecutive_failures|metric-failing"], 3.0)
        self.assertEqual(samples["battlebuddy_poller_last_success_age_seconds|metric-failing"], -1.0)
        self.assertNotIn("battlebuddy_poller_active|reddit-intel", samples)

    def test_poller_health_metric_collection_fails_closed(self):
        payload = self._run_poller_health("failure")

        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["samples"], {})
        self.assertNotIn("credential-like-secret", payload["body"])

    def test_configured_temp_root_counts_and_combines_memory_pending(self):
        pending = self.root / "pending"
        failed = self.root / "failed"
        pending.mkdir(parents=True)
        failed.mkdir()
        (pending / "one.json").write_text("{}", encoding="utf-8")
        (pending / "two.json").write_text("{}", encoding="utf-8")
        (pending / "ignored.wav").write_bytes(b"audio")
        (failed / "three.json").write_text("{}", encoding="utf-8")

        payload = self._run(memory_depth=3)
        metrics = self._metric_map(payload)

        # There is one queue now, not two. It used to report
        # memory_pending + file_pending, which double-counted nothing and
        # measured nothing useful: ops_verify gates on queue_depth, and that
        # gauge read the in-memory half, so it showed 0 while 92 clips waited.
        self.assertEqual(
            payload["state"],
            {
                "pending": 5,
                "file_pending": 5,
                "file_failed": 1,
                "file_scan_error": 0,
                "total_pending": 5,
            },
        )
        self.assertEqual(metrics["battlebuddy_backlog_files_pending"]["value"], 5)
        self.assertEqual(metrics["battlebuddy_backlog_files_failed"]["value"], 1)
        self.assertEqual(metrics["battlebuddy_backlog_total_depth"]["value"], 5)
        self.assertIn("durable", metrics["battlebuddy_backlog_total_depth"]["help"])
        self.assertIn("scan-error", metrics["battlebuddy_backlog_total_depth"]["help"])
        self.assertEqual(metrics["battlebuddy_backlog_files_scan_error"]["value"], 0)
        self.assertEqual(payload["scrape_status"], 200)
        self.assertEqual(
            payload["scraped"]["battlebuddy_backlog_queue_depth"], 5.0,
            "the scraped queue_depth gauge must report the real durable depth; "
            "ops_verify gates on it and a Grafana panel graphs it, so a constant "
            "here means a permanently blind alert",
        )

    def test_ingest_outcome_counter_is_exported_and_starts_empty(self):
        """Shed audio must be visible, or overload loss cannot be measured.

        The whole point of the backlog is that clips get queued instead of
        dropped. That claim is only falsifiable if the dropping is counted --
        and before this, discarded audio left no trace anywhere: it never reached
        the database, so a shed hour looked exactly like a busy one.
        """
        payload = self._run(memory_depth=0)
        self.assertEqual(payload["scrape_status"], 200)
        self.assertIn("battlebuddy_ingest_outcomes", payload["scraped"])
        self.assertEqual(
            0.0, payload["scraped"]["battlebuddy_ingest_outcomes"],
            "a fresh process has shed nothing yet",
        )
        self.assertIn(
            "LOSSES", payload["ingest_help"],
            "the help text must say which outcomes are losses, so the metric "
            "cannot be misread as a throughput count",
        )

    def test_missing_root_sets_scan_error_without_creating_it(self):
        # depth 0 so the child does not create the root it is meant to be absent
        payload = self._run(memory_depth=0)
        metrics = self._metric_map(payload)

        self.assertFalse(self.root.exists())
        self.assertEqual(payload["state"]["pending"], 0)
        self.assertEqual(payload["state"]["file_failed"], 0)
        self.assertEqual(payload["state"]["file_scan_error"], 1)
        self.assertEqual(payload["state"]["total_pending"], 0)
        self.assertEqual(metrics["battlebuddy_backlog_files_scan_error"]["value"], 1)

    def test_unreadable_root_sets_scan_error(self):
        (self.root / "pending").mkdir(parents=True)
        (self.root / "failed").mkdir()

        payload = self._run(memory_depth=0, scan_mode="unreadable")
        metrics = self._metric_map(payload)

        self.assertEqual(payload["state"]["pending"], 0)
        self.assertEqual(payload["state"]["file_failed"], 0)
        self.assertEqual(payload["state"]["file_scan_error"], 1)
        self.assertEqual(payload["state"]["total_pending"], 0)
        self.assertEqual(metrics["battlebuddy_backlog_files_scan_error"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
