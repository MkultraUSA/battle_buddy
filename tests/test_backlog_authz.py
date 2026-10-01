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

    def test_guard_returns_none_on_success_not_the_request_body(self):
        """Returning `data` makes Flask echo the caller's token in the response.

        Observed live: POST with {"token": ...} came back as
        {"token": "<the token>"}. The None return IS the success signal, exactly
        as in _require_receive_token.
        """
        fn = _receive_free_functions()["_require_backlog_token"]
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        for ret in returns:
            if isinstance(ret.value, ast.Name) and ret.value.id == "data":
                self.fail(
                    "_require_backlog_token returns `data` on success; Flask will "
                    "jsonify the parsed request body and echo the caller's token "
                    "back in the response"
                )
        # `return None` parses to Return(value=Constant(None)), NOT value=None.
        def returns_none(r: ast.Return) -> bool:
            return r.value is None or (
                isinstance(r.value, ast.Constant) and r.value.value is None
            )
        self.assertTrue(
            any(returns_none(r) for r in returns),
            "the guard must `return None` on success so the caller can tell "
            "authorised from refused",
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
                # _require_backlog_token returns None on SUCCESS and a
                # (response, status) tuple on refusal, matching
                # _require_receive_token. An `if checked is None: return checked`
                # call site inverts that and makes the endpoint MORE open, not
                # less -- it was the bug this very PR introduced.
                self.assertIn(
                    "if denied is not None", src,
                    f"{route} must act on the guard's refusal; a None check "
                    "inverts the contract and lets unauthenticated callers through",
                )
                self.assertNotIn(
                    "if checked is None", src,
                    f"{route} uses the inverted guard contract",
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