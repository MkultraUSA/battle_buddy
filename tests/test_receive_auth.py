"""`/receive` must not be an unauthenticated public ingest.

Port 9001 binds 0.0.0.0 and is reachable from the public internet (verified by
TCP connect), and nginx adds no `auth_request` and no rate limit to its
`location /receive` block. Before this gate, anyone who could reach the host
could POST base64 audio and have it:

  * transcribed by Whisper, burning the VPS CPU;
  * stored as a call in the public-safety database;
  * passed to `llm_analyze`, spending LLM credits;
  * run through `analyze_for_incident`, **creating an incident** that pins itself
    on the public map;
  * pushed to subscribers as an outbound Nextcloud Talk alert by `post_to_talk`;
  * and able to queue OP25 hold/skip commands back to the recorder Pi.

For a product whose entire value is trustworthy public-safety information, being
able to place a fake shooting on the map is an information-integrity failure,
not a privilege one. This is finding C4 in the 2026-09-25 audit slate.

Each scenario runs in a clean subprocess (`_receive_auth_child.py`) so that the
route under test is the real `audio_receiver.receive`, not a stub: importing it
requires `faster_whisper`, which several suites install as a `sys.modules` stub
at collection time and never remove.

The scenarios deliberately send an EMPTY body. That proves the gate was passed
(authenticated) while writing nothing: no audio, no Whisper call, no LLM call,
no database row. `calls_rows` is asserted to be 0 on every path, so a test can
never pass by having quietly done real work.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_CHILD = _HERE / "_receive_auth_child.py"

TOKEN = "test-receive-token-0123456789abcdef"


class Result:
    def __init__(self, raw: dict) -> None:
        self.status: int = raw["status"]
        self.body = raw["body"]
        self.calls_rows: int = raw["calls_rows"]


def run(*, token: str | None = None, headers: dict | None = None,
        body: dict | None = None) -> Result:
    """Drive one /receive request in a fresh interpreter."""
    scenario = {"token": token, "headers": headers or {}, "body": body or {}}
    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "scenario.json"
        out_path = Path(tmp) / "result.json"
        in_path.write_text(json.dumps(scenario), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(_CHILD), str(in_path), str(out_path)],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"child failed rc={proc.returncode}\n"
                f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-4000:]}"
            )
        return Result(json.loads(out_path.read_text(encoding="utf-8")))


def _audio_receiver_importable() -> tuple[bool, str]:
    """Can this interpreter really import audio_receiver?

    The suite skips rather than passes when it cannot, so an environment missing
    faster_whisper never reports a green run it did not earn. On the production
    venv this is always true, and that is the authoritative baseline.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import audio_receiver"],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=300,
    )
    return probe.returncode == 0, (probe.stderr or "")[-500:]


_IMPORTABLE, _IMPORT_ERROR = _audio_receiver_importable()


class _ReceiveAuthCase(unittest.TestCase):
    """Base case so EVERY scenario skips together when the route is unimportable.

    Per-class decoration was wrong here: it left two of three classes running,
    which turned an environment limitation into five red tests rather than an
    honest skip.
    """

    def setUp(self) -> None:
        if not _IMPORTABLE:
            self.skipTest(
                "audio_receiver cannot be imported here (needs faster_whisper); "
                "the authoritative baseline is /opt/battlebuddy/venv on the VPS. "
                f"{_IMPORT_ERROR}"
            )


class TestReceiveRequiresAToken(_ReceiveAuthCase):
    """The gate fails closed and writes nothing on any rejection path."""

    def test_unconfigured_server_refuses_all_ingest(self):
        """No BB_RECEIVE_TOKEN -> 503, so a missing secret is loud, not silent."""
        r = run(token=None, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status, 503)
        self.assertIn("not configured", (r.body or {}).get("error", ""))
        self.assertEqual(r.calls_rows, 0)

    def test_empty_token_is_treated_as_unconfigured(self):
        """A whitespace-only secret must not become a bypassable empty token."""
        r = run(token="   ", headers={"Authorization": "Bearer   "})
        self.assertEqual(r.status, 503)
        self.assertEqual(r.calls_rows, 0)

    def test_missing_credential_is_rejected(self):
        r = run(token=TOKEN, headers={})
        self.assertEqual(r.status, 401)
        self.assertEqual((r.body or {}).get("error"), "unauthorized")
        self.assertEqual(r.calls_rows, 0)

    def test_wrong_token_is_rejected(self):
        r = run(token=TOKEN, headers={"Authorization": "Bearer not-the-token"})
        self.assertEqual(r.status, 401)
        self.assertEqual(r.calls_rows, 0)

    def test_empty_bearer_is_rejected_rather_than_matching_empty(self):
        r = run(token=TOKEN, headers={"Authorization": "Bearer "})
        self.assertEqual(r.status, 401)
        self.assertEqual(r.calls_rows, 0)

    def test_token_is_not_accepted_as_a_query_parameter(self):
        """Only the header counts; a token in a URL lands in access logs."""
        r = run(token=TOKEN, body={"token": TOKEN})
        self.assertEqual(r.status, 401)
        self.assertEqual(r.calls_rows, 0)


class TestReceiveAcceptsAValidCredential(_ReceiveAuthCase):
    """A correct credential passes the gate. An empty body then fails validation,
    which is how we observe success with zero side effects."""

    def test_bearer_token_passes_the_gate(self):
        r = run(token=TOKEN, headers={"Authorization": f"Bearer {TOKEN}"})
        # 400 missing audio_b64 == authentication succeeded, then body validation.
        self.assertEqual(r.status, 400)
        self.assertEqual((r.body or {}).get("error"), "missing audio_b64")
        self.assertEqual(r.calls_rows, 0, "an empty body must never write a call")

    def test_x_receive_token_header_is_accepted(self):
        """Documented alternative for clients that cannot set Authorization."""
        r = run(token=TOKEN, headers={"X-Receive-Token": TOKEN})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.calls_rows, 0)

    def test_bearer_is_case_sensitive_on_the_scheme(self):
        """Only `Bearer ` is stripped, so `bearer x` is compared literally."""
        r = run(token=TOKEN, headers={"Authorization": f"bearer {TOKEN}"})
        self.assertEqual(r.status, 401)
        self.assertEqual(r.calls_rows, 0)


class TestGateRunsBeforeAnyWork(_ReceiveAuthCase):
    """Authentication must precede parsing, storage, Whisper, LLM and alerting."""

    def test_rejection_happens_before_body_is_inspected(self):
        """A body that would otherwise be valid is still refused on auth."""
        valid_audio = {
            "audio_b64": "UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=",
            "tgid": 1487,
            "tag": "APD",
            "node": "attacker",
        }
        r = run(token=TOKEN, headers={"Authorization": "Bearer wrong"}, body=valid_audio)
        self.assertEqual(r.status, 401)
        self.assertEqual(
            r.calls_rows, 0,
            "a rejected request must not reach insert_call",
        )

    def test_rejection_precedes_the_body_required_check(self):
        """The 401 must come first, so an unauthenticated caller learns nothing
        about whether their payload was well-formed."""
        r = run(token=TOKEN, headers={}, body={})
        self.assertEqual(r.status, 401)


if __name__ == "__main__":
    unittest.main()