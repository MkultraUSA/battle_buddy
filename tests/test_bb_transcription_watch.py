"""Tests for scripts/bb_transcription_watch.py.

This script is an alerting path, and it was silently dead: it queried three
metric names the application never emitted, and `metrics.get(key, 0.0)` turned
each miss into a healthy zero. Every queue gate therefore read 0 forever and
could not fire, while the watcher printed `raw_queue=0 oldest=0s failed=0` --
fabricated health indistinguishable from a healthy queue.

The tests below mostly guard that class of bug: a metric this script depends on
must exist, under the labels it expects, or the watcher must say so.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bb_transcription_watch.py"


def _load():
    spec = importlib.util.spec_from_file_location("bb_transcription_watch", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules, so register first.
    sys.modules["bb_transcription_watch"] = module
    spec.loader.exec_module(module)
    return module


watch = _load()


def _body(**overrides) -> str:
    """A minimal but complete exposition body for the metrics the watcher needs."""
    lines = [
        'battlebuddy_transcript_quality_coverage_ratio{window="15m"} 1',
        'battlebuddy_transcript_quality_reliability_score{window="15m"} 1',
        "battlebuddy_transcription_in_progress 0",
        'battlebuddy_transcription_completed{status="lock_timeout",window="15m"} 0',
        'battlebuddy_transcription_completed{status="timeout",window="15m"} 0',
        'battlebuddy_transcription_completed{status="exception",window="15m"} 0',
        'battlebuddy_transcription_completed{status="empty",window="15m"} 0',
        'battlebuddy_transcription_completed{status="success",window="15m"} 20',
        'battlebuddy_transcription_success_ratio{window="15m"} 1',
        "battlebuddy_transcription_latency_seconds_p95{window=\"15m\"} 5",
        "battlebuddy_process_rss_bytes 1503238553",
        "battlebuddy_backlog_files_pending 0",
        "battlebuddy_backlog_files_failed 0",
        "battlebuddy_backlog_files_scan_error 0",
        "battlebuddy_backlog_oldest_age_seconds 0",
    ]
    for key, value in overrides.items():
        name, _, raw = key.partition("|")
        if raw:
            lines = [ln for ln in lines if not ln.startswith(name)]
            lines.append(f"{name}{{{raw}}} {value}")
        else:
            lines = [ln for ln in lines if not ln.startswith(name)]
            lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


class TestParseMetrics(unittest.TestCase):
    def test_labels_are_sorted_and_kept(self):
        metrics = watch.parse_metrics('m{a="2",b="1"} 7\n')
        self.assertIn(("m", (("a", "2"), ("b", "1"))), metrics)

    def test_label_order_in_source_does_not_matter(self):
        first = watch.parse_metrics('m{a="1",b="2"} 5\n')
        second = watch.parse_metrics('m{b="2",a="1"} 5\n')
        self.assertEqual(first, second)

    def test_comments_and_garbage_are_ignored(self):
        metrics = watch.parse_metrics("# HELP x y\n# TYPE x counter\n\nnotanumber\nm 1\n")
        self.assertEqual({("m", ()): 1.0}, metrics)


class TestMissingMetricsAreLoud(unittest.TestCase):
    """The bug class this whole change exists to close."""

    def test_a_missing_required_metric_is_critical_not_healthy(self):
        reader = watch.MetricReader(watch.parse_metrics(_body()))
        # Drop the age metric the way the old names were dropped.
        body = "\n".join(
            ln for ln in _body().splitlines()
            if not ln.startswith("battlebuddy_backlog_oldest_age_seconds")
        )
        reader = watch.MetricReader(watch.parse_metrics(body))
        st = watch.build_status(reader)
        level, reasons = watch.evaluate(st, reader)
        self.assertEqual("critical", level)
        self.assertTrue(
            any("battlebuddy_backlog_oldest_age_seconds" in r for r in reasons),
            f"the absent metric must be named; got {reasons}",
        )

    def test_reader_records_its_misses(self):
        reader = watch.MetricReader(watch.parse_metrics("unrelated_metric 1\n"))
        reader.read("battlebuddy_backlog_files_pending")
        self.assertIn("battlebuddy_backlog_files_pending", reader.missing)

    def test_healthy_body_reports_ok(self):
        reader = watch.MetricReader(watch.parse_metrics(_body()))
        st = watch.build_status(reader)
        level, reasons = watch.evaluate(st, reader)
        self.assertEqual("ok", level, f"unexpected: {reasons}")

    def test_no_required_metric_missing_when_all_present(self):
        """A counter with no samples has no line at all.

        battlebuddy_ingest_outcomes is legitimately absent until something is
        queued or shed, so "no misses whatsoever" is the wrong assertion -- what
        matters is that nothing the watcher *depends on* is missing.
        """
        reader = watch.MetricReader(watch.parse_metrics(_body()))
        watch.build_status(reader)
        required = {name for name, _labels in watch.REQUIRED_METRICS}
        self.assertEqual(set(), reader.missing & required)


class TestQueueGates(unittest.TestCase):
    def _eval(self, **overrides):
        reader = watch.MetricReader(watch.parse_metrics(_body(**overrides)))
        st = watch.build_status(reader)
        return watch.evaluate(st, reader)

    def test_stalled_worker_is_caught_by_age_not_depth(self):
        """Depth alone cannot see this failure.

        claim takes a lease and leaves the item in pending, so a worker that
        claims a clip and dies holds the depth at 1 forever while nothing
        progresses. The age is what moves.
        """
        level, reasons = self._eval(
            **{"battlebuddy_backlog_files_pending|": 1,
               "battlebuddy_backlog_oldest_age_seconds": 1200}
        )
        self.assertEqual("critical", level)
        self.assertTrue(any("not draining" in r for r in reasons), reasons)

    def test_moderate_age_warns(self):
        level, reasons = self._eval(
            **{"battlebuddy_backlog_files_pending|": 1,
               "battlebuddy_backlog_oldest_age_seconds": 400}
        )
        self.assertEqual("warning", level)
        self.assertTrue(any("300s" in r for r in reasons), reasons)

    def test_age_ignored_when_queue_is_empty(self):
        """A stale reading with nothing queued is not a stall."""
        level, reasons = self._eval(
            **{"battlebuddy_backlog_files_pending|": 0,
               "battlebuddy_backlog_oldest_age_seconds": 1200}
        )
        self.assertEqual("ok", level, reasons)

    def test_unreadable_queue_is_critical(self):
        level, reasons = self._eval(
            **{"battlebuddy_backlog_files_scan_error": 1}
        )
        self.assertEqual("critical", level)
        self.assertTrue(any("unreadable" in r for r in reasons), reasons)

    def test_depth_thresholds(self):
        self.assertEqual(
            "warning", self._eval(**{"battlebuddy_backlog_files_pending|": 30})[0]
        )
        self.assertEqual(
            "critical", self._eval(**{"battlebuddy_backlog_files_pending|": 150})[0]
        )

    def test_failed_clips(self):
        self.assertEqual(
            "warning", self._eval(**{"battlebuddy_backlog_files_failed|": 2})[0]
        )
        self.assertEqual(
            "critical", self._eval(**{"battlebuddy_backlog_files_failed|": 9})[0]
        )


class TestRatioGatesNeedADenominator(unittest.TestCase):
    """A ratio with no denominator is not a failure.

    Traffic is ~1 call per 85 seconds, so a 15-minute window is frequently
    empty. The application computes the success ratio as 0.0 for such a window,
    which is indistinguishable from every transcription failing -- so an
    unguarded gate pages continuously on a healthy quiet system.

    Caught by a dry run against production: the live value was 1.0 moments after
    the same read produced 0.00.
    """

    def _eval(self, **overrides):
        reader = watch.MetricReader(watch.parse_metrics(_body(**overrides)))
        st = watch.build_status(reader)
        return st, *watch.evaluate(st, reader)

    def test_empty_window_does_not_page(self):
        # Reproduce what the application actually reports for an empty window:
        # no attempts at all, and a success ratio of 0.0 because the denominator
        # is zero.
        body = _body(**{
            'battlebuddy_transcription_completed|status="success",window="15m"': 0,
            'battlebuddy_transcription_success_ratio|window="15m"': 0.0,
        })
        reader = watch.MetricReader(watch.parse_metrics(body))
        st = watch.build_status(reader)
        self.assertEqual(0, st.samples_15m)
        self.assertEqual(0.0, st.success_ratio_15m,
                         "this is the 0/0 reading that must not page")
        level, reasons = watch.evaluate(st, reader)
        self.assertEqual("ok", level, f"empty window paged: {reasons}")
        self.assertTrue(any("skipped" in r for r in reasons), reasons)

    def test_small_window_does_not_page(self):
        _st, level, reasons = self._eval(
            **{'battlebuddy_transcription_completed|status="success",window="15m"': 2}
        )
        self.assertEqual("ok", level, f"2 samples paged: {reasons}")

    def test_real_failure_with_enough_samples_still_pages(self):
        """The guard must not swallow genuine failures."""
        _st, level, reasons = self._eval(
            **{
                'battlebuddy_transcription_completed|status="success",window="15m"': 5,
                'battlebuddy_transcription_completed|status="exception",window="15m"': 40,
                'battlebuddy_transcription_success_ratio|window="15m"': 0.11,
            }
        )
        self.assertEqual("critical", level)
        self.assertTrue(any("success ratio" in r for r in reasons), reasons)


class TestIngestOutcomes(unittest.TestCase):
    def test_shed_audio_is_counted_and_alerts_once_meaningful(self):
        reader = watch.MetricReader(watch.parse_metrics(
            _body(**{
                'battlebuddy_ingest_outcomes_total|reason="throttled",node="pi5"': 60,
            })
        ))
        st = watch.build_status(reader)
        level, reasons = watch.evaluate(st, reader)
        self.assertEqual(60, st.ingest_shed)
        self.assertEqual("warning", level)
        self.assertTrue(any("discarded" in r for r in reasons), reasons)

    def test_queued_audio_is_not_a_loss(self):
        reader = watch.MetricReader(watch.parse_metrics(
            _body(**{
                'battlebuddy_ingest_outcomes_total|reason="backlogged",node="pi5"': 500,
            })
        ))
        st = watch.build_status(reader)
        level, reasons = watch.evaluate(st, reader)
        self.assertEqual(500, st.ingest_backlogged)
        self.assertEqual(0, st.ingest_shed)
        self.assertEqual("ok", level, reasons)


class TestRegressionGuards(unittest.TestCase):
    """Explicitly pin the names that were wrong for so long."""

    STALE_NAMES = (
        "battlebuddy_raw_audio_queue_pending",
        "battlebuddy_raw_audio_queue_oldest_age_seconds",
        "battlebuddy_raw_audio_queue_failed",
    )

    def test_no_metric_constant_uses_an_old_nonexistent_name(self):
        """Check the constants, not the file text.

        The module docstring names the stale metrics on purpose, to explain the
        bug. Asserting on raw source text therefore fails on its own
        documentation; what matters is that none of them is wired up as a name
        the watcher reads.
        """
        wired = {
            value for key, value in vars(watch).items()
            if key.startswith("METRIC_") and isinstance(value, str)
        }
        for stale in self.STALE_NAMES:
            self.assertNotIn(
                stale, wired,
                f"{stale} never existed; querying it reads 0 and disables its gate",
            )

    def test_required_metrics_use_names_the_app_exports(self):
        """The end-to-end guard: watched names must be published by the app."""
        app = (_SCRIPT.parent.parent / "audio_receiver.py").read_text(encoding="utf-8")
        for name, _labels in watch.REQUIRED_METRICS:
            self.assertIn(
                name, app,
                f"the watcher alerts on {name}, which the application never "
                "exports -- that is how its queue gates went dead",
            )

    def test_names_the_watcher_relies_on_are_published_by_the_app(self):
        """The strongest version: check the application's own source.

        Parsing the constant out of audio_receiver.py means a rename there fails
        this test instead of silently blinding the watcher in production.
        """
        app = (_SCRIPT.parent.parent / "audio_receiver.py").read_text(encoding="utf-8")
        for name in (
            watch.METRIC_QUEUE_PENDING,
            watch.METRIC_QUEUE_FAILED,
            watch.METRIC_QUEUE_SCAN_ERROR,
            watch.METRIC_QUEUE_OLDEST_AGE,
            watch.METRIC_IN_PROGRESS,
        ):
            self.assertIn(name, app, f"{name} is watched but not exported by the app")


if __name__ == "__main__":
    unittest.main()