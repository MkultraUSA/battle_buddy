"""The deploy notifier must never let a commit message become a shell command.

A commit message is arbitrary text chosen by whoever pushed. `deploy.yml` used
to interpolate it straight into a double-quoted `run:` block:

    -d text="ok%0A${{ github.event.head_commit.message }}"

Merging a message that happened to mention a function in backticks made bash
perform command substitution on it. Concretely, merging the camera snapshot
work produced `write_text: command not found`, `os.replace: command not found`
and a syntax error; the step exited non-zero, so the workflow went red and
`if: failure()` fired a "deploy FAILED" page -- immediately after the same run
had already sent a correct "deployed successfully" page. Two contradictory
alerts about one healthy deploy, and a red check on a green merge.

These tests execute the real notifier scripts with a deliberately hostile commit
message and a stubbed curl, so the guarantee is behavioural rather than a
comment saying it should be true.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"

EXPR_COMMIT = "${{ github.event.head_commit.message }}"
EXPR_TOKEN = "${{ secrets.TELEGRAM_BOT_TOKEN }}"
EXPR_CHAT = "${{ secrets.TELEGRAM_CHAT_ID }}"
EXPR_RUN_URL = (
    "${{ github.server_url }}/${{ github.repository }}/actions/runs/"
    "${{ github.run_id }}"
)
SMOKE_RESULTS = "/tmp/smoke_results.txt"


def _steps() -> list[tuple[str, str]]:
    workflow = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
    out = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                out.append((step.get("name", "?"), step["run"]))
    return out


def _notifiers() -> list[tuple[str, str]]:
    return [(n, s) for n, s in _steps() if "Notify Telegram" in n]


def _hostile_message(canary: Path) -> str:
    """A commit message that would be dangerous if any shell ever parsed it.

    The payload is `touch`, not `rm`: the point is to prove command substitution
    happened, and a test that actually deletes things to prove a quoting bug is
    its own hazard.
    """
    return (
        "fix: mention `write_text` and `os.replace` in prose\n"
        "\n"
        f"Ran $(touch {canary}) and `touch {canary}` and ${{HOME}} too\n"
        'quotes: "double" and \'single\'; ampersand & equals = done\n'
        "percent % sign, a dash - and a backslash \\ too"
    )


def _materialise(script: str, canary: Path, smoke_results: Path) -> str:
    """Turn a workflow `run:` block into something bash can execute locally."""
    return (script
            .replace(EXPR_COMMIT, _hostile_message(canary))
            .replace(EXPR_TOKEN, "TESTTOKEN")
            .replace(EXPR_CHAT, "TESTCHAT")
            .replace(SMOKE_RESULTS, str(smoke_results))
            .replace(EXPR_RUN_URL, "https://example.invalid/run/1"))


def _stub_curl(tmp_path: Path) -> Path:
    """A curl that records its arguments and sends nothing anywhere."""
    record = tmp_path / "sent.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{record}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    # Without this the real curl is found instead and the test silently
    # measures nothing.
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return record


class TestTheCommitMessageIsNeverInterpolatedIntoAScript:
    def test_deploy_yaml_parses(self):
        workflow = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
        assert set(workflow["jobs"]) == {"deploy", "smoke-test", "ops-verify"}

    def test_no_run_block_mentions_the_commit_message_expression(self):
        """The whole fix in one assertion: it must arrive via env:, not inline.

        Quoting is not the defence. A commit message is untrusted input that can
        contain a double quote, and a quoted interpolation is still parsed by
        the shell; only keeping it out of the script entirely is.
        """
        offenders = [name for name, script in _steps() if EXPR_COMMIT in script]
        assert offenders == [], (
            f"these steps interpolate the commit message into the script: "
            f"{offenders}"
        )

    def test_the_commit_message_is_passed_through_the_environment(self):
        raw = DEPLOY.read_text(encoding="utf-8")
        assert f"COMMIT_MESSAGE: {EXPR_COMMIT}" in raw, (
            "the notifier should read the message from an env var"
        )

    def test_every_notifier_encodes_its_payload(self):
        """--data-urlencode, not -d text=, so newlines and & survive."""
        for name, script in _notifiers():
            assert "-d text=" not in script, (
                f"{name} still hand-builds the form body"
            )
            assert "--data-urlencode" in script, (
                f"{name} does not URL-encode its payload"
            )


class TestTheSmokeTestPointsAtTheRealSite:
    """The post-deploy smoke test pointed at a domain that does not resolve.

    `SMOKE_TEST_BASE_URL` was `https://battlebuddy.new` — one letter short of
    the real host. Nothing failed loudly: the job ran, could not resolve, and
    because `needs: deploy` had been skipped by the notify bug above, the smoke
    test had not been exercising anything for some time while appearing to be a
    real post-deploy check.

    Pinned against an explicit host rather than by resolving DNS, because a test
    that depends on the network is a test that fails for the wrong reason.
    """

    PRODUCTION_HOST = "battlebuddy.news"

    def _workflow_files(self) -> list[Path]:
        return sorted((ROOT / ".github" / "workflows").glob("*.yml"))

    def test_the_smoke_test_base_url_is_the_production_site(self):
        raw = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        m = re.search(r"SMOKE_TEST_BASE_URL:\s*(\S+)", raw)
        assert m, "the smoke test no longer sets a base URL"
        assert m.group(1) == f"https://{self.PRODUCTION_HOST}", (
            f"the smoke test points at {m.group(1)}, which is not the site "
            f"battlebuddy serves (https://{self.PRODUCTION_HOST})"
        )

    def test_no_workflow_mentions_a_battlebuddy_host_that_does_not_exist(self):
        """Catches the typo class generally, not just the one that happened."""
        bad = []
        for path in self._workflow_files():
            for host in re.findall(r"https://(battlebuddy\.[a-z]+)",
                                   path.read_text(encoding="utf-8")):
                if host != self.PRODUCTION_HOST:
                    bad.append(f"{path.name}: {host}")
        assert bad == [], (
            f"these workflow hosts are not the production site: {bad}"
        )

    def test_the_notifier_gate_count_is_not_stale(self):
        """The SLO page hardcoded '13 gates'; it is 19 now.

        A stale number in an alert is small, but it is the same failure as a
        stale metric name: the reader trusts a value nobody maintains. The
        runtime count is asserted in test_camera_snapshot_ops.py, which has the
        stub harness needed to run main() -- it cannot be derived by counting
        gate() call sites here, because one of them is inside a loop that emits
        five gates.
        """
        raw = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        assert "13 gates" not in raw, (
            "the SLO notifier is still claiming the pre-camera gate count"
        )
        assert re.search(r"SLOs green \(\d+ gates\)", raw), (
            "the SLO notifier should state how many gates ran"
        )


class TestAHostileCommitMessageCannotExecuteAnything:
    """Behaviour, not intention: run the real scripts and see what happens."""

    def _run(self, tmp_path, script):
        canary = tmp_path / "pwned"
        smoke_results = tmp_path / "smoke_results.txt"
        smoke_results.write_text("FAILED tests/test_smoke.py::something\n")
        record = _stub_curl(tmp_path)
        body = _materialise(script, canary, smoke_results)

        # GitHub runs `run:` blocks under `bash -e`, so match that.
        proc = subprocess.run(
            ["bash", "-e", "-c", body],
            capture_output=True, text=True, timeout=30,
            env={
                "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "TELEGRAM_BOT_TOKEN": "TESTTOKEN",
                "TELEGRAM_CHAT_ID": "TESTCHAT",
                "COMMIT_MESSAGE": _hostile_message(canary),
                "RUN_URL": "https://example.invalid/run/1",
            },
        )
        return proc, record, canary, body

    @pytest.mark.parametrize("name,script", _notifiers(),
                             ids=[n for n, _ in _notifiers()])
    def test_the_script_runs_clean_and_passes_the_message_through(
        self, tmp_path, name, script
    ):
        proc, record, canary, body = self._run(tmp_path, script)

        assert proc.returncode == 0, (
            f"{name} exited {proc.returncode}\n{proc.stderr}\n"
            f"--- script ---\n{body}"
        )
        assert "command not found" not in proc.stderr, (
            f"{name} tried to execute part of the commit message:\n{proc.stderr}"
        )
        assert "syntax error" not in proc.stderr, (
            f"{name} could not parse a normal commit message:\n{proc.stderr}"
        )
        assert not canary.exists(), (
            f"{name} executed attacker-controlled text from a commit message"
        )

        sent = record.read_text(encoding="utf-8")
        # Every distinctive fragment arrives intact, backticks and all.
        for fragment in ("write_text", "os.replace", "touch", "${HOME}",
                         "double", "ampersand", "backslash"):
            assert fragment in sent, (
                f"{name} lost {fragment!r} on the way to Telegram:\n{sent}"
            )

    @pytest.mark.parametrize("name,script", _notifiers(),
                             ids=[n for n, _ in _notifiers()])
    def test_every_notifier_says_which_deploy_it_is_about(self, tmp_path, name,
                                                          script):
        """An alert that does not name the commit is an alert you cannot act on."""
        proc, record, canary, body = self._run(tmp_path, script)
        assert proc.returncode == 0, proc.stderr
        sent = record.read_text(encoding="utf-8")
        assert "fix: mention" in sent, (
            f"{name} does not include the commit message, so a breach or a "
            f"failure page gives no clue which deploy caused it:\n{sent}"
        )

    @pytest.mark.parametrize("name,script", _notifiers(),
                             ids=[n for n, _ in _notifiers()])
    def test_the_url_and_chat_id_are_not_leaked_into_the_text(self, tmp_path,
                                                              name, script):
        """The token must be in the URL only, never in the message body."""
        proc, record, canary, body = self._run(tmp_path, script)
        assert proc.returncode == 0, proc.stderr
        sent = record.read_text(encoding="utf-8")
        assert "TESTTOKEN" in sent, "the stub curl was not given the API URL"
        text_arg = [ln for ln in sent.splitlines() if ln.startswith("text=")]
        assert text_arg, f"{name} sent no text field:\n{sent}"
        assert "TESTTOKEN" not in text_arg[0], (
            "the bot token leaked into the message body"
        )

    def test_a_commit_message_that_would_have_broken_the_old_version_still_works(
        self, tmp_path
    ):
        """Regression witness: the exact payload that caused the false page.

        Run through the old shape -- inline interpolation inside -d text= --
        this message produced `write_text: command not found` and a non-zero
        exit. Through the new shape it must be inert.
        """
        canary = tmp_path / "pwned"
        smoke_results = tmp_path / "smoke_results.txt"
        smoke_results.write_text("x\n")
        _stub_curl(tmp_path)
        message = _hostile_message(canary)

        old_style = (
            'curl -s "https://api.telegram.org/botTESTTOKEN/sendMessage" '
            f'-d chat_id="TESTCHAT" -d "text=ok%0A{message}"'
        )
        old = subprocess.run(["bash", "-e", "-c", old_style],
                             capture_output=True, text=True, timeout=30,
                             env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"})
        assert "command not found" in old.stderr, (
            "the old shape no longer reproduces the bug, so this test no "
            "longer demonstrates anything -- check what changed"
        )
        assert canary.exists(), "the old shape did not execute anything?"

        canary.unlink()
        new_style = (
            'curl -s "https://api.telegram.org/botTESTTOKEN/sendMessage" '
            '--data-urlencode "chat_id=TESTCHAT" '
            '--data-urlencode "text=ok\n$COMMIT_MESSAGE"'
        )
        new = subprocess.run(["bash", "-e", "-c", new_style],
                             capture_output=True, text=True, timeout=30,
                             env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
                              "COMMIT_MESSAGE": message})
        assert new.returncode == 0, new.stderr
        assert "command not found" not in new.stderr
        assert not canary.exists(), (
            "the new shape still executed the commit message"
        )