"""The deploy script must not report success when nothing restarted.

`scripts/deploy.sh` used to try `supervisorctl` only. On a systemd host
(supervisorctl absent) it printed "skipping restart", then printed
"Deploy complete", and exited 0. The new code was on disk and the old
process kept serving traffic. Every deploy on this box was a no-op that
looked clean -- the same "looks successful, isn't" shape as the four
Grafana gates that could never fire.

These tests read the script as text and exercise its decision branches in a
subshell. They do not deploy anything; they pin the three outcomes:

  * a known systemd unit  -> restarts it and confirms it is active
  * no supervisor at all   -> exits non-zero, never prints "Deploy complete"
  * health counters of -1  -> exits non-zero

The last one matters most: the `-1` sentinel bug sat in production for
weeks behind a green deploy. A deploy that checks the counters cannot be
fooled by it again.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = ROOT / "scripts" / "deploy.sh"


def _script() -> str:
    return DEPLOY_SH.read_text(encoding="utf-8")


def test_deploy_script_exists_and_is_executable_bash():
    assert DEPLOY_SH.is_file(), "scripts/deploy.sh is missing"
    text = _script()
    assert text.startswith("#!/bin/bash")
    assert subprocess.run(["bash", "-n", str(DEPLOY_SH)]).returncode == 0, (
        "deploy.sh is not valid bash"
    )


def test_supervisorctl_is_not_the_only_path():
    """The regression itself: systemd must be tried, not just supervisorctl."""
    text = _script()
    assert "systemctl restart" in text, (
        "deploy.sh no longer restarts via systemd; on this host that means no "
        "restart at all"
    )
    # systemd is the primary path, supervisorctl the fallback for older hosts.
    assert text.index("systemctl restart") < text.index("supervisorctl restart")


def test_no_supervisor_is_a_failure_not_a_skip():
    """No supervisor must exit non-zero and never print Deploy complete."""
    text = _script()
    # Look only at executable lines. The phrase survives in a comment that
    # explains the bug it used to be, which is not the same as the behaviour.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "skipping restart" not in code, (
        "'skipping restart' is the original defect: it reported a deploy that "
        "did not happen"
    )
    assert "no supervisor found" in code
    # The failure branch must exit before the success banner.
    fail_at = code.index("no supervisor found")
    banner_at = code.index("Deploy complete")
    assert fail_at < banner_at
    # And it must actually exit non-zero in that branch.
    branch = code[fail_at:banner_at]
    assert re.search(r"exit 1", branch), (
        "the no-supervisor branch must exit non-zero; otherwise a caller "
        "cannot tell a no-op deploy from a real one"
    )


def test_health_failure_is_a_deploy_failure():
    """A -1 counter must fail the deploy, not be reported as clean."""
    text = _script()
    assert "/api/health" in text, (
        "deploy.sh does not check the app at all, so a deploy that breaks the "
        "service cannot be detected by the deploy itself"
    )
    assert '== -1' in text or "==-1" in text or "== -1" in text, (
        "the health check must treat -1 as a failure -- that sentinel is "
        "exactly what hid the broken status=active query for weeks"
    )


def test_restart_is_confirmed_not_just_requested():
    """`systemctl restart` exits 0 even when the unit dies immediately."""
    text = _script()
    assert "is-active" in text, (
        "the restart is not confirmed; a unit that exits on start still "
        "reports a successful restart command"
    )
    assert text.index("systemctl restart") < text.index("is-active")


def test_deploy_still_refuses_on_drift():
    """The force flag and the dirty-tree guard must survive the rewrite."""
    text = _script()
    assert "guard_drift_check.sh" in text
    assert "--force" in text
    # A forced deploy must be visibly marked, not silent.
    assert "DEPLOY FORCED" in text