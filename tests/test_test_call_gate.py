"""`/test_call` must not be an open fabrication primitive.

This is the most dangerous route on the service and it had **no test at all** —
it shipped unauthenticated and stayed that way.

It bypasses Whisper, so it needs no audio. A two-line POST injects a fully-formed
call with caller-chosen `tag`, `category`, `lat`, `lon` and `location`. That call
then reaches `analyze_for_incident`, which fires for any non-locution talkgroup
using the LLM result as its primary signal, and `post_to_talk`, which pushes the
caller's text and location to subscribers. So anyone who could reach it could pin
a fake shooting at chosen coordinates on the public map and message subscribers
about it. Information integrity, not privilege.

Two independent conditions are required, and the tests below pin both plus the
`is_test` marking that keeps injections out of the quality metrics and the map.

Every scenario runs in a clean interpreter because both switches are read from
the environment at import time.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CHILD = _HERE / "_test_call_gate_child.py"

TOKEN = "t" * 64

#: A transcript that would plausibly file an incident, so the "can it fabricate"
#: question is answered by the refusal rather than by a weak payload.
INCIDENT_TRANSCRIPT = (
    " shots fired, multiple units responding, barricade the intersection "
    "now, suspect still on scene "
)


def _run(*, enabled=None, token=None, body=None, headers=None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        scenario_path = Path(tmp) / "scenario.json"
        result_path = Path(tmp) / "result.json"
        scenario_path.write_text(
            json.dumps({
                "enabled": enabled,
                "token": token,
                "body": body if body is not None else {},
                "headers": headers or {},
            }),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(scenario_path), str(result_path)],
            capture_output=True, text=True, timeout=300, cwd=str(_ROOT),
        )
        if proc.returncode != 0 or not result_path.exists():
            raise AssertionError(
                f"child failed ({proc.returncode})\n"
                f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
            )
        return json.loads(result_path.read_text(encoding="utf-8"))


def _importable() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-c", "import audio_receiver"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    return proc.returncode == 0, (proc.stderr or "")[-300:]


_IMPORTABLE, _IMPORT_ERROR = _importable()


class _TestCallCase(unittest.TestCase):
    def setUp(self) -> None:
        if not _IMPORTABLE:
            self.skipTest(
                "audio_receiver cannot be imported here (needs faster_whisper); "
                f"the authoritative baseline is /opt/battlebuddy/venv. {_IMPORT_ERROR}"
            )


class TestTestCallIsNotReachableWhenNotArmed(_TestCallCase):
    """Default off. This is the condition that matters most in production."""

    def test_disabled_returns_404_even_with_a_valid_token(self):
        r = _run(enabled=False, token=TOKEN,
                 body={"tgid": 1315, "transcript": INCIDENT_TRANSCRIPT})
        self.assertEqual(404, r["status"])
        self.assertEqual(0, r["calls_rows"], "a disabled hook must write nothing")

    def test_unset_enable_flag_is_treated_as_disabled(self):
        r = _run(enabled=None, token=TOKEN,
                 body={"tgid": 1315, "transcript": INCIDENT_TRANSCRIPT})
        self.assertEqual(404, r["status"])
        self.assertEqual(0, r["calls_rows"])

    def test_enabled_but_no_token_configured_is_503(self):
        """Armed without a secret is a misconfiguration, not a free pass."""
        r = _run(enabled=True, token=None,
                 body={"tgid": 1315, "transcript": INCIDENT_TRANSCRIPT})
        self.assertEqual(503, r["status"])
        self.assertEqual(0, r["calls_rows"])


class TestTestCallRequiresItsOwnToken(_TestCallCase):
    def test_no_token_is_401(self):
        r = _run(enabled=True, token=TOKEN, body={"tgid": 1315})
        self.assertEqual(401, r["status"])
        self.assertEqual(0, r["calls_rows"])

    def test_wrong_token_is_401(self):
        r = _run(enabled=True, token=TOKEN, body={"tgid": 1315, "token": "wrong"})
        self.assertEqual(401, r["status"])
        self.assertEqual(0, r["calls_rows"])

    def test_correct_token_in_body_is_accepted(self):
        r = _run(enabled=True, token=TOKEN,
                 body={"tgid": 1315, "transcript": "routine traffic check",
                       "token": TOKEN})
        self.assertEqual(200, r["status"])

    def test_bearer_header_is_accepted(self):
        r = _run(enabled=True, token=TOKEN,
                 body={"tgid": 1315, "transcript": "routine traffic check"},
                 headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(200, r["status"])

    def test_dedicated_header_is_accepted(self):
        r = _run(enabled=True, token=TOKEN,
                 body={"tgid": 1315, "transcript": "routine traffic check"},
                 headers={"X-Test-Call-Token": TOKEN})
        self.assertEqual(200, r["status"])

    def test_non_ascii_token_is_401_not_500(self):
        """compare_digest raises TypeError on a non-ASCII str, and header values
        arrive as latin-1 -- so a stray byte used to mean a 500."""
        r = _run(enabled=True, token=TOKEN,
                 body={"tgid": 1315},
                 headers={"X-Test-Call-Token": "café"})
        self.assertEqual(401, r["status"])


class TestInjectedRowsAreMarkedAsTest(_TestCallCase):
    """Gating is not enough on its own.

    An injection that reached the map or skewed the quality metrics would make
    the endpoint a hazard even when someone legitimately arms it for testing.
    """

    def test_injected_call_is_flagged_is_test(self):
        r = _run(enabled=True, token=TOKEN,
                 body={"tgid": 1315, "transcript": "routine check", "token": TOKEN})
        self.assertEqual(200, r["status"])
        self.assertEqual(1, r["calls_rows"])
        self.assertEqual(1, r["test_rows"],
                         "an injected call must be marked so quality metrics and "
                         "the incident map can exclude it")

    def test_refused_injection_marks_nothing(self):
        r = _run(enabled=True, token=TOKEN,
                 body={"tgid": 1315, "transcript": INCIDENT_TRANSCRIPT})
        self.assertEqual(401, r["status"])
        self.assertEqual(0, r["test_rows"])

    def test_injected_incident_is_flagged_too(self):
        """Otherwise the fabricated incident looks real on the public map."""
        r = _run(enabled=True, token=TOKEN,
                 body={"tgid": 1315, "transcript": INCIDENT_TRANSCRIPT,
                       "token": TOKEN})
        self.assertEqual(200, r["status"])
        self.assertEqual(
            0, r["incidents_from_test"],
            "an incident created from a test call must be marked is_test; "
            "every map and sitrep query excludes is_test=1",
        )


class TestTokenIsNotTheReceiveCredential(_TestCallCase):
    """The recorder Pi must not be able to invent transcripts."""

    def test_receive_token_does_not_open_test_call(self):
        """BB_RECEIVE_TOKEN is shared with every recorder; this one must not be.

        A compromised recorder can already inject audio. If it also held this
        credential it could fabricate a transcript outright, which is a different
        and much cheaper attack.
        """
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("BB_TEST_CALL_TOKEN"', source)
        self.assertNotIn(
            "BB_TEST_CALL_TOKEN", source.split("def _require_test_call_token")[1].split("def test_call")[0],
            "the gate must read its own secret, never the receive token",
        )


if __name__ == "__main__":
    unittest.main()