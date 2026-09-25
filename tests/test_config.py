import importlib
import sys


def _fresh_config_module():
    sys.modules.pop("modules.config", None)
    import modules

    if hasattr(modules, "config"):
        delattr(modules, "config")
    return importlib.import_module("modules.config")


def test_config_imports_without_required_secrets(monkeypatch):
    """The config module should import with no real deployment secrets present."""
    for key in (
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "TALK_PASS",
        "NC_PASS",
        "STRIPE_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    config = _fresh_config_module()

    assert config.OPENROUTER_API_KEY == ""
    assert config.ANTHROPIC_API_KEY == ""
    assert config.TALK_PASS == ""
    assert config.NC_PASS == ""
    assert config.STRIPE_SECRET_KEY == ""


def test_config_paths_can_be_overridden(monkeypatch):
    monkeypatch.setenv("BATTLE_BUDDY_HOME", "/tmp/battlebuddy-test")
    monkeypatch.setenv("DB_PATH", "/tmp/battlebuddy-test/test.db")

    config = _fresh_config_module()

    assert config.BATTLE_BUDDY_HOME == "/tmp/battlebuddy-test"
    assert config.DB_PATH == "/tmp/battlebuddy-test/test.db"


def test_homicide_seed_path_defaults_to_production(monkeypatch):
    """No env override must keep the production seed path."""
    monkeypatch.delenv("HOMICIDE_SEED_PATH", raising=False)
    monkeypatch.delenv("BATTLE_BUDDY_DATA_DIR", raising=False)
    monkeypatch.delenv("BATTLE_BUDDY_HOME", raising=False)

    config = _fresh_config_module()

    assert config.HOMICIDE_SEED_PATH == "/opt/battlebuddy/homicides_2026.json"


def test_homicide_seed_path_follows_home(monkeypatch):
    """A sandbox clone must redirect the seed, never touch /opt/battlebuddy."""
    monkeypatch.delenv("HOMICIDE_SEED_PATH", raising=False)
    monkeypatch.delenv("BATTLE_BUDDY_DATA_DIR", raising=False)
    monkeypatch.setenv("BATTLE_BUDDY_HOME", "/tmp/battlebuddy-test")

    config = _fresh_config_module()

    assert config.HOMICIDE_SEED_PATH == "/tmp/battlebuddy-test/homicides_2026.json"
    assert not config.HOMICIDE_SEED_PATH.startswith("/opt/battlebuddy")


def test_homicide_seed_path_follows_data_dir(monkeypatch):
    monkeypatch.delenv("HOMICIDE_SEED_PATH", raising=False)
    monkeypatch.setenv("BATTLE_BUDDY_HOME", "/tmp/battlebuddy-test")
    monkeypatch.setenv("BATTLE_BUDDY_DATA_DIR", "/tmp/battlebuddy-test/data")

    config = _fresh_config_module()

    assert config.HOMICIDE_SEED_PATH == "/tmp/battlebuddy-test/data/homicides_2026.json"


def test_homicide_seed_path_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("BATTLE_BUDDY_HOME", "/tmp/battlebuddy-test")
    monkeypatch.setenv("BATTLE_BUDDY_DATA_DIR", "/tmp/battlebuddy-test/data")
    monkeypatch.setenv("HOMICIDE_SEED_PATH", "/tmp/battlebuddy-test/seed.json")

    config = _fresh_config_module()

    assert config.HOMICIDE_SEED_PATH == "/tmp/battlebuddy-test/seed.json"
