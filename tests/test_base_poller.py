import importlib.util
import unittest
import unittest.mock as mock
from pathlib import Path

_BASE_PATH = Path(__file__).parent.parent / "modules" / "pollers" / "base.py"
_SPEC = importlib.util.spec_from_file_location("base_poller_under_test", _BASE_PATH)
_base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_base)
BasePoller = _base.BasePoller


class _SequencePoller(BasePoller):
    NAME = "test-health"

    def __init__(self, outcomes, interval=10):
        super().__init__(interval=interval)
        self.outcomes = iter(outcomes)
        self.run_calls = 0

    def run(self):
        outcome = next(self.outcomes)
        self.run_calls += 1
        if isinstance(outcome, Exception):
            raise outcome


class BasePollerTests(unittest.TestCase):
    def test_jitter_bounds_and_backoff_reset_after_successful_cycle(self):
        poller = _SequencePoller([
            RuntimeError("first"),
            RuntimeError("second"),
            None,
            RuntimeError("fourth"),
        ])
        delays = []

        def wait(delay):
            delays.append(delay)
            if len(delays) == 4:
                poller.stop_event.set()
            return poller.stop_event.is_set()

        with mock.patch.object(
            _base.random,
            "random",
            side_effect=[0.0, 1.0, 0.0, 0.0],
        ), mock.patch.object(poller.stop_event, "wait", side_effect=wait):
            poller._loop()

        self.assertEqual(poller.run_calls, 4)
        self.assertEqual(delays, [9.0, 22.0, 9.0, 9.0])

    def test_backoff_stays_capped_after_long_failure_run(self):
        failure_count = 1100
        poller = _SequencePoller(
            [RuntimeError("failure")] * failure_count,
            interval=0.1,
        )
        delays = []

        def wait(delay):
            delays.append(delay)
            if len(delays) == failure_count:
                poller.stop_event.set()
            return poller.stop_event.is_set()

        with mock.patch.object(_base.random, "random", return_value=0.5), \
             mock.patch("builtins.print"), \
             mock.patch.object(poller.stop_event, "wait", side_effect=wait):
            poller._loop()

        self.assertEqual(poller.run_calls, failure_count)
        self.assertEqual(delays[16:], [3600.0] * (failure_count - 16))

    def test_health_tracks_failures_and_resets_after_success(self):
        poller = _SequencePoller([])
        poller._record_failure()
        poller._record_failure()

        with mock.patch.object(_base.time, "time", return_value=100.0):
            poller._record_success()

        record = next(
            item for item in _base.get_poller_health(now=130.0)
            if item["name"] == "test-health"
        )
        self.assertEqual(record["consecutive_failures"], 0)
        self.assertEqual(record["last_success_ts"], 100.0)
        self.assertEqual(record["last_success_age_seconds"], 30.0)
        self.assertFalse(record["active"])

        poller._record_failure()
        record = next(
            item for item in _base.get_poller_health(now=130.0)
            if item["name"] == "test-health"
        )
        self.assertEqual(record["consecutive_failures"], 1)
        self.assertEqual(record["last_success_age_seconds"], 30.0)

    def test_health_snapshot_survives_clock_failure(self):
        poller = _SequencePoller([])

        with mock.patch.object(_base.time, "time", side_effect=RuntimeError("clock unavailable")):
            self.assertEqual(_base.get_poller_health(), [])

        self.assertEqual(poller.consecutive_failures, 0)


if __name__ == "__main__":
    unittest.main()
