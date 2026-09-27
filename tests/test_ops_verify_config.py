"""Regression tests for the ops_verify SLO gate runtime configuration.

The post-deploy SLO gate SSHes into production and runs scripts/ops_verify.py
from a bare shell, which does not inherit the service's environment. Before
this was fixed the script hardcoded a database path inside the git checkout,
while the service reads DB_PATH from its EnvironmentFile set. The gate then
inspected an empty file and reported "no such table: calls" on every deploy,
which made the pipeline permanently red and trained everyone to ignore it.

These tests pin the two properties that matter:
  1. the EnvironmentFile set is applied in systemd order, later files winning;
  2. the database path comes from the service environment, never a hardcoded
     path inside the checkout.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ops_verify.py"


def _load_ops_verify(monkeypatch, env_files, environ=None):
    """Import ops_verify.py with a faked systemctl and environment."""
    fake = " ".join(f"{p} (ignore_errors=no)" for p in env_files)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else "", 0, fake, ""),
    )
    for key in list(os.environ):
        if key.startswith(("DB_PATH", "BATTLE_BUDDY_")):
            monkeypatch.delenv(key, raising=False)
    for key, value in (environ or {}).items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("ops_verify_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ops_verify_under_test"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("ops_verify_under_test", None)
    return mod


def test_later_env_file_wins(tmp_path, monkeypatch):
    """systemd applies later EnvironmentFile entries over earlier ones."""
    first = tmp_path / "first.env"
    first.write_text("DB_PATH=/from/first.db\n")
    second = tmp_path / "second.env"
    second.write_text("DB_PATH=/from/second.db\n")

    mod = _load_ops_verify(monkeypatch, [str(first), str(second)])
    assert os.environ["DB_PATH"] == "/from/second.db"
    assert mod.DB == "/from/second.db"


def test_db_path_comes_from_service_env(tmp_path, monkeypatch):
    """The gate must use the service's DB_PATH, not a path in the checkout."""
    env = tmp_path / "runtime-data.env"
    env.write_text("DB_PATH=/opt/battlebuddy-data/calls.db\nBATTLE_BUDDY_DATA_DIR=/opt/battlebuddy-data\n")

    mod = _load_ops_verify(monkeypatch, [str(env)])
    assert mod.DB == "/opt/battlebuddy-data/calls.db"
    assert not mod.DB.startswith("/opt/battlebuddy/calls.db")


def test_quotes_and_comments_are_handled(tmp_path, monkeypatch):
    """Quoted values and comment lines must not corrupt the environment."""
    env = tmp_path / "runtime-data.env"
    env.write_text(
        "# a comment line\n"
        "\n"
        "BATTLE_BUDDY_DATA_DIR='/opt/battlebuddy-data'\n"
        'DB_PATH="/opt/battlebuddy-data/calls.db"\n'
    )

    mod = _load_ops_verify(monkeypatch, [str(env)])
    assert mod.DB == "/opt/battlebuddy-data/calls.db"
    assert os.environ["BATTLE_BUDDY_DATA_DIR"] == "/opt/battlebuddy-data"


def test_missing_env_files_are_tolerated(tmp_path, monkeypatch):
    """A missing EnvironmentFile must not crash the gate."""
    mod = _load_ops_verify(monkeypatch, [str(tmp_path / "absent.env")])
    # Falls back to the config default, but must not raise on import.
    assert isinstance(mod.DB, str)


def test_no_hardcoded_checkout_db_path_in_source():
    """Regression guard: the old hardcoded path must not come back."""
    source = SCRIPT.read_text()
    assert 'DB = "/opt/battlebuddy/calls.db"' not in source
    assert "load_service_env" in source
