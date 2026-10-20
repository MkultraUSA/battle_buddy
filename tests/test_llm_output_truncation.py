"""A cut-off LLM reply must not be retried, and the completion budget must be real.

`_call_openrouter_llm` sent max_tokens=300. The model's reasoning tokens count
against that budget, so long answers were cut off mid-JSON. The parse failed and
the identical request was sent again, up to three times. The fix raises the
budget and stops retrying a reply that stopped at the limit. A genuine
malformed reply that finished normally must still be retried.
"""

import json
import sys
import types

import pytest

# `stripe` is not installed in this environment; modules.llm imports it.
sys.modules.setdefault("stripe", types.SimpleNamespace(api_key=None))

# tests/test_pi_watchdog.py can install a file-less modules.config stub; evict it.
_config = sys.modules.get("modules.config")
if _config is not None and getattr(_config, "__file__", None) is None:
    del sys.modules["modules.config"]


class _FakeResponse:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def llm(monkeypatch):
    import modules.llm as real

    monkeypatch.setattr(real, "_get_effective_model", lambda: "test/reasoning-model")
    monkeypatch.setattr(real, "_runtime_bans", {})
    monkeypatch.setattr(real, "_ban_offenses", {})
    monkeypatch.setattr(real, "_model_health", {})
    return real


@pytest.fixture()
def fake_api(llm, monkeypatch):
    state = {"requests": [], "sleeps": [], "responses": []}

    def fake_urlopen(req, timeout=None):
        state["requests"].append(json.loads(req.data))
        return _FakeResponse(state["responses"].pop(0))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", state["sleeps"].append)
    return state


def _completion(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def test_request_asks_for_the_larger_budget(llm, fake_api):
    fake_api["responses"].append(_completion('{"incident_type": "ROUTINE"}'))

    assert llm._call_openrouter_llm("sys", "user") == {"incident_type": "ROUTINE"}
    assert fake_api["requests"][0]["max_tokens"] == 1200
    assert llm._LLM_MAX_TOKENS == 1200


def test_truncated_reply_is_not_retried(llm, fake_api):
    fake_api["responses"].append(_completion('{"incident_type": "ROUT', finish_reason="length"))

    with pytest.raises(llm._OutputTruncated):
        llm._call_openrouter_llm("sys", "user")
    assert len(fake_api["requests"]) == 1
    assert fake_api["sleeps"] == []


def test_truncated_reply_does_not_penalise_the_model(llm, fake_api):
    fake_api["responses"].append(_completion(None, finish_reason="length"))

    with pytest.raises(llm._OutputTruncated):
        llm._call_openrouter_llm("sys", "user")
    assert llm._runtime_bans == {}
    assert llm._model_health.get("test/reasoning-model", {}).get("fail", 0) == 0


def test_malformed_but_finished_reply_is_still_retried(llm, fake_api):
    for _ in range(3):
        fake_api["responses"].append(_completion("not json", finish_reason="stop"))

    with pytest.raises(json.JSONDecodeError):
        llm._call_openrouter_llm("sys", "user")
    assert len(fake_api["requests"]) == 3
    assert len(fake_api["sleeps"]) == 2
