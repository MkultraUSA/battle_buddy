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

    audio_receiver._backlog_queue.extend(range(int(os.environ["TEST_MEMORY_DEPTH"])))
    if os.environ["TEST_SCAN_MODE"] == "unreadable":
        with mock.patch.object(
            raw_audio_queue.Path,
            "glob",
            side_effect=PermissionError("denied"),
        ):
            state = audio_receiver._get_backlog_metric_state()
    else:
        state = audio_receiver._get_backlog_metric_state()
    print(json.dumps({
        "state": state,
        "metrics": audio_receiver._backlog_file_metric_specs(state),
    }))
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

        self.assertEqual(
            payload["state"],
            {
                "memory_pending": 3,
                "file_pending": 2,
                "file_failed": 1,
                "file_scan_error": 0,
                "total_pending": 5,
            },
        )
        self.assertEqual(metrics["battlebuddy_backlog_files_pending"]["value"], 2)
        self.assertEqual(metrics["battlebuddy_backlog_files_failed"]["value"], 1)
        self.assertEqual(metrics["battlebuddy_backlog_total_depth"]["value"], 5)
        self.assertIn("in-memory remote-worker queue", metrics["battlebuddy_backlog_total_depth"]["help"])
        self.assertIn("scan-error", metrics["battlebuddy_backlog_total_depth"]["help"])
        self.assertEqual(metrics["battlebuddy_backlog_files_scan_error"]["value"], 0)

    def test_missing_root_sets_scan_error_without_creating_it(self):
        payload = self._run(memory_depth=2)
        metrics = self._metric_map(payload)

        self.assertFalse(self.root.exists())
        self.assertEqual(payload["state"]["file_pending"], 0)
        self.assertEqual(payload["state"]["file_failed"], 0)
        self.assertEqual(payload["state"]["file_scan_error"], 1)
        self.assertEqual(payload["state"]["total_pending"], 2)
        self.assertEqual(metrics["battlebuddy_backlog_files_scan_error"]["value"], 1)

    def test_unreadable_root_sets_scan_error(self):
        (self.root / "pending").mkdir(parents=True)
        (self.root / "failed").mkdir()

        payload = self._run(memory_depth=2, scan_mode="unreadable")
        metrics = self._metric_map(payload)

        self.assertEqual(payload["state"]["file_pending"], 0)
        self.assertEqual(payload["state"]["file_failed"], 0)
        self.assertEqual(payload["state"]["file_scan_error"], 1)
        self.assertEqual(payload["state"]["total_pending"], 2)
        self.assertEqual(metrics["battlebuddy_backlog_files_scan_error"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
