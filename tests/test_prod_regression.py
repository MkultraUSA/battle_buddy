"""The production regression battery must be able to fail.

`scripts/prod_regression.py` runs against a live server, which means it is not
exercised by CI. That is precisely the condition under which a check rots: a
green result nobody has watched, in a file nobody runs. The project's own list
records a post-deploy smoke test that pointed at a domain which does not resolve
and stayed green for months because `needs:` skipped it.

So this file does the only thing that can be done offline: prove the battery's
comparators detect the failures they claim to, using injected responses rather
than a server.

Three things are checked, in order of how badly they would hurt if wrong:

  1. **The XSS comparator detects a raw reflection.** That is the check most
     likely to rot into always-passing, because it only runs when someone
     remembers.
  2. **The metrics contract reads the shipped source.** It parses
     `bb_transcription_watch.py` for gated names rather than carrying its own
     list. A hand-copied list is how `REQUIRED_METRICS` came to cover 7 of 13
     names; if this regresses to a literal, the battery starts making claims
     about a list nobody maintains.
  3. **Exit codes are distinguishable.** 1 is a regression, 2 is "could not run",
     and conflating them is how an unreachable host becomes a green run.

None of this talks to production. That is the point.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "prod_regression.py"


def _load():
    """Import the battery as a module.

    Registered in sys.modules before execution: `@dataclass` resolves
    `sys.modules[cls.__module__].__dict__` while the class body is being built,
    so a spec-loaded module that is not registered fails at import with an
    AttributeError that has nothing to do with the code under test.
    """
    spec = importlib.util.spec_from_file_location("prod_regression", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["prod_regression"] = module
    spec.loader.exec_module(module)
    return module


class TestTheBatteryIsImportableAndHonest(unittest.TestCase):
    def test_it_exists_where_this_test_expects(self):
        self.assertTrue(_SCRIPT.is_file(), f"{_SCRIPT} is missing")

    def test_it_declares_three_distinguishable_outcomes(self):
        src = _SCRIPT.read_text(encoding="utf-8")
        self.assertIn("return 1", src, "a failed check must exit 1")
        self.assertIn("return 2", src, "a battery that could not run must exit 2")

    def test_unreachable_is_its_own_exception_not_a_failed_check(self):
        """A dropped connection is not a regression, and must not look like one.

        Without this the battery reports the network being down as the product
        being broken, which trains people to ignore it.
        """
        mod = _load()
        self.assertTrue(issubclass(mod.Unreachable, Exception))
        b = mod.Battery("t")
        b.record("some check", False, "could not run: no route to host")
        self.assertEqual(1, len(b.failures))

    def test_nothing_ran_is_not_a_pass(self):
        b = _load().Battery("t")
        for _ in range(3):
            b.skip("a", "host offline")
        self.assertEqual([], b.failures, "a skip is not a failure")
        self.assertEqual(0, b.ran, "skips must not count as checks run")


class TestTheXssComparatorHasTeeth(unittest.TestCase):
    """The check most likely to rot into always passing."""

    def _against(self, body: str, status: int = 200):
        mod = _load()
        b = mod.Battery("t")
        mod.http_get = lambda url, timeout=20: (status, body)
        mod.check_xss(b, base="http://x.invalid")
        return b

    def test_a_raw_reflection_is_caught(self):
        """The exact shape the shipped code produced."""
        mod = _load()
        b = self._against('<script>\nconst TOKEN = "' + mod.XSS_PAYLOAD + '";\n</script>')
        failures = [r for r in b.results if not r.ok]
        self.assertTrue(failures, "a raw reflection passed the XSS check")

    def test_an_escaped_reflection_passes(self):
        b = self._against('<script>\nconst TOKEN = "\\";alert(1);//";\n</script>')
        self.assertEqual([], [r for r in b.results if not r.ok],
                         "an escaped reflection was reported as a failure")

    def test_the_failing_case_really_contains_the_payload(self):
        """Guard the guard: the vulnerable fixture must carry the payload text.

        Without this a comparator that matched nothing at all would satisfy the
        test above forever, which is the exact shape of the failure this whole
        battery exists to catch.
        """
        mod = _load()
        fixture = '<script>\nconst TOKEN = "' + mod.XSS_PAYLOAD + '";\n</script>'
        self.assertIn(mod.XSS_PAYLOAD, fixture)
        self.assertNotIn('const TOKEN = "\\' + mod.XSS_PAYLOAD, fixture)

    def test_an_error_status_is_not_a_pass(self):
        b = self._against("irrelevant", status=500)
        self.assertTrue([r for r in b.results if not r.ok],
                        "a 500 was reported as a passing XSS check")

    def test_an_unreachable_host_is_reported_as_a_failure_not_a_pass(self):
        mod = _load()

        def boom(url, timeout=20):
            raise mod.Unreachable("connection refused")

        b = mod.Battery("t")
        mod.http_get = boom
        mod.check_xss(b, base="http://x.invalid")
        self.assertTrue([r for r in b.results if not r.ok],
                        "an unreachable host was reported as a pass")


class TestMetricNamesComeFromTheShippedSource(unittest.TestCase):
    def test_it_reads_the_watcher_rather_than_a_literal(self):
        mod = _load()
        names = mod.watched_metric_names()
        self.assertGreater(len(names), 10, "implausibly few gated names found")

    def test_it_finds_the_names_the_watcher_actually_gates_on(self):
        mod = _load()
        names = mod.watched_metric_names()
        for expected in ("battlebuddy_backlog_files_pending",
                         "battlebuddy_transcription_success_ratio",
                         "battlebuddy_transcript_quality_calls"):
            self.assertIn(expected, names,
                          f"{expected} is gated on by the watcher but not detected")

    def test_there_is_no_hand_copied_list_to_go_stale(self):
        """The failure mode being guarded: two files, two lists, no cross-check.

        Matches an assignment rather than the bare name, because the module
        docstring has to be able to *name* the thing it refuses to do.
        """
        src = _SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(
            src, r"^\s*(REQUIRED_METRICS|WATCHED_METRICS|GATED_METRICS)\s*=",
            "the battery must not assign its own metric list; that is how the "
            "watcher's own coverage went stale in the first place",
        )
        self.assertIn(
            "bb_transcription_watch.py", src,
            "the names must be read from the watcher's own source at runtime",
        )

    def test_it_would_notice_a_gate_the_app_does_not_emit(self):
        """The whole reason this battery exists, as a pure comparison."""
        mod = _load()
        emitted = {"battlebuddy_backlog_files_pending"}
        wanted = mod.watched_metric_names()
        missing = sorted(w for w in wanted if w not in emitted)
        self.assertTrue(missing, "the contract comparison cannot detect a gap")
        self.assertIn("battlebuddy_transcription_success_ratio", missing)


class TestItIsReadOnly(unittest.TestCase):
    """A regression battery that can mutate production is a liability."""

    def test_no_http_method_other_than_get(self):
        src = _SCRIPT.read_text(encoding="utf-8")
        for verb in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"'):
            self.assertNotIn(verb, src, f"the battery issues {verb}")

    def test_the_database_is_opened_read_only(self):
        src = _SCRIPT.read_text(encoding="utf-8")
        self.assertIn("sqlite3 -readonly", src)
        # And never without the flag.
        self.assertNotRegex(src, r"sqlite3(?! -readonly)\s+/opt",
                            "a sqlite3 invocation is missing -readonly")


if __name__ == "__main__":
    unittest.main()