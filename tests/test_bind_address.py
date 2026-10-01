"""The app must bind loopback by default, not every interface.

Port 9001 was hardcoded to `host="0.0.0.0"` and the whole HTTP surface was
reachable from the public internet: `/receive` took unauthenticated audio (now
token-gated by PR #161), and everything else was exposed twice -- once through
nginx and once directly, bypassing whatever nginx does.

The reverse proxy is the intended ingress. The capture nodes post to nginx over
TLS; `stream_recorder.py` and `scripts/ops_verify.py` use `127.0.0.1`; and Alloy
scrapes metrics from `127.0.0.1:9001` (`/etc/alloy/config.alloy`), so nothing
legitimate needs a wider bind.

These guard the DEFAULT, which is the part that regresses quietly: a
`--host 0.0.0.0` added to the systemd unit, or a future edit back to a literal in
`app.run`, reopens the exposure without touching anything else in the tree.

Note there is no `main()` function: the startup block -- argparse, the thread
supervisors, `app.run` -- sits directly under `if __name__ == "__main__":`.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


def _startup_block() -> ast.If:
    """The `if __name__ == "__main__":` node."""
    tree = ast.parse((_ROOT / "audio_receiver.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            return node
    raise AssertionError('audio_receiver.py has no `if __name__ == "__main__":` block')


def _app_run_calls() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_startup_block())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]


def _argparse_defaults() -> dict[str, ast.expr]:
    """flag name -> default expression, for every add_argument in the block."""
    defaults: dict[str, ast.expr] = {}
    for node in ast.walk(_startup_block()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            for kw in node.keywords:
                if kw.arg == "default":
                    defaults[node.args[0].value] = kw.value
    return defaults


class TestBindAddress(unittest.TestCase):
    def test_app_run_binds_the_host_argument_not_a_literal(self):
        """`app.run` must not hardcode an interface again."""
        calls = _app_run_calls()
        self.assertTrue(calls, "no app.run(...) call found in the __main__ block")
        host_args = [
            kw.value for call in calls for kw in call.keywords if kw.arg == "host"
        ]
        self.assertTrue(host_args, "app.run() has no host= keyword")
        for value in host_args:
            self.assertIsInstance(
                value, ast.Name,
                "app.run(host=...) must take the --host argument; a literal here "
                "is how 0.0.0.0 came back",
            )
            self.assertEqual(value.id, "args.host")

    def test_default_bind_is_loopback(self):
        defaults = _argparse_defaults()
        self.assertIn(
            "--host", defaults,
            "no --host argument exists, so the bind address cannot be set per "
            "deployment and is whatever app.run hardcodes",
        )
        self.assertEqual(
            ast.literal_eval(defaults["--host"]), "127.0.0.1",
            "the default bind must be loopback; the reverse proxy is the "
            "intended ingress and capture nodes reach the app through it",
        )

    def test_wider_bind_requires_an_explicit_opt_in(self):
        """0.0.0.0 must only ever be reachable by asking for it."""
        literals = set()
        for value in _argparse_defaults().values():
            try:
                literals.add(ast.literal_eval(value))
            except (ValueError, SyntaxError):
                continue  # a computed default is not a wide bind
        self.assertNotIn(
            "0.0.0.0", literals,
            "a default of 0.0.0.0 reopens the public exposure this closes",
        )


if __name__ == "__main__":
    unittest.main()