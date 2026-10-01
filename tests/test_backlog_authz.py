"""The backlog worker endpoints must fail closed.

`/api/backlog/claim` and `/api/backlog/complete` were both guarded by:

    if _backlog_token and token != _backlog_token:  -> 401

That is **fail-open**. With `BB_BACKLOG_AGENT_TOKEN` unset, the condition is
simply skipped and the route proceeds. nginx proxies `location /api/` to the
app, so both endpoints sat behind no authentication at all.

The consequence is worse than a read-only leak. `complete` writes a worker's
transcript, description, itype and coordinates back onto the call and into the
incident. An unauthenticated caller could therefore not only drain the queue,
but fabricate the text of a real incident.

This is deliberately stricter than queueing itself, which is opt-in via
`BB_BACKLOG_ENABLED`. Queueing can be off by default because that only wastes
memory. These two routes mutate state, so an unset secret must refuse.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_ROOT = pathlib.Path(__file__).parent.parent


def _receive_free_functions() -> dict:
    """audio_receiver cannot be imported here (needs Flask + faster_whisper), so
    the guard is inspected as AST. Tests assert on executable structure, never on
    prose: an earlier version grepped the source for a variable name and passed
    with the guard deleted, because the name also appeared in a docstring."""
    tree = ast.parse((_ROOT / "audio_receiver.py").read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = node
    return out


class TestBacklogTokenGuardExists(unittest.TestCase):
    def test_guard_is_defined(self):
        self.assertIn(
            "_require_backlog_token", _receive_free_functions(),
            "there is no fail-closed guard for the backlog worker endpoints",
        )

    def test_guard_refuses_when_unconfigured(self):
        """An unset secret must return 503, not fall through to the work."""
        fn = _receive_free_functions()["_require_backlog_token"]
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            # a Return of a tuple containing 503 somewhere inside
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and any(
                    isinstance(k, ast.Constant) and k.value == 503
                    for k in ast.walk(ret)
                ):
                    return
        self.fail(
            "no `if not expected: return ..., 503` in _require_backlog_token; an "
            "unset BB_BACKLOG_AGENT_TOKEN must refuse rather than admit"
        )

    def test_guard_compares_encoded_bytes(self):
        fn = _receive_free_functions()["_require_backlog_token"]
        src = ast.unparse(fn)
        self.assertIn(
            "compare_digest", src,
            "the credential check must use hmac.compare_digest",
        )
        self.assertRegex(
            src, r"compare_digest\(\s*supplied\.encode",
            "compare_digest raises TypeError on a non-ASCII str and header values "
            "arrive as latin-1, so one byte >= 0x80 would be a 500, not a 401",
        )


class TestBothRoutesAreGated(unittest.TestCase):
    """Neither route may keep its own fail-open check."""

    def _route_source(self, name: str) -> str:
        tree = ast.parse((_ROOT / "audio_receiver.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.unparse(node)
        raise AssertionError(f"{name} not found")

    def test_claim_and_complete_call_the_guard(self):
        for route in ("api_backlog_claim", "api_backlog_complete"):
            with self.subTest(route=route):
                src = self._route_source(route)
                self.assertIn(
                    "_require_backlog_token()", src,
                    f"{route} does not call the fail-closed guard",
                )
                self.assertIn(
                    "if checked is None", src,
                    f"{route} must act on the guard's return value",
                )

    def test_the_old_fail_open_check_is_gone(self):
        """`if _backlog_token and ...` is the exact bug being removed."""
        for route in ("api_backlog_claim", "api_backlog_complete"):
            with self.subTest(route=route):
                src = self._route_source(route)
                self.assertNotIn(
                    "_backlog_token and", src,
                    f"{route} still has the fail-open token check",
                )


if __name__ == "__main__":
    unittest.main()