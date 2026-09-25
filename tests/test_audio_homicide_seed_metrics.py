"""
tests/test_audio_homicide_seed_metrics.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The curated homicide seed must be read through the one shared seed-path
contract (``modules.config`` -> ``modules.homicide_count``) by both of its
readers in audio_receiver.py: the Prometheus gauges and the ``!query`` bot.

Coverage:
  - the gauges and the !query summary report the *configured* seed, not
    ``homicides_2026.json`` next to the source file, so a relocated deployment
    never reports a different (or empty) dataset;
  - HOMICIDE_SEED_PATH, BATTLE_BUDDY_DATA_DIR and BATTLE_BUDDY_HOME are all
    honoured, and a whitespace-only value is treated as unset, exactly as
    everywhere else;
  - a missing or corrupt seed is visibly distinguishable from a legitimate
    zero: ``battlebuddy_homicides_seed_error`` = 1, the fault is logged, and
    the freshness gauge falls to 0 so the ops gate fails;
  - a legitimately empty seed is *not* an error.

audio_receiver.py cannot be imported in-process (it builds the whole Flask app
and pulls optional providers), so the module is loaded in a child process with
``stripe`` stubbed — the same approach tests/test_audio_backlog_metrics.py uses.
Every run redirects BATTLE_BUDDY_HOME/DB_PATH into a temp dir, so the production
tree is never read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SEED_BASENAME = "homicides_2026.json"

_CHILD = textwrap.dedent(
    """
    import json
    import sys
    from unittest import mock

    sys.modules["stripe"] = mock.MagicMock()

    import audio_receiver

    print(json.dumps({
        "specs": audio_receiver._homicide_seed_metric_specs(),
        "summary": audio_receiver._homicide_seed_summary(),
    }))
    """
)

# Two entries, one of them a multi-victim incident, so a reader that silently
# fell back to a zero (or to the repo copy) cannot produce these numbers.
_SEED = [
    {"n": 1, "date": "2026-01-09", "address": "8201 Tuscany Way", "summary": "homicide",
     "url": "https://example.com/homicide-1"},
    {"n": 2, "date": "2026-03-01", "address": "700 W 6th St", "summary": "mass shooting",
     "url": "https://example.com/homicide-2", "count": 3},
]


class AudioHomicideSeedMetricsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self.base / "data"
        self.data_dir.mkdir()
        self.seed_path = self.data_dir / _SEED_BASENAME

    # -- helpers ---------------------------------------------------------

    def _write_seed(self, entries) -> None:
        self.seed_path.write_text(json.dumps(entries), encoding="utf-8")

    def _run(self, env_extra: dict) -> dict:
        env = os.environ.copy()
        env.update({
            "BATTLE_BUDDY_HOME": str(self.base),
            "DB_PATH": str(self.base / "calls.db"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SMOKE_TEST_BASE_URL": "",
        })
        # Start from a clean slate so an inherited value cannot decide the test.
        for key in ("HOMICIDE_SEED_PATH", "BATTLE_BUDDY_DATA_DIR"):
            env.pop(key, None)
        env.update(env_extra)
        result = subprocess.run(
            [sys.executable, "-c", _CHILD],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            check=True,
            text=True,
            timeout=180,
        )
        payload = json.loads(result.stdout.splitlines()[-1])
        payload["stdout"] = result.stdout
        return payload

    @staticmethod
    def _gauges(payload) -> dict:
        return {name: {"help": help_text, "value": value}
                for name, help_text, value in payload["specs"]}

    # -- the configured seed is the one that is read ---------------------

    def test_gauges_and_summary_follow_the_configured_seed(self):
        self._write_seed(_SEED)
        payload = self._run({"HOMICIDE_SEED_PATH": str(self.seed_path)})
        gauges = self._gauges(payload)

        self.assertEqual(gauges["battlebuddy_homicides_ytd"]["value"], 2)
        self.assertEqual(gauges["battlebuddy_homicides_ytd_victims"]["value"], 4)
        self.assertEqual(
            gauges["battlebuddy_homicides_seed_newest_ts"]["value"],
            datetime.strptime("2026-03-01", "%Y-%m-%d").timestamp(),
        )
        self.assertEqual(gauges["battlebuddy_homicides_seed_error"]["value"], 0)
        self.assertEqual(payload["summary"], "2 homicide incidents, 4 victims YTD 2026")

    def test_data_dir_override_is_honoured(self):
        """Proves the shared resolver is used, not a single env var."""
        self._write_seed(_SEED)
        payload = self._run({"BATTLE_BUDDY_DATA_DIR": str(self.data_dir)})
        self.assertEqual(self._gauges(payload)["battlebuddy_homicides_ytd"]["value"], 2)

    def test_home_override_is_honoured(self):
        self._write_seed(_SEED)
        payload = self._run({"BATTLE_BUDDY_HOME": str(self.data_dir)})
        self.assertEqual(self._gauges(payload)["battlebuddy_homicides_ytd"]["value"], 2)

    def test_whitespace_seed_env_is_treated_as_unset(self):
        self._write_seed(_SEED)
        payload = self._run({
            "HOMICIDE_SEED_PATH": "   ",
            "BATTLE_BUDDY_DATA_DIR": str(self.data_dir),
        })
        self.assertEqual(self._gauges(payload)["battlebuddy_homicides_ytd"]["value"], 2)

    def test_repo_seed_next_to_the_source_is_never_the_fallback(self):
        """Regression: the reader used to open audio_receiver's own directory."""
        repo_seed = _ROOT / _SEED_BASENAME
        self.assertTrue(repo_seed.exists(), "the clone ships a curated seed")
        self._write_seed(_SEED)
        payload = self._run({"HOMICIDE_SEED_PATH": str(self.seed_path)})
        gauges = self._gauges(payload)

        repo_entries = len(json.loads(repo_seed.read_text(encoding="utf-8")))
        self.assertNotEqual(gauges["battlebuddy_homicides_ytd"]["value"], float(repo_entries),
                            "gauges must not report the seed beside audio_receiver.py")
        self.assertEqual(gauges["battlebuddy_homicides_ytd"]["value"], 2)

    def test_source_has_no_module_relative_seed_read(self):
        import io
        import tokenize

        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        # Drop comments and string literals: the filename may be named in a
        # docstring that explains the old behaviour, never in executable code.
        code = "".join(
            tok.string
            for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type not in (tokenize.COMMENT, tokenize.STRING)
        )
        self.assertNotIn(_SEED_BASENAME, code)
        self.assertNotIn("os.path.dirname(__file__)", code)
        self.assertIn("_homicide_seed_metric_specs", code)
        self.assertIn("_homicide_seed_summary", code)

    # -- a fault is visibly not a zero ------------------------------------

    def test_missing_seed_is_an_error_not_a_zero(self):
        payload = self._run({"HOMICIDE_SEED_PATH": str(self.data_dir / "absent.json")})
        gauges = self._gauges(payload)

        self.assertEqual(gauges["battlebuddy_homicides_seed_error"]["value"], 1)
        self.assertEqual(gauges["battlebuddy_homicides_ytd"]["value"], 0)
        self.assertEqual(gauges["battlebuddy_homicides_ytd_victims"]["value"], 0)
        # Freshness falls to 0, which fails the ops "homicide data fresh" gate.
        self.assertEqual(gauges["battlebuddy_homicides_seed_newest_ts"]["value"], 0)
        self.assertIn("1 = missing/corrupt", gauges["battlebuddy_homicides_seed_error"]["help"])
        self.assertEqual(payload["summary"], "homicide data unavailable")
        self.assertIn("homicide seed unavailable", payload["stdout"])

    def test_corrupt_seed_is_an_error(self):
        self.seed_path.write_text("[[[ truncated", encoding="utf-8")
        payload = self._run({"HOMICIDE_SEED_PATH": str(self.seed_path)})
        self.assertEqual(self._gauges(payload)["battlebuddy_homicides_seed_error"]["value"], 1)
        self.assertIn("homicide seed unavailable", payload["stdout"])

    def test_non_list_seed_is_an_error(self):
        self.seed_path.write_text(json.dumps({"n": 1}), encoding="utf-8")
        payload = self._run({"HOMICIDE_SEED_PATH": str(self.seed_path)})
        self.assertEqual(self._gauges(payload)["battlebuddy_homicides_seed_error"]["value"], 1)

    def test_legitimately_empty_seed_is_not_an_error(self):
        self._write_seed([])
        payload = self._run({"HOMICIDE_SEED_PATH": str(self.seed_path)})
        gauges = self._gauges(payload)

        self.assertEqual(gauges["battlebuddy_homicides_seed_error"]["value"], 0)
        self.assertEqual(gauges["battlebuddy_homicides_ytd"]["value"], 0)
        self.assertEqual(gauges["battlebuddy_homicides_ytd_victims"]["value"], 0)
        self.assertEqual(payload["summary"], "0 homicide incidents, 0 victims YTD 2026")
        self.assertNotIn("homicide seed unavailable", payload["stdout"])

    def test_ops_gate_reads_the_seed_error_gauge(self):
        """scripts/ops_verify.py must fail on a fault, not on a false zero."""
        source = (_ROOT / "scripts" / "ops_verify.py").read_text(encoding="utf-8")
        self.assertIn("battlebuddy_homicides_seed_error", source)
        self.assertIn('gate("homicide seed readable"', source)


if __name__ == "__main__":
    unittest.main()
