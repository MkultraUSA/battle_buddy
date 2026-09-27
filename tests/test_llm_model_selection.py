"""Regression tests for LLM model selection.

Three defects shipped together and silently killed the LLM enrichment feature:

1. ``OPENROUTER_MODEL`` was defined in ``modules/config.py`` and read by nothing, so the
   operator's configured model was dead config and every call went through the
   recommendations auto-picker instead.
2. ``_get_effective_model()`` fell back to the hardcoded id ``openai/gpt-oss-20b:free``,
   which is an OpenRouter-only model. Against a different OpenAI-compatible endpoint that
   id does not exist and every call returned HTTP 400.
3. ``_fetch_recommendations()`` called the recommendations host even when its URL was
   configured empty, logging ``unknown url type: ''`` on every call.

The endpoint moved to OpenCode Zen, so the tests here are provider-agnostic: they assert
that an explicitly configured model wins and that no provider-specific id is hardcoded.
"""

import sys
import types

import pytest

# `stripe` is not installed in this environment; modules.llm imports it.
sys.modules.setdefault("stripe", types.SimpleNamespace(api_key=None))

# tests/test_pi_watchdog.py installs a file-less modules.config stub into
# sys.modules at collection time, which shadows the real module and makes a
# module-level `from modules.config import ...` fail with "unknown location".
# Evict a stub that has no __file__; the real module always has one.
_config = sys.modules.get("modules.config")
if _config is not None and getattr(_config, "__file__", None) is None:
    del sys.modules["modules.config"]

from modules.config import OPENROUTER_MODEL  # noqa: E402


@pytest.fixture()
def llm(monkeypatch):
    """Import modules.llm with a controlled config surface."""
    import modules.llm as real

    for name in (
        "_recommendations_cache",
        "_recommendations_cache_ts",
    ):
        if hasattr(real, name):
            monkeypatch.setattr(real, name, {} if "cache" in name else 0.0)
    return real


def test_configured_model_wins(llm, monkeypatch):
    """An explicitly configured model must be returned verbatim."""
    monkeypatch.setattr(llm, "OPENROUTER_MODEL", "some-model-id")
    # Even if recommendations claim something else, the operator's choice stands.
    monkeypatch.setattr(llm, "_fetch_recommendations", lambda: {
        "recommendations": [{"status": "online", "supports_json": True,
                             "model_id": "auto-picked-model"}]
    })
    assert llm._get_effective_model() == "some-model-id"


def test_configured_model_wins_when_recommendations_fail(llm, monkeypatch):
    """A dead recommendations feed must never block a working configuration."""
    monkeypatch.setattr(llm, "OPENROUTER_MODEL", "some-model-id")
    monkeypatch.setattr(llm, "_fetch_recommendations", lambda: {})
    assert llm._get_effective_model() == "some-model-id"


def test_no_hardcoded_provider_specific_fallback(llm, monkeypatch):
    """The old OpenRouter-only fallback must not come back.

    It was the direct cause of HTTP 400s after the endpoint changed.
    """
    monkeypatch.setattr(llm, "OPENROUTER_MODEL", "")
    monkeypatch.setattr(llm, "_fetch_recommendations", lambda: {})
    assert llm._get_effective_model() == ""


def test_source_contains_no_baked_model_id(llm):
    """Guard against any hardcoded provider model id reappearing in the source."""
    import inspect

    source = inspect.getsource(llm._get_effective_model)
    for banned in ("gpt-oss-20b", ":free", "openai/gpt-"):
        assert banned not in source, (
            "hardcoded provider model id %r is back in _get_effective_model" % banned
        )


def test_empty_recommendations_url_makes_no_request(llm, monkeypatch):
    """An empty URL must short-circuit, not raise urllib 'unknown url type'."""
    monkeypatch.setattr(llm, "OPENROUTER_RECOMMENDATIONS_URL", "")
    assert llm._fetch_recommendations() == {}


def test_recommendations_are_still_used_when_unconfigured(llm, monkeypatch):
    """The auto-picker must keep working for operators who want it."""
    monkeypatch.setattr(llm, "OPENROUTER_MODEL", "")
    monkeypatch.setattr(llm, "_fetch_recommendations", lambda: {
        "recommendations": [{"status": "online", "supports_json": True,
                             "model_id": "auto-picked-model"}]
    })
    assert llm._get_effective_model() == "auto-picked-model"


def test_config_default_model_is_not_empty():
    """A deployment must have a model configured, or the LLM cannot work."""
    assert (OPENROUTER_MODEL or "").strip(), (
        "no default model in modules/config.py; _get_effective_model would return '' "
        "and every call would fail"
    )
