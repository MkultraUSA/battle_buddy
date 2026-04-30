import importlib


def test_config_imports_without_required_secrets(monkeypatch):
    """The config module should import with no real deployment secrets present."""
    for key in (
        "OPENROUTER_API_KEY",
        "GROQ_API_KEY",
        "ANTHROPIC_API_KEY",
        "TALK_PASS",
        "NC_PASS",
        "STRIPE_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    import modules.config as _config
    config = importlib.reload(_config)

    assert config.OPENROUTER_API_KEY == ""
    assert config.ANTHROPIC_API_KEY == ""
    assert config.TALK_PASS == ""
    assert config.NC_PASS == ""
    assert config.STRIPE_SECRET_KEY == ""


def test_config_paths_can_be_overridden(monkeypatch):
    monkeypatch.setenv("BATTLE_BUDDY_HOME", "/tmp/battlebuddy-test")
    monkeypatch.setenv("DB_PATH", "/tmp/battlebuddy-test/test.db")

    import modules.config as config

    config = importlib.reload(config)

    assert config.BATTLE_BUDDY_HOME == "/tmp/battlebuddy-test"
    assert config.DB_PATH == "/tmp/battlebuddy-test/test.db"
