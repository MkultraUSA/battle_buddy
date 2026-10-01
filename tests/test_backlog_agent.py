"""Contract tests for scripts/backlog_agent.py.

The worker is the other half of the backlog and it had drifted from the server's
contract. Two of these tests exist for bugs that were live in the old version:

  * an empty transcript was reported with the RETRY action. The server releases
    the lease on retry, so the identical clip was claimed again, forever -- a
    poison item starving everything behind it.
  * /complete was sent only {item_id, transcript}. The server reads tgid, tag,
    category, node and duration from the body, defaulting to tgid 0 and no
    coordinates, so every backlogged call was filed as category "Unknown" at
    default downtown coordinates.

Both are behavioural, not shape, so these drive the real functions and capture
what would go over the wire.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
_AGENT_PATH = _ROOT / "scripts" / "backlog_agent.py"


def _load_agent(transcribe_result=("", 0.0)):
    """Import the worker with a stubbed transcription backend.

    modules.transcription pulls in faster_whisper, which most environments
    without the production venv do not have. The stub is removed again in
    tearDown so a real import elsewhere is not poisoned.
    """
    had = "modules.transcription" in sys.modules
    saved = sys.modules.get("modules.transcription")
    stub = mock.MagicMock()
    stub.transcribe = mock.MagicMock(return_value=transcribe_result)
    sys.modules["modules.transcription"] = stub

    spec = importlib.util.spec_from_file_location("_bb_agent_under_test", _AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._pytest_restore = (had, saved)
    module._pytest_stub = stub
    return module


def _restore(module):
    had, saved = module._pytest_restore
    if had:
        sys.modules["modules.transcription"] = saved
    else:
        sys.modules.pop("modules.transcription", None)


ITEM = {
    "id": "item-1",
    "audio_b64": "SEVMTG8=",   # "HELLO"
    "tgid": 12345,
    "tag": "APD-DISPATCH",
    "category": "Police Dispatch",
    "node": "pi5",
    "duration": 4.5,
    "received_ts": 1.0,
}


class TestBacklogAgentContract(unittest.TestCase):
    def tearDown(self):
        module = getattr(self, "_module", None)
        if module is not None:
            _restore(module)

    def _agent(self, transcript=("", 0.0)):
        self._module = _load_agent(transcript)
        self.sent: list[tuple[str, dict]] = []
        self._module.api_request = lambda path, payload: self.sent.append((path, payload)) or {}
        return self._module

    # --- the two live bugs ------------------------------------------------

    def test_empty_transcript_is_completed_not_retried(self):
        """A retry would release the lease and re-claim the same silence forever."""
        agent = self._agent(("", 0.0))
        agent.handle_one(dict(ITEM))

        path, payload = self.sent[-1]
        self.assertEqual("/api/backlog/complete", path)
        self.assertEqual(
            "complete", payload["action"],
            "an empty transcript must be completed so the server DISCARDS the "
            "clip; 'retry' releases the lease and re-claims it indefinitely",
        )
        self.assertEqual("", payload["transcript"])

    def test_completion_echoes_the_calls_metadata(self):
        """Without this every backlogged call is filed Unknown, at default coords."""
        agent = self._agent(("Engine 12 en route to a structure fire.", 0.91))
        agent.handle_one(dict(ITEM))

        _, payload = self.sent[-1]
        self.assertEqual(12345, payload["tgid"])
        self.assertEqual("APD-DISPATCH", payload["tag"])
        self.assertEqual("Police Dispatch", payload["category"])
        self.assertEqual("pi5", payload["node"])
        self.assertEqual(4.5, payload["duration"])
        self.assertEqual(0.91, payload["accuracy"])
        self.assertEqual("item-1", payload["item_id"])

    # --- lease and recovery ----------------------------------------------

    def test_transcription_failure_returns_the_item(self):
        """A crash on our side must hand the clip back, not strand it.

        Recovery lives in main()'s loop rather than handle_one, so drive the
        loop: one claim, then the worker goes idle and we break out via sleep.
        """
        agent = self._agent()
        # Patch the module's own `transcribe`, not the stub's attribute: the
        # worker does `from modules.transcription import transcribe`, so the name
        # is bound in its namespace at import time and the stub is irrelevant
        # afterwards.
        agent.transcribe = mock.MagicMock(side_effect=RuntimeError("model lock"))
        agent.BB_TOKEN = "test-token"

        class _Stop(Exception):
            pass

        with mock.patch.object(agent, "claim_one", side_effect=[dict(ITEM), None]), \
             mock.patch.object(agent.time, "sleep", side_effect=_Stop):
            with self.assertRaises(_Stop):
                agent.main()

        path, payload = self.sent[-1]
        self.assertEqual("/api/backlog/complete", path)
        self.assertEqual(
            "retry", payload["action"],
            "if transcription crashes we never learned anything about the audio, "
            "so the clip must go back for another attempt",
        )
        self.assertEqual("item-1", payload["item_id"])

    def test_long_transcription_warns_about_the_lease(self):
        agent = self._agent(("hello", 0.5))
        agent.LEASE_SECONDS = 0  # any elapsed time exceeds this
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            agent.handle_one(dict(ITEM))
        self.assertIn("longer than", buf.getvalue())


class TestBacklogAgentClaimBehaviour(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "_module", None) is not None:
            _restore(self._module)

    def test_no_work_is_not_an_error(self):
        module = self._module = _load_agent()
        module.api_request = lambda path, payload: {"ok": True, "item": None, "status": "no_work"}
        self.assertIsNone(module.claim_one())

    def test_claim_returns_the_item(self):
        module = self._module = _load_agent()
        module.api_request = lambda path, payload: {"ok": True, "item": dict(ITEM), "status": "ok"}
        self.assertEqual("item-1", module.claim_one()["id"])

    def test_api_request_classifies_401_and_503_as_fatal(self):
        """Pins the classification itself.

        An earlier version of this test patched `claim_one` to raise
        AuthFailure directly, so it never went through api_request -- and a
        mutation that stopped classifying 401 as fatal passed it. Drive
        api_request against a real HTTPError instead.
        """
        import urllib.error
        module = self._module = _load_agent()

        for code in (401, 503):
            with self.subTest(code=code):
                def _raise(*_a, **_kw):
                    raise urllib.error.HTTPError(
                        module.BB_BASE_URL, code, "err", {}, None
                    )
                with mock.patch.object(module.urllib.request, "urlopen", _raise):
                    with self.assertRaises(module.AuthFailure):
                        module.api_request("/api/backlog/claim", {})

    def test_api_request_does_not_treat_server_faults_as_auth_problems(self):
        """A 500 is retryable; it must not kill a healthy worker."""
        import urllib.error
        module = self._module = _load_agent()

        def _raise(*_a, **_kw):
            raise urllib.error.HTTPError(module.BB_BASE_URL, 500, "err", {}, None)

        with mock.patch.object(module.urllib.request, "urlopen", _raise):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                module.api_request("/api/backlog/claim", {})
        self.assertNotIsInstance(ctx.exception, module.AuthFailure)

    def test_auth_failure_is_fatal_not_a_silent_idle(self):
        """A misconfigured token must not look like a healthy idle worker.

        401 and 503 both need a human: retrying cannot fix a wrong token or a
        server with no secret configured.
        """
        import urllib.error
        module = self._module = _load_agent()

        def _refuse(path, payload):
            raise urllib.error.HTTPError(
                module.BB_BASE_URL, 401, "Unauthorized", {}, None
            )
        module.api_request = _refuse
        # main() refuses to start without a token (exit 2); this test is about
        # what happens once a worker IS running and the server rejects it.
        module.BB_TOKEN = "test-token"

        with mock.patch.object(module, "claim_one", side_effect=module.AuthFailure("401")):
            self.assertEqual(3, module.main())


if __name__ == "__main__":
    unittest.main()