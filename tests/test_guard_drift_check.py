"""
Tests for scripts/guard_drift_check.sh — git drift detection guard.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def guard_script():
    """Return path to the guard_drift_check.sh script."""
    p = Path(__file__).parent.parent / "scripts" / "guard_drift_check.sh"
    assert p.exists(), f"Script not found: {p}"
    return str(p)


# Git identity env for temp repos (no global git config on CI / containers)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.test",
}
_GIT_INIT_DEFAULT_BRANCH = "main"


def _git_env(extra=None):
    env = {**os.environ, **_GIT_ENV}
    if extra:
        env.update(extra)
    return env


def _git(repo: str, *args):
    """Run a git command in a repo."""
    subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True, env=_git_env())


@pytest.fixture
def temp_repo(guard_script):
    """Create a temporary git repo with an origin/main setup.

    Returns (repo_path, script_path).
    """
    tmp = tempfile.mkdtemp(prefix="bb_drift_test_")
    repo = Path(tmp) / "repo"
    repo.mkdir()

    # Init bare "remote"
    remote = Path(tmp) / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "-C", str(remote), "init", "--bare", f"--initial-branch={_GIT_INIT_DEFAULT_BRANCH}"],
        check=True, capture_output=True, env=_git_env(),
    )

    # Init local and point to remote
    subprocess.run(
        ["git", "-C", str(repo), "init", f"--initial-branch={_GIT_INIT_DEFAULT_BRANCH}"],
        check=True, capture_output=True, env=_git_env(),
    )
    _git(str(repo), "remote", "add", "origin", str(remote))

    # Create initial commit on main
    (repo / "README.md").write_text("# test repo")
    _git(str(repo), "add", "README.md")
    _git(str(repo), "commit", "-m", "initial")
    _git(str(repo), "push", "-u", "origin", _GIT_INIT_DEFAULT_BRANCH)

    yield str(repo), guard_script

    # Cleanup
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _run(repo: str, script: str, mode: str = "check", cwd_override: str | None = None) -> tuple[int, str]:
    """Run guard script and return (exit_code, stdout)."""
    env = _git_env({"BATTLE_BUDDY_HOME": repo})
    cwd = cwd_override if cwd_override is not None else repo
    r = subprocess.run(
        ["bash", script, mode],
        env=env,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return r.returncode, r.stdout.strip()


# ── Clean working tree, on main ──────────────────────────────────────────

def test_clean_and_on_main_passes_check(temp_repo):
    repo, script = temp_repo
    code, out = _run(repo, script, "check")
    assert code == 0, f"Expected 0, got {code}: {out}"
    assert out == ""


def test_clean_and_on_main_passes_report(temp_repo):
    repo, script = temp_repo
    code, out = _run(repo, script, "report")
    assert code == 0, f"Expected 0, got {code}: {out}"
    assert "✓" in out
    assert "working tree is clean" in out
    assert "on origin/main" in out


# ── Dirty working tree ───────────────────────────────────────────────────

def test_dirty_untracked_file_fails_check(temp_repo):
    repo, script = temp_repo
    (Path(repo) / "untracked.txt").write_text("drift")
    code, out = _run(repo, script, "check")
    assert code == 1, f"Expected 1, got {code}: {out}"
    assert "dirty" in out.lower()


def test_dirty_modified_file_fails_report(temp_repo):
    repo, script = temp_repo
    (Path(repo) / "README.md").write_text("modified content")
    code, out = _run(repo, script, "report")
    assert code == 1, f"Expected 1, got {code}: {out}"
    assert "✗" in out
    assert "dirty" in out.lower()


# ── Not on origin/main ───────────────────────────────────────────────────

def test_different_commit_fails_check(temp_repo):
    repo, script = temp_repo
    _git(repo, "checkout", "-b", "feature/test")
    (Path(repo) / "feat.txt").write_text("new feature")
    _git(repo, "add", "feat.txt")
    _git(repo, "commit", "-m", "feat")
    code, out = _run(repo, script, "check")
    assert code == 1, f"Expected 1, got {code}: {out}"
    assert "not on origin/main" in out


def test_different_commit_fails_report(temp_repo):
    repo, script = temp_repo
    _git(repo, "checkout", "-b", "feature/test2")
    (Path(repo) / "feat2.txt").write_text("feature 2")
    _git(repo, "add", "feat2.txt")
    _git(repo, "commit", "-m", "feat2")
    code, out = _run(repo, script, "report")
    assert code == 1, f"Expected 1, got {code}: {out}"
    assert "✗" in out


# ── check-quiet mode ─────────────────────────────────────────────────────

def test_check_quiet_passes_silently(temp_repo):
    repo, script = temp_repo
    code, out = _run(repo, script, "check-quiet")
    assert code == 0, f"Expected 0, got {code}: {out}"
    assert out == ""


def test_check_quiet_fails_with_output(temp_repo):
    repo, script = temp_repo
    (Path(repo) / "README.md").write_text("change")
    code, out = _run(repo, script, "check-quiet")
    assert code == 1, f"Expected 1, got {code}: {out}"
    assert "DRIFT" in out


# ── Unknown mode ─────────────────────────────────────────────────────────

def test_unknown_mode_exits_2(temp_repo):
    repo, script = temp_repo
    code, out = _run(repo, script, "bogus")
    assert code == 2, f"Expected 2, got {code}: {out}"


# ── Missing repo path ────────────────────────────────────────────────────

def test_missing_project_dir_fails(guard_script):
    """Bogus BATTLE_BUDDY_HOME should cause script to fail."""
    env = _git_env({"BATTLE_BUDDY_HOME": "/nonexistent/path"})
    r = subprocess.run(
        ["bash", guard_script, "check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0, f"Expected non-zero, got {r.returncode}: {r.stdout}"
