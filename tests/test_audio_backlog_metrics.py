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

    # init_db() alone is not enough for the collector: it must be able to read
    # every column audio_receiver queries. That used to require an ALTER here,
    # which is how the missing columns stayed missing -- the workaround lived in
    # a test instead of in init_db. tests/test_schema_contract.py now asserts the
    # schema directly, so this can just call init_db().
    init_db()

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

    # Record a known set of outcomes before scraping. A Prometheus family with
    # no samples emits only HELP/TYPE and no value line, so "starts at zero" is
    # not directly observable -- the counter has to actually be driven to be
    # visible. Counts live in this child process only, so nothing leaks.
    _record_ingest = audio_receiver._record_ingest_outcome
    for _ in range(int(os.environ.get("TEST_SEED_INGEST", "0"))):
        _record_ingest("throttled", "pi5")
    if os.environ.get("TEST_SEED_INGEST"):
        _record_ingest("queue_full", "broadcastify")
        _record_ingest("backlogged", "pi5")

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
        if line.startswith("battlebuddy_backlog_") or line.startswith("battlebuddy_ingest_"):
            # Labelled samples keep their labels in the first field, so key on
            # name+labels rather than the bare metric name.
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
        "llm_help": chr(10).join(
            l for l in resp.get_data(as_text=True).splitlines()
            if l.startswith("# HELP battlebuddy_llm")
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
        self.seed_ingest = 0

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
            "TEST_SEED_INGEST": str(self.seed_ingest),
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

    def test_ingest_outcome_counter_reports_shed_and_queued(self):
        """Shed audio must be visible, or overload loss cannot be measured.

        The backlog's whole claim is that clips get queued instead of dropped.
        That is only falsifiable if the dropping is counted -- and before this,
        discarded audio left no trace anywhere: it never reached the database,
        so a shed hour looked exactly like a busy one. That is how the backlog sat
        at 92 clips shedding 35% of audio with nothing in the data to say so.
        """
        self.seed_ingest = 2
        payload = self._run(memory_depth=0)

        self.assertEqual(payload["scrape_status"], 200)
        self.assertIn(
            "LOSSES", payload["ingest_help"],
            "the help text must say which outcomes are losses, so the metric "
            "cannot be misread as a throughput count",
        )
        s = payload["scraped"]
        # prometheus_client appends _total to a CounterMetricFamily and emits
        # labels in sorted order, hence node= before reason=.
        self.assertEqual(
            2.0, s['battlebuddy_ingest_outcomes_total{node="pi5",reason="throttled"}'],
            "shed audio must be counted, not dropped silently",
        )
        self.assertEqual(
            1.0, s['battlebuddy_ingest_outcomes_total{node="broadcastify",reason="queue_full"}'],
        )
        self.assertEqual(
            1.0, s['battlebuddy_ingest_outcomes_total{node="pi5",reason="backlogged"}'],
            "audio safely queued must be counted separately from audio lost",
        )

    def test_ingest_outcomes_absent_before_anything_is_shed(self):
        """A fresh process emits the HELP block but no value lines.

        Prometheus omits a family with zero samples, so this asserts the family
        is registered and documented rather than inventing a zero.
        """
        payload = self._run(memory_depth=0)
        self.assertEqual(payload["scrape_status"], 200)
        self.assertIn("battlebuddy_ingest_outcomes", payload["ingest_help"])

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

    def test_llm_outcomes_appear_in_a_scrape(self):
        """The counter must actually reach the metrics endpoint.

        Unit tests on llm_analyze can all pass while the metric family is never
        emitted, or emitted under the wrong name. prometheus_client appends _total
        to a CounterMetricFamily, so the wire name is battlebuddy_llm_total.
        """
        payload = self._run(memory_depth=0)
        self.assertEqual(payload["scrape_status"], 200)
        self.assertIn("battlebuddy_llm_total", payload["scraped"],
                      "battlebuddy_llm_total is the emitted name; the family is "
                      "declared as battlebuddy_llm")
        self.assertIn("LLM", payload["llm_help"],
                      "the help text should make the skip reasons legible "
                      "without reading modules/llm.py")

    def test_scraping_metrics_does_not_create_the_queue_root(self):
        """A metric must not create the directory it observes.

        The oldest-age metric is produced by get_raw_audio_queue_stats, and that
        function originally used the mkdir-ing helpers. Scraping /metrics would
        then create a missing queue root, so `scan_error` would stop reporting
        1 and the Hostinger watcher's "queue unreadable" alarm would go blind --
        the metric would have repaired the very fault it exists to reveal.
        """
        payload = self._run(memory_depth=0)
        self.assertFalse(
            self.root.exists(),
            "scraping /metrics created the queue root; scan_error can no longer "
            "report an absent or unreadable queue",
        )
        self.assertEqual(1, payload["state"]["file_scan_error"])

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
