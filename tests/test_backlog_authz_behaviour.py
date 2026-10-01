"""Behavioural tests for the /api/backlog worker endpoints.

These assert executed behaviour, not source shape. The AST tests in this file
cover the static wiring; this class runs the real Flask app in a subprocess
(see _backlog_authz_child.py) because shape-level assertions let three genuine
defects through to production:

  * `if checked is None: return checked` -- inverted. The guard returns None on
    SUCCESS, so an unauthenticated caller had their refusal ignored and received
    200, while a valid caller got a 500. The endpoints were no more protected
    than before the guard was added.
  * The guard ended in `return data`, so Flask jsonified the parsed request body
    and echoed the caller's token back in the response.
  * An assertion checked `Return.value is None` when `return None` actually
    parses to Constant(None), failing against correct code.

The property that matters most is not the status code but what an unauthorised
caller can cause: `complete` runs insert_call -> llm_analyze ->
analyze_for_incident -> post_to_talk, so an open endpoint lets a stranger write
a call row, spend an LLM call, file an incident and post to Talk. Each refusal
case below therefore asserts the database is untouched and the queue is intact,
not merely that the response was 401.
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
_CHILD = _HERE / "_backlog_authz_child.py"


def _audio_receiver_importable() -> tuple[bool, str]:
    """Can this interpreter really import audio_receiver?

    The suite skips rather than passes when it cannot, so an environment missing
    faster_whisper never reports a green run it did not earn. On the production
    venv this is always true, and that is the authoritative baseline. Mirrors
    test_receive_auth.py.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import audio_receiver"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    return probe.returncode == 0, (probe.stderr or "")[-500:]


_IMPORTABLE, _IMPORT_ERROR = _audio_receiver_importable()


class _BacklogAuthzCase(unittest.TestCase):
    """Base case so EVERY scenario skips together when the route is unimportable.

    Per-class decoration was wrong in test_receive_auth.py: it left two of three
    classes running, turning an environment limitation into five red tests
    rather than an honest skip.
    """

    def setUp(self) -> None:
        if not _IMPORTABLE:
            self.skipTest(
                "audio_receiver cannot be imported here (needs faster_whisper); "
                "the authoritative baseline is /opt/battlebuddy/venv on the VPS. "
                f"{_IMPORT_ERROR}"
            )

# A payload that would do real damage if it reached the handler: it inserts a
# call row, runs an LLM call, and files an incident.
_FABRICATED = {
    "item_id": "attacker-supplied",
    "tgid": 12345,
    "tag": "seeding",
    "transcript": "Structure fire, 100 block of Main St, second engine staging.",
    "node": "pie3",
    "duration": 3.0,
}


def _run(route: str, *, token=None, body=None, headers=None):
    """Run one scenario in a clean interpreter; returns the child's JSON result."""
    if body is None:
        body = dict(_FABRICATED) if route == "complete" else {}
    with tempfile.TemporaryDirectory() as tmp:
        scenario_path = Path(tmp) / "scenario.json"
        result_path = Path(tmp) / "result.json"
        scenario_path.write_text(
            json.dumps(
                {
                    "route": route,
                    "token": token,
                    "body": body,
                    "headers": headers or {},
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(scenario_path), str(result_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0 or not result_path.exists():
            raise AssertionError(
                f"child failed for route={route} token={token!r}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return json.loads(result_path.read_text(encoding="utf-8"))


class TestBacklogEndpointsRefuseUnauthenticatedCallers(_BacklogAuthzCase):
    """No token configured is a 503, not an open door.

    Deliberately stricter than queueing, which is opt-in and harmless when off:
    these endpoints mutate incident state, so an unset secret must refuse.
    """

    def test_unset_secret_refuses_claim(self):
        r = _run("claim", token=None)
        self.assertEqual(503, r["status"])
        self.assertEqual(1, r["queue_depth"], "refused claim must not drain the queue")

    def test_unset_secret_refuses_complete(self):
        r = _run("complete", token=None)
        self.assertEqual(503, r["status"])
        self.assertEqual(0, r["calls_rows"], "refused complete must not write a call")

    def test_missing_token_refuses_claim(self):
        r = _run("claim", token="s" * 64, body={})
        self.assertEqual(401, r["status"])
        self.assertEqual(1, r["queue_depth"])

    def test_missing_token_refuses_complete(self):
        r = _run("complete", token="s" * 64, body={})
        self.assertEqual(401, r["status"])
        self.assertEqual(0, r["calls_rows"])

    def test_wrong_token_refuses_complete(self):
        r = _run("complete", token="s" * 64, body={"token": "wrong"})
        self.assertEqual(401, r["status"])
        self.assertEqual(0, r["calls_rows"], "a wrong token must not fabricate a call")

    def test_non_ascii_token_is_401_not_500(self):
        """compare_digest raises TypeError on a non-ASCII str.

        Header values arrive as latin-1, so a stray byte used to turn an auth
        failure into a 500. The guard compares encoded bytes.
        """
        r = _run("complete", token="s" * 64, headers={"X-Backlog-Token": "café"})
        self.assertEqual(401, r["status"])
        self.assertNotEqual(500, r["status"])


class TestBacklogEndpointsAcceptAValidWorker(_BacklogAuthzCase):
    def test_valid_token_reaches_claim(self):
        r = _run("claim", token="s" * 64, body={"token": "s" * 64})
        self.assertEqual(200, r["status"])
        self.assertEqual("ok", (r["body"] or {}).get("status"))
        self.assertEqual(0, r["queue_depth"], "an authorised claim does drain the queue")

    def test_valid_token_via_bearer_reaches_complete(self):
        # Empty transcript: the handler returns early as "empty", so the test
        # proves the credential was accepted without spending an LLM call.
        r = _run(
            "complete",
            token="s" * 64,
            body={"item_id": "x", "transcript": ""},
            headers={"Authorization": "Bearer " + "s" * 64},
        )
        self.assertEqual(200, r["status"])


class TestBacklogTokenIsNeverEchoed(_BacklogAuthzCase):
    """The guard must return None on success, not the parsed request body.

    Returning `data` made Flask jsonify the request body, so a worker that
    supplied the token in the body received it straight back in the response.
    """

    SECRET = "c" * 64

    def test_token_in_body_is_not_reflected(self):
        r = _run("claim", token=self.SECRET, body={"token": self.SECRET})
        self.assertEqual(200, r["status"])
        self.assertNotIn(self.SECRET, r["raw_body"])

    def test_token_in_body_is_not_reflected_on_complete(self):
        r = _run(
            "complete",
            token=self.SECRET,
            body={"token": self.SECRET, "item_id": "x", "transcript": ""},
        )
        self.assertEqual(200, r["status"])
        self.assertNotIn(self.SECRET, r["raw_body"])


if __name__ == "__main__":
    unittest.main()