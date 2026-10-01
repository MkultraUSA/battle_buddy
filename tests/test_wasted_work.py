"""Stop paying to transcribe silence, and stop queueing audio nobody will fetch.

Two measured problems in production, both from the same root cause: audio is
accepted, then mishandled on the way through.

1. **Whisper's stock output on non-speech reaches the LLM.** Over 24 hours, 191 of
   703 LLM calls (27.1%) were spent on transcripts the model itself described as
   "garbled" or "unintelligible". Every one of those was classified
   `ROUTINE pri=NONE` -- none produced an incident. Measured top offenders over
   3 days: "Thank you." x396, "You" x288, "." x257, "10-4." x65, dot-runs x52.

2. **The backlog queue has no consumer.** `BB_BACKLOG_AGENT_TOKEN` is set, so a
   worker is *authorised* to claim items, but none is deployed, and
   `battlebuddy_backlog_completed_total` stayed at 0. Items sit in an in-memory
   deque until they are lost on restart. Worse, a stuck queue pins
   `_should_backlog()` in its aggressive band: at depth 92 it returned
   `random() > 0.35`, continuously shedding 35% of every call that could not get
   a process slot. That is a live ingest defect, not just wasted memory.

These tests pin both. They are deliberately cheap: no Flask app, no network.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


class TestNonSpeechDetection(unittest.TestCase):
    """`_looks_like_nonspeech` must catch filler without catching real traffic."""

    def _predicate(self):
        import sys
        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))
        from modules.llm import _looks_like_nonspeech
        return _looks_like_nonspeech

    def test_punctuation_only_is_nonspeech(self):
        f = self._predicate()
        for junk in (".", ". . . . . .", "...", "   ", "\n", "- - -", "10-4."):
            with self.subTest(text=junk):
                self.assertTrue(f(junk), f"{junk!r} is Whisher filler")

    def test_whisper_stock_phrases_are_nonspeech(self):
        f = self._predicate()
        for junk in ("Thank you.", "You", "you", "Thank you. Thank you.",
                     "Never.", "Okay.", "Careful.", "Go ahead.", "Bye."):
            with self.subTest(text=junk):
                self.assertTrue(f(junk), f"{junk!r} should be skipped")

    def test_real_radio_traffic_is_not_flagged(self):
        f = self._predicate()
        real = [
            "Engine 6 responding to the structure fire on Burnet Road, requesting a second alarm.",
            "10-52 is subject in custody, transporting to county jail.",
            "Medic 12 in service, traffic stop at 12th and Lamar.",
            "Supervisor, we have an officer involved shooting, 4000 block of",
            "Be advised, the scene is established, command post at the corner.",
            # a real call that happens to open with filler must still pass
            "Okay. Engine 6 is on scene, heavy smoke showing from the second floor.",
            "Yeah, dispatch shows the warrant check is clear.",
        ]
        for text in real:
            with self.subTest(text=text[:44]):
                self.assertFalse(f(text), f"{text!r} is real traffic and must reach the LLM")

    def test_a_lone_common_word_is_not_enough_to_skip(self):
        """'Okay' alone is filler; 'Okay, we have a second alarm on the fire' is not."""
        f = self._predicate()
        self.assertTrue(f("Okay."))
        self.assertFalse(f("Okay, we have a second alarm on the fire."))


class TestLlmCallIsGated(unittest.TestCase):
    """The predicate must be wired into llm_analyze, not merely defined."""

    def test_llm_analyze_returns_before_calling_the_model(self):
        tree = ast.parse((_ROOT / "modules" / "llm.py").read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "llm_analyze")
        called = {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertIn("_looks_like_nonspeech", called,
                      "llm_analyze never consults the non-speech predicate")
        # it must be an early guard: appear before any outbound call
        src = ast.unparse(fn)
        self.assertLess(
            src.index("_looks_like_nonspeech"), src.index("_llm_backoff_until =")
            if "_llm_backoff_until =" in src else len(src),
        )


class TestBacklogIsOptIn(unittest.TestCase):
    """Queueing must not happen unless a worker exists to drain the queue.

    An earlier version of these assertions grepped the function's source for
    the flag name -- and passed with the gate deleted, because the name also
    appears in the docstring. Asserting on prose is asserting on nothing. These
    inspect the executable AST instead, with the docstring excluded.
    """

    def _fn(self):
        tree = ast.parse((_ROOT / "audio_receiver.py").read_text())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_should_backlog")

    def _code_without_docstring(self):
        fn = self._fn()
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]          # drop the docstring
        return body

    def test_function_has_an_executable_flag_guard(self):
        found = False
        for node in self._code_without_docstring():
            if not isinstance(node, ast.If):
                continue
            if "_BACKLOG_ENABLED" not in ast.unparse(node.test):
                continue
            # the guarded branch must actually refuse
            returns_false = any(
                isinstance(inner, ast.Return)
                and isinstance(inner.value, ast.Constant)
                and inner.value.value is False
                for inner in ast.walk(node)
            )
            if returns_false:
                found = True
        self.assertTrue(
            found,
            "_should_backlog has no executable `if not _BACKLOG_ENABLED: return False` "
            "guard. Without it the queue is filled by a worker that does not exist, "
            "and its depth pins the throttle into its aggressive band.",
        )

    def test_flag_guard_precedes_depth_throttling(self):
        """The property is ORDER, not mechanism.

        This used to look for `ast.With` containing "depth", which asserted the
        shape of the old in-memory implementation (`with _backlog_lock: depth =
        len(_backlog_queue)`). The durable queue reads depth through a helper
        call instead, so the With-based check failed on correct code -- the same
        trap that let an inverted guard ship twice.

        What actually matters: the enable check must run before anything reads
        queue depth, so that with queueing switched off we never touch the store.
        """
        body = self._code_without_docstring()
        guard_idx = depth_idx = None
        for i, node in enumerate(body):
            src = ast.unparse(node)
            if isinstance(node, ast.If) and "_BACKLOG_ENABLED" in src:
                guard_idx = i
            # Any statement that reads depth, by whatever mechanism.
            if "_backlog_depth" in src or (
                isinstance(node, ast.With) and "depth" in src
            ):
                depth_idx = i
        self.assertIsNotNone(guard_idx, "no flag guard found")
        self.assertIsNotNone(
            depth_idx,
            "no depth throttle found: _should_backlog must consult queue depth",
        )
        self.assertLess(
            guard_idx, depth_idx,
            "the enable check must come before the depth throttle, otherwise a "
            "stuck queue still sheds audio while queueing is switched off",
        )

    def test_flag_defaults_to_off(self):
        tree = ast.parse((_ROOT / "audio_receiver.py").read_text())
        assigns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(getattr(x, "id", None) == "_BACKLOG_ENABLED" for x in n.targets)
        ]
        self.assertEqual(len(assigns), 1, "_BACKLOG_ENABLED should be assigned once")
        src = ast.unparse(assigns[0])
        self.assertIn("BB_BACKLOG_ENABLED", src)
        self.assertRegex(src, r"'1'|'true'|'yes'|'on'",
                         "it must be an explicit opt-in list, not a default-on flag")


if __name__ == "__main__":
    unittest.main()
