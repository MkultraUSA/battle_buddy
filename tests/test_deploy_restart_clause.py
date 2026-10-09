"""A deploy script that cannot find systemd must not be trusted to find anything.

The first version of the restart fix detected systemd with:

    if command -v systemctl &>/dev/null && systemctl list-unit-files "$SVC" &>/dev/null | grep -q "$SVC"; then

`&>` binds to the *simple command*, not the pipeline, so that parses as
`systemctl list-unit-files ... &` — backgrounded — piped into grep. The
unit lookup never ran, the condition was false, and every deploy took the
"no supervisor found" branch and failed.

It failed loudly, which is why it was caught rather than shipped as a
silent no-op. But the guard intended to prove the script knew how to
restart the service was itself unable to run. A detection clause that
never executes is the same failure class as a check that cannot fail.

These tests execute the clause in a subshell with a stub `systemctl`, so
the shell's own parsing decides the answer. Reading the script as text
cannot catch a parsing bug -- that is the whole point of this file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = ROOT / "scripts" / "deploy.sh"

# The exact clause from deploy.sh, extracted so the test exercises the shipped
# text rather than a copy that could drift away from it.
CLAUSE_START = 'if command -v systemctl &>/dev/null && (systemctl list-unit-files'
CLAUSE_END = '; then'


def _restart_condition() -> str:
    """The condition text of the shipped restart `if`, without the `if`/`then`.

    Extracting just the condition lets the test wrap it in a complete
    statement. Taking the whole `if ... fi` block would drag in the real
    `systemctl restart`, and this test must not restart anything.
    """
    text = DEPLOY_SH.read_text(encoding="utf-8")
    line = next(
        ln.strip() for ln in text.splitlines() if "systemctl list-unit-files" in ln
    )
    assert line.startswith("if "), f"unexpected shape: {line}"
    return line[len("if "):].rsplit(CLAUSE_END, 1)[0].strip()


def _run_with_stub(condition: str, *, systemctl_rc: int, unit_found: bool) -> str:
    """Evaluate the shipped condition with a stub systemctl on PATH.

    Wrapped in a complete `if` so the shell's own parsing decides the answer.
    The stub honours the real tool's contract: print the unit line and exit 0
    when the unit exists, print nothing useful and exit non-zero when it does
    not. The stub also drops a marker file, so "did the command actually run"
    is answerable separately from "did the branch get taken".
    """
    stub_dir = Path("/tmp/_deploy_clause_test")
    stub_dir.mkdir(exist_ok=True)
    marker = stub_dir / "ran.marker"
    if marker.exists():
        marker.unlink()

    stub = stub_dir / "systemctl"
    body = f'touch "{marker}"\n'
    if unit_found:
        body += 'echo "fakesvc.service enabled enabled"\n'
    else:
        body += 'echo "0 unit files listed."\n'
    body += f"exit {systemctl_rc}\n"
    stub.write_text("#!/bin/bash\n" + body)
    stub.chmod(0o755)

    script = (
        f"PATH={stub_dir}:$PATH\n"
        f"if {condition}; then echo BRANCH_SYSTEMD; else echo BRANCH_OTHER; fi\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=20
    )
    ran = marker.exists()
    if not ran:
        return "STUB_NEVER_RAN"
    return proc.stdout.strip()


def test_deploy_script_is_valid_bash():
    assert DEPLOY_SH.is_file()
    assert subprocess.run(["bash", "-n", str(DEPLOY_SH)]).returncode == 0


def test_systemd_branch_is_taken_when_the_unit_exists():
    """A known systemd unit must select the systemd branch."""
    out = _run_with_stub(
        _restart_condition(), systemctl_rc=0, unit_found=True
    )
    assert out == "BRANCH_SYSTEMD", (
        f"expected the systemd branch for a unit systemctl knows about, "
        f"got {out!r}. A stub that never ran means the condition is not "
        "executing the lookup it appears to execute."
    )


def test_systemd_branch_is_not_taken_when_the_unit_is_unknown():
    """An unknown unit must fall through to supervisorctl or fail."""
    out = _run_with_stub(
        _restart_condition(), systemctl_rc=0, unit_found=False
    )
    assert out == "BRANCH_OTHER", (
        f"systemd branch taken for a unit that does not exist: {out!r}"
    )


def test_the_lookup_command_actually_runs():
    """The regression: `cmd &>/dev/null | grep` backgrounds cmd instead.

    `&>` binds to the simple command, not the pipeline, so the systemctl call
    is backgrounded and the branch condition is decided by nothing. Reading
    the script cannot catch that; only executing it can.
    """
    out = _run_with_stub(
        _restart_condition(), systemctl_rc=0, unit_found=True
    )
    assert out != "STUB_NEVER_RAN", (
        "the systemctl lookup never executed. `&>` before a pipe backgrounds "
        "the command instead of redirecting the pipeline, so the guard that "
        "proves the script can restart the service never runs."
    )


def test_the_fix_is_a_subshell_not_a_background_operator():
    """Guard the shape, so a future edit cannot reintroduce the parse bug."""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    line = next(
        ln for ln in text.splitlines() if "systemctl list-unit-files" in ln
    )
    assert "&>/dev/null |" not in line, (
        f"this line reintroduces the background-parse bug: {line.strip()}"
    )
    assert "(" in line and ")" in line, (
        "the pipeline needs a subshell so `&>` cannot bind to the simple "
        f"command: {line.strip()}"
    )