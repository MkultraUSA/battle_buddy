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
    def __init__(self, outcomes):
        super().__init__(interval=10)
        self.outcomes = iter(outcomes)
        self.run_calls = 0

    def run(self):
        outcome = next(self.outcomes)
        self.run_calls += 1
        if isinstance(outcome, Exception):
            raise outcome


class BasePollerTests(unittest.TestCase):
    def test_backoff_resets_after_successful_cycle(self):
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

        with mock.patch.object(_base.random, "random", return_value=0.5), \
             mock.patch.object(poller.stop_event, "wait", side_effect=wait):
            poller._loop()

        self.assertEqual(poller.run_calls, 4)
        self.assertEqual(delays, [10.0, 20.0, 10.0, 10.0])


if __name__ == "__main__":
    unittest.main()
