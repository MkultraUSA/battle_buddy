"""The browser's camera frame gate has to be tested by behaviour, not by text.

Everything `tests/test_camera_layer.py` can say about the browser's copy of that
gate is a string: it reads `p.hostname !== CAMERA_FRAME_HOST` out of
`static/js/public_map.js` and asserts the substring is present. That pins the
shape of what we shipped, not the property we want. Rewriting the gate to return
the URL untouched -- leaving the hostname comparison alive in a comment, or
behind `if (false)` -- passes every assertion in that file while the last line of
defence is gone, and generated snapshot data is exactly the kind of input you
want that defence for.

`scripts/check_camera_js.mjs` lifts the real functions out of the real file and
runs hostile URLs through them. This module exists so that check is actually
*run*: it is not a separate CI job, because a check that lives outside the suite
is a check nobody reads the result of, and the Telegram notifier summarises
pytest only. Run through pytest it inherits the same counting, the same failure
report and the same alerting.

Two things this file is careful about, both learned the hard way elsewhere in
this repo:

  * **A skipped check that reports success is worse than no check.** A post-deploy
    smoke test pointed at a domain that did not resolve and was green for months
    because it had never once executed anything. So if `node` is missing *in CI*,
    that is a failure, not a skip. Locally it is a skip, because a contributor
    without node should not be blocked.
  * **A witness that mutates the source file is a hazard.** The neutered copy
    below is written to a temporary directory. The first version of an earlier
    witness in this repo wrote its misspelt call into the real `audio_receiver.py`
    and, when its assertion failed part way, the restore in `finally` never ran
    and left a typo in the working tree -- which is also the route by which
    unrelated drift blocks a deploy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_camera_js.mjs"
_JS = _ROOT / "static" / "js" / "public_map.js"


def _node() -> str | None:
    return shutil.which("node")


def _in_ci() -> bool:
    return bool(os.environ.get("CI"))


def _run(js_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_node(), str(_SCRIPT), str(js_path)],
        cwd=_ROOT, capture_output=True, text=True, timeout=120,
    )


class TestNodeIsActuallyAvailable(unittest.TestCase):
    """A check that cannot run must never be reported as one that did."""

    def test_node_is_present_in_ci(self):
        if _node() is None and _in_ci():
            self.fail(
                "node is not installed, so the browser-side camera gate is not "
                "being tested at all. Ubuntu runners ship node; add a setup-node "
                "step rather than letting this skip."
            )


@unittest.skipIf(_node() is None, "node is not installed (fine locally, not in CI)")
class TestBrowserSideCameraGateIsTestedByBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _run(_JS)

    def test_it_exits_clean(self):
        self.assertEqual(
            0, self.result.returncode,
            f"the camera gate check failed:\n{self.result.stdout[-4000:]}"
            f"\n{self.result.stderr[-2000:]}",
        )

    def test_the_body_reports_success_and_not_just_the_exit_code(self):
        """Exit status alone is a weaker claim than the output makes.

        Telegram answers HTTP 200 with `{"ok":false}` for an over-long message, so
        a `curl` exited 0, the step passed, CI stayed green and an alert silently
        never arrived. Same shape here: assert on what the script said, not only
        on what it returned.
        """
        self.assertIn("CAMERA_JS_CHECKS: ok", self.result.stdout)
        # Scoped to the real source: the witness section further down is *meant*
        # to print FAIL lines, and a check that forbids the word anywhere would
        # forbid the harness from proving it works.
        real_section = self.result.stdout.split("== [witness")[0]
        self.assertNotIn("FAIL", real_section)

    def test_it_ran_the_checks_rather_than_finding_nothing_to_do(self):
        """Zero assertions is indistinguishable from a green run."""
        ran = len(re.findall(r"^  (?:ok  |FAIL)", self.result.stdout, re.M))
        self.assertGreater(ran, 25, f"only {ran} checks ran; the audit went missing")
        self.assertIn("[real]", self.result.stdout)


@unittest.skipIf(_node() is None, "node is not installed (fine locally, not in CI)")
class TestTheGateCheckWouldNoticeABrokenGate(unittest.TestCase):
    """The in-script witness, confirmed from outside.

    `check_camera_js.mjs` neuters `cameraFrameUrl` and requires its own audit to
    catch it, which covers the harness against quietly stopping. That witness is
    fifteen lines that look like dead weight, and deleting it would cost nothing
    visible -- so this test runs the script against a second, differently broken
    source and requires failures to be reported *by name*.

    Broken here rather than there on purpose: the script's own witness already
    neuters `cameraFrameUrl`, and neutering it twice makes the script bail with
    "could not neuter" instead of reporting the failures we came here to read.
    This takes out `esc` instead, which exercises the markup-escaping half of
    the audit and leaves the in-script witness intact.
    """

    def test_a_neutered_escaper_is_reported_by_name(self):
        source = _JS.read_text(encoding="utf-8")
        broken = re.sub(
            r"^function esc\(s\) \{[\s\S]*?^\}",
            "function esc(s) {\n  return String(s);\n}",
            source,
            count=1,
            flags=re.M,
        )
        self.assertNotEqual(source, broken, "could not neuter esc(); nothing proved")

        with tempfile.TemporaryDirectory() as tmp:
            # Written to a temp dir, never over the repo file: see the module
            # docstring for what happened last time a test edited a source file
            # in place.
            path = Path(tmp) / "public_map_broken.js"
            path.write_text(broken, encoding="utf-8")
            result = _run(path)

        self.assertNotEqual(0, result.returncode, "a gate with no escaping passed")
        self.assertIn("CAMERA_JS_CHECKS:", result.stdout)
        self.assertNotIn("CAMERA_JS_CHECKS: ok", result.stdout)
        self.assertRegex(
            result.stdout, r"(?m)^  FAIL .*(escaped|handler reaches the markup)",
            "the failure was not reported by name, so the output is not trustworthy",
        )

    def test_the_scripts_own_witness_still_ran_and_caught_something(self):
        self.assertRegex(
            _run(_JS).stdout,
            r"(?m)^== witness: the neutered gate was caught by [1-9]\d* checks ==$",
            "the in-script witness is missing or caught nothing",
        )


class TestTheCheckIsWiredToTheRealSource(unittest.TestCase):
    """Cheap guards on the wiring, so it cannot rot unnoticed."""

    def test_the_script_exists_where_the_test_expects_it(self):
        self.assertTrue(_SCRIPT.is_file(), f"{_SCRIPT} is missing")

    def test_the_script_defaults_to_the_shipped_map_source(self):
        self.assertIn("static/js/public_map.js", _SCRIPT.read_text(encoding="utf-8"))

    def test_it_reads_the_functions_rather_than_a_copy_of_them(self):
        """A vendored copy is a second thing to keep in sync, and the day it
        drifts is the day this check starts reporting on code that never ships."""
        src = _SCRIPT.read_text(encoding="utf-8")
        self.assertIn("readFileSync", src)
        self.assertNotRegex(src, r"function cameraFrameUrl\(v\)\s*\{\s*(var|const|let)\s+s")

    def test_the_map_interaction_check_is_committed_too(self):
        """The expensive half -- a real dispatched click -- needs a browser, so it
        cannot run in CI. It still belongs in the repository: the click driver
        that found the swallowed-click bug lived in /tmp, which is where tooling
        goes to be lost."""
        self.assertTrue((_ROOT / "scripts" / "map_interaction_check.mjs").is_file())


if __name__ == "__main__":
    unittest.main()