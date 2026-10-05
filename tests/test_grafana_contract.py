"""The Grafana dashboards and the app's exported metrics must agree.

This is the project's own defect #2, applied to the side nothing currently
watches. The Telegram watcher once queried three metric names the app never
emitted, and `metrics.get(key, 0.0)` turned each miss into a healthy zero — so
its gates could never fire while printing reassuring zeroes. The dashboards have
the same exposure in the opposite direction: a panel whose series is silently
absent renders an empty graph, and an empty graph looks exactly like "no
incidents".

Two directions, both of which have already found something:

  * **dangling** — a dashboard reads a name the app does not emit. The panel is
    blank and always has been.
  * **orphan** — the app emits a name nobody reads. Either dead weight, or a
    signal that was built for a decision and never wired to one.

**These tests run offline.** `fixtures/grafana/*.json` holds the five Battle Buddy
dashboards, fetched deliberately by `scripts/fetch_grafana_dashboards.py`; CI
never calls Grafana. A third-party API with a token that expires and rate limits
that bite is a flake generator in a suite nobody learns to trust, and what needs
checking here is a contract between two things we both control — which a file in
the repository answers completely.

The app's side is observed rather than read: a subprocess imports the real
collector and scrapes `/metrics`, because grepping `audio_receiver.py` for metric
names is the technique that let a browser-side gate be "protected" by a comment
(see test_bb_transcription_watch.py, whose cross-reference guards are
`assertIn(name, source)` and are satisfied by prose).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _ROOT / "fixtures" / "grafana"
_WATCHER = _ROOT / "scripts" / "bb_transcription_watch.py"
_OPS = _ROOT / "scripts" / "ops_verify.py"

_METRIC = re.compile(r"\b(battlebuddy_[a-z_0-9]+)\b")

#: PromQL silently drops a binary operation whose two sides carry different label
#: sets, and it does so **without an error**: the panel renders empty and looks
#: exactly like a calm week.
#:
#: `bb-intel-public`'s DETECTION RATIO was `sum(battlebuddy_incidents_24h) /
#: (battlebuddy_calls_24h > 0) * 100`. The left side aggregates to a label-free
#: vector; the right side keeps whatever labels the call counter carries, and
#: `battlebuddy_incidents_24h` exports twelve separate series. Nothing matched, so
#: the query returned no data at any point in its life — a detection-ratio panel
#: that had never once reported a detection ratio.
#:
#: Every existing check here missed it. Both metric names are real, so the
#: dangling check passed; nothing consumes nothing, so the orphan check passed;
#: and the query is valid PromQL, so it raises nothing. The defect was invisible
#: to all three directions because all three ask "is this name emitted", and the
#: bug is not about names — it is about whether the two sides can ever join.
#:
#: The fix is `on() group_left`, `scalar()`, or aggregating both sides. This
#: catches the shape that needs one of those, which is the shape that was shipped.
_AGGREGATOR = r"(?:sum|avg|min|max|count|stddev|stdvar|topk|bottomk|quantile)"
#: A bare vector filtered by a comparison, e.g. `(battlebuddy_calls_24h > 0)`.
_FILTERED_VECTOR = r"\(\s*[a-z_][a-z_0-9]*\s*[<>=!]+\s*-?[\d.]+\s*\)"
_SILENT_EMPTY = re.compile(
    rf"(?:\b{_AGGREGATOR}\s*\([^()]*\)\s*[/%*]\s*{_FILTERED_VECTOR}"
    rf"|{_FILTERED_VECTOR}\s*[/%*]\s*\b{_AGGREGATOR}\s*\([^()]*\))",
    re.IGNORECASE,
)

#: Metrics the app exports that nothing consumes, with the reason each is allowed.
#:
#: This is a ratchet, not a list of permissions. A name may only be added here
#: deliberately and with a reason, and `test_the_allowlist_has_not_grown` fails if
#: the count rises — so "one more orphaned metric" cannot happen quietly. Fixing
#: one means deleting its entry, which is the point.
KNOWN_ORPHANS = {
    "battlebuddy_llm":
        "Label-free alias emitted alongside the real counter; unused by design.",
    "battlebuddy_llm_total":
        "KNOWN GAP. Objective 2 (#178) made the nine LLM outcomes durable, and "
        "nothing consumes the result — no panel, gate or alert.",
    "battlebuddy_regression_ran":
        "Added in #192 for the regression battery. Awaiting its Grafana panel.",
    "battlebuddy_regression_failed":
        "Added in #192; awaiting its panel. Gated by ops_verify meanwhile.",
    "battlebuddy_regression_check":
        "Added in #192; awaiting its panel. Gated by ops_verify meanwhile.",
    "battlebuddy_regression_last_run_age_seconds":
        "Added in #192; awaiting its panel. Gated by ops_verify meanwhile.",
    "battlebuddy_scrape_samples_total":
        "Broken by design: rebuilt on every scrape, so it reads 1.0 forever and "
        "rate() on it is always 0. Reported by the #190 review; unread as well "
        "as useless. Should be deleted, not displayed.",
    "battlebuddy_scrape_samples":
        "Same broken counter, name without the _total suffix.",
    "battlebuddy_backlog_pending_bytes":
        "Diagnostic detail for the backlog panel; the panel shows depth instead.",
    "battlebuddy_backlog_total_depth":
        "Superseded by battlebuddy_backlog_files_pending, which is what the "
        "dashboard reads. Left exported during the move from RAM to disk.",
    "battlebuddy_regression_error":
        "Emitted only when the battery's results file cannot be read at all, so "
        "it is absent from a healthy scrape and appears here only in a run with "
        "no file. Gated by ops_verify meanwhile. Added in #192.",
}

#: How many entries the allowlist is allowed to have. Lower it when you fix one.
ALLOWLIST_CEILING = len(KNOWN_ORPHANS)


def _fixtures() -> dict[str, dict]:
    out = {}
    for path in sorted(_FIXTURES.glob("*.json")):
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


def _walk(panels, names: set[str]) -> None:
    """Every metric name in every target, alert condition and nested row."""
    for panel in panels or []:
        for target in panel.get("targets") or []:
            for key in ("expr", "query", "rawSql", "definition"):
                value = target.get(key)
                if isinstance(value, str):
                    names.update(_METRIC.findall(value))
        alert = panel.get("alert")
        if isinstance(alert, dict):
            for cond in alert.get("conditions") or []:
                for key in ("expr", "query"):
                    value = cond.get(key)
                    if isinstance(value, str):
                        names.update(_METRIC.findall(value))
        _walk(panel.get("panels"), names)


def _panels_with_exprs(doc: dict):
    """Yield (panel, target) for every target carrying a PromQL expression.

    Unlike `_walk`, which collects names into a set, this hands back the panel
    and the query, because this direction has to report *where* the bad
    expression is rather than only that one exists.
    """
    for panel in doc.get("panels") or []:
        for target in panel.get("targets") or []:
            if isinstance(target, dict) and isinstance(target.get("expr"), str):
                yield panel, target
        yield from _panels_with_exprs(panel)


def _dashboard_names() -> tuple[dict[str, set[str]], set[str]]:
    per_dashboard: dict[str, set[str]] = {}
    for uid, doc in _fixtures().items():
        names: set[str] = set()
        _walk(doc.get("panels"), names)
        for annotation in (doc.get("annotations", {}) or {}).get("list") or []:
            for key in ("expr", "query"):
                value = annotation.get(key)
                if isinstance(value, str):
                    names.update(_METRIC.findall(value))
        per_dashboard[uid] = names
    union: set[str] = set()
    for names in per_dashboard.values():
        union |= names
    return per_dashboard, union


def _emitted_metrics() -> set[str]:
    """Scrape the real registry in a subprocess.

    Imports the app rather than reading it, because grepping the source for
    metric names is precisely how the cross-reference guards in
    test_bb_transcription_watch.py came to be satisfied by a comment.
    """
    child = (
        "import json, sys\n"
        "from unittest import mock\n"
        "for m in ('stripe', 'faster_whisper', 'anthropic'):\n"
        "    sys.modules[m] = mock.MagicMock()\n"
        "import tempfile, os\n"
        "tmp = tempfile.mkdtemp()\n"
        "os.environ.setdefault('DB_PATH', os.path.join(tmp, 'x.db'))\n"
        "os.environ.setdefault('BATTLE_BUDDY_DATA_DIR', tmp)\n"
        "os.environ.setdefault('BB_RAW_AUDIO_QUEUE_DIR', os.path.join(tmp, 'rq'))\n"
        "os.environ.setdefault('TIPS_UPLOAD_DIR', os.path.join(tmp, 't'))\n"
        "os.environ.setdefault('HOMICIDE_SEED_PATH', os.path.join(tmp, 'h.json'))\n"
        "import audio_receiver as ar\n"
        "ar.init_db()\n"
        "from prometheus_client import generate_latest\n"
        "text = generate_latest(ar._BB_METRICS_REGISTRY).decode()\n"
        "names = set()\n"
        "for line in text.splitlines():\n"
        "    if line.startswith('# HELP'):\n"
        "        names.add(line.split()[2])\n"
        "print('NAMES:' + json.dumps(sorted(names)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child], cwd=str(_ROOT), capture_output=True,
        text=True, timeout=600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode != 0 or "NAMES:" not in proc.stdout:
        return set()
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("NAMES:")][-1]
    names = set(json.loads(line[len("NAMES:"):]))
    # A histogram or counter exposes _bucket/_sum/_count; panels may name the
    # family or a suffix of it, so accept either.
    families: set[str] = set()
    for name in names:
        for suffix in ("_bucket", "_sum", "_count", "_created", "_total"):
            if name.endswith(suffix):
                families.add(name[: -len(suffix)])
    return names | families


def _matches(name: str, candidates) -> bool:
    return any(name == c or name.startswith(c + "_") or c.startswith(name + "_")
               or name in c for c in candidates)


class TestTheFixturesAreReal(unittest.TestCase):
    def test_all_five_dashboards_are_present_and_not_empty(self):
        docs = _fixtures()
        self.assertEqual(5, len(docs), f"expected 5 dashboards, found {sorted(docs)}")
        for uid, doc in docs.items():
            with self.subTest(uid=uid):
                self.assertTrue(doc.get("panels"),
                                f"{uid} has no panels — the fetch probably failed")
                self.assertEqual(uid, (doc.get("__meta__") or {}).get("uid"))
                self.assertIsNotNone((doc.get("__meta__") or {}).get("version"))

    def test_the_fixtures_cover_a_lot_of_panels(self):
        total = 0
        for doc in _fixtures().values():
            total += len(doc.get("panels") or [])
        self.assertGreater(total, 100,
                           f"only {total} panels across the fixtures; the fetch is "
                           "probably returning an error payload or a partial tree")

    def test_no_credential_reached_a_fixture(self):
        """The fetch redacts, and this proves it rather than trusting it."""
        for path in _FIXTURES.glob("*.json"):
            raw = path.read_text(encoding="utf-8")
            for marker in ("eyJ", "glsa_", "Bearer ", "secureJsonData\":"):
                with self.subTest(path=path.name, marker=marker):
                    self.assertNotIn(marker, raw)


class TestNoDashboardReadsAMetricThatDoesNotExist(unittest.TestCase):
    """Direction A. A panel reading an absent series is blank and always was."""

    def setUp(self):
        self.emitted = _emitted_metrics()
        if not self.emitted:
            if os.environ.get("CI"):
                self.fail(
                    "the app's metrics could not be scraped in CI, so this "
                    "contract is not being checked at all. CI installs "
                    "prometheus_client and stubs the optional deps; something "
                    "changed."
                )
            self.skipTest(
                "the app's metrics could not be scraped here (needs "
                "prometheus_client and the optional deps). CI installs them, so "
                "this must not be skipped there."
            )

    def test_every_name_a_dashboard_reads_is_emitted(self):
        _, union = _dashboard_names()
        self.assertGreater(len(union), 20, "the extraction found almost nothing")
        dangling = sorted(n for n in union if not _matches(n, self.emitted))
        self.assertEqual(
            [], dangling,
            f"these dashboards read metrics the app does not emit, so those panels "
            f"render empty and always have: {dangling}. Either the metric was "
            "renamed or the panel is stale.",
        )


class TestNothingIsExportedForNothing(unittest.TestCase):
    """Direction B. A signal nobody reads is either dead weight or an unwired decision."""

    def setUp(self):
        self.emitted = _emitted_metrics()
        if not self.emitted:
            if os.environ.get("CI"):
                self.fail(
                    "the app's metrics could not be scraped in CI, so no orphan "
                    "is being detected. Nothing about this check is environment-"
                    "dependent once the dependencies are installed."
                )
            self.skipTest(
                "the app's metrics could not be scraped here; CI installs the "
                "dependencies and must not skip this."
            )

    def _consumers(self) -> set[str]:
        names: set[str] = set()
        for doc in _fixtures().values():
            blob = json.dumps(doc)
            names.update(_METRIC.findall(blob))
        for path in (_WATCHER, _OPS):
            names.update(_METRIC.findall(path.read_text(encoding="utf-8")))
        return names

    def test_every_orphan_is_known_and_allowlisted(self):
        consumers = self._consumers()
        orphans = sorted(
            n for n in self.emitted
            if n.startswith("battlebuddy_") and not _matches(n, consumers)
        )
        unlisted = [n for n in orphans if n not in KNOWN_ORPHANS]
        self.assertEqual(
            [], unlisted,
            f"newly orphaned metrics: {unlisted}. Either something reads them, or "
            "add them to KNOWN_ORPHANS with a reason — a silent addition is how "
            "the previous 35-ish untallied orphans accumulated.",
        )

    def test_the_allowlist_has_not_grown(self):
        """The ratchet. Fixing an orphan should delete an entry, not add one."""
        self.assertLessEqual(
            len(KNOWN_ORPHANS), ALLOWLIST_CEILING,
            f"KNOWN_ORPHANS grew from {ALLOWLIST_CEILING} to {len(KNOWN_ORPHANS)}. "
            "Every new entry should come with a consumer or a deletion, not a "
            "longer list of excuses.",
        )

    def test_every_allowlist_entry_carries_a_real_reason(self):
        for name, reason in KNOWN_ORPHANS.items():
            with self.subTest(name=name):
                self.assertRegex(
                    name, r"^battlebuddy_[a-z_0-9]+$",
                    "an allowlist key that is not a metric name is a typo",
                )
                self.assertGreater(
                    len(reason), 40,
                    f"{name} is allowlisted with no explanation. \"It is fine\" is "
                    "not a reason; say what it is for or wire it to something.",
                )

    def test_an_allowlisted_name_is_really_an_orphan(self):
        """Guards the guard: a fixed orphan left in the list would hide the next one."""
        consumers = self._consumers()
        for name in KNOWN_ORPHANS:
            if name in self.emitted:
                continue
            with self.subTest(name=name):
                self.assertFalse(
                    _matches(name, consumers),
                    f"{name} is allowlisted but something reads it now — delete "
                    "the entry so the ratchet tightens",
                )


class TestTheContractCheckCanFail(unittest.TestCase):
    """Witnesses. A guard never shown failing might have stopped guarding."""

    def test_a_dashboard_reading_an_absent_metric_is_detected(self):
        self.assertTrue(_matches("battlebuddy_x", {"battlebuddy_x"}))
        self.assertFalse(_matches("battlebuddy_x", {"battlebuddy_y"}))
        # Suffixed forms must match either way, since a panel may name the family.
        self.assertTrue(_matches("battlebuddy_x_bucket", {"battlebuddy_x"}))
        self.assertTrue(_matches("battlebuddy_x", {"battlebuddy_x_bucket"}))

    def test_a_name_outside_the_app_namespace_is_ignored(self):
        for name in ("node_cpu_seconds_total", "up", "grafana_"):
            self.assertFalse(name.startswith("battlebuddy_"))

    def test_the_extraction_finds_a_planted_metric(self):
        """The scraper must see a metric that is definitely in the fixtures."""
        _, union = _dashboard_names()
        self.assertIn("battlebuddy_transcription_success_ratio", union)

    def test_a_metric_in_a_nested_row_or_alert_is_found(self):
        """Rows nest panels, and alert conditions hold queries outside `targets`.

        Both are easy to forget, and a panel missed here means a contract that
        silently stops covering it.
        """
        names: set[str] = set()
        _walk([{
            "targets": [{"expr": "battlebuddy_direct"}],
            "panels": [{"targets": [{"expr": "battlebuddy_nested"}]}],
            "alert": {"conditions": [{"expr": "battlebuddy_alert"}]},
        }], names)
        self.assertEqual(
            {"battlebuddy_direct", "battlebuddy_nested", "battlebuddy_alert"}, names,
        )

    def test_metrics_outside_the_app_namespace_are_ignored(self):
        """The 35 Grafana Agent and node_exporter boards in this org share the
        stack, so a dashboard may legitimately query series that are not ours."""
        names: set[str] = set()
        _walk([{"targets": [{"expr": "node_cpu_seconds_total and up"}]}], names)
        self.assertEqual(set(), names)


class TestNoPanelIsSilentlyEmptyByLabelMismatch(unittest.TestCase):
    """Direction C. Valid PromQL whose two sides can never join renders nothing.

    The other two directions ask whether a metric name is emitted. This one asks
    whether the query can produce a value at all, which is the question that
    would have caught the detection-ratio panel on the day it was written rather
    than the day somebody noticed the graph was blank.
    """

    def test_no_dashboard_divides_an_aggregate_by_a_labelled_vector(self):
        offenders = []
        for name, doc in _fixtures().items():
            for panel, target in _panels_with_exprs(doc):
                expr = " ".join(target["expr"].split())
                if _SILENT_EMPTY.search(expr):
                    offenders.append(f"{name}: {panel.get('title')!r} -> {expr}")
        self.assertEqual(
            [], offenders,
            "these panels join an aggregate to a filtered vector with no on()/"
            "group_left or scalar(), so PromQL drops the result and they render "
            "empty forever: " + "; ".join(offenders),
        )

    def test_the_detector_catches_the_expression_that_was_shipped(self):
        """A regex nobody has seen fail is a regex nobody should trust."""
        self.assertTrue(_SILENT_EMPTY.search(
            "sum(battlebuddy_incidents_24h) / (battlebuddy_calls_24h > 0) * 100"))

    def test_the_three_accepted_fixes_are_not_flagged(self):
        for expr in (
            "sum(battlebuddy_incidents_24h) / on() group_left sum(battlebuddy_calls_24h) * 100",
            "sum(battlebuddy_incidents_24h) / scalar(battlebuddy_calls_24h) * 100",
            "100 * sum(battlebuddy_incidents_24h) / sum(battlebuddy_calls_24h)",
        ):
            with self.subTest(expr=expr):
                self.assertIsNone(_SILENT_EMPTY.search(expr))

    def test_an_ordinary_ratio_is_not_flagged(self):
        """The common shape — both sides aggregated — is the fix, not the bug."""
        for expr in (
            "battlebuddy_direct / battlebuddy_calls",
            "sum(rate(battlebuddy_errors_total[5m])) / sum(rate(battlebuddy_requests_total[5m]))",
        ):
            with self.subTest(expr=expr):
                self.assertIsNone(_SILENT_EMPTY.search(expr))


if __name__ == "__main__":
    unittest.main()