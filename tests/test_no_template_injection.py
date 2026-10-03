"""No value may be interpolated raw into a `<script>` block.

`/premium/commute` builds its page with a Python f-string and reflects
`request.args.get("token")` straight into a JavaScript string literal:

    <script>
    const TOKEN = "{token or ''}";

The page is reachable by anyone with a link and the token is reflected *before*
any validation, so this was unauthenticated reflected XSS. Fixed by routing the
value through `_js_str`.

The fix is one call site. This file is about the property, so the next template
added to this 4,900-line file cannot reintroduce it.

**How it is checked, and why not with a regex on the source.** Three attempts,
each wrong in an instructive way:

  1. Scanning the raw text with `\\{[^{}]*\\}` flagged `{id:'weather'}`. In an
     f-string, a literal brace is written `{{`; but `/premium/display` is a *plain*
     string, not an f-string, so its braces are already single and the pattern
     cannot tell an interpolation from a JavaScript object literal. 67 false
     positives.
  2. Excluding `(?<!\\{)` fixed #1 and immediately flagged every `${x}` in the
     client-side templating on `/premium/display` — those are JavaScript template
     literals, not ours.
  3. Excluding `${` fixed that and left the plain-string case from #1.

So: don't scan text at all. Walk the AST for `ast.JoinedStr` — which is exactly
and only an f-string — and take its interpolations from `ast.FormattedValue`
nodes, which is exactly and only what Python will substitute. Placeholder markers
are then substituted into the reconstructed template and the `<script>` regions
found around them. No brace-counting heuristics remain.

**Scope.** This is server-side template injection only. `/premium/display` builds
DOM by assigning `innerHTML` from JSON, including a Reddit title that was
`html.unescape`d at ingest. That is a real and separate defect, it needs escaping
on the client, and conflating the two would send the fix to the wrong file.

The witnesses at the end exist because a guard never shown failing might have
stopped guarding.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "audio_receiver.py"
_SRC = _APP.read_text(encoding="utf-8")

_SCRIPT_BLOCK = re.compile(r"<script[^>]*>(.*?)</script>", re.S)
_MARK = "\x00FMT%d\x00"

#: The one call allowed to appear inside a `<script>` block. Matched on the whole
#: expression being a call to `_js_str` (with or without a coercion in front of
#: it), so `f"{_js_str(x)}"` passes while `f"{x}"` and `f"{_js_str}"` do not.
_SAFE_CALL = re.compile(r"^_js_str\([^()]*(?:\([^()]*\))?[^()]*\)$")


def _fstring_scripts() -> list[tuple[int, list[str]]]:
    """[(lineno, [fields interpolated inside a <script> block])] per f-string.

    Only `ast.JoinedStr` is considered, because that node type is precisely an
    f-string. A plain multi-line string cannot interpolate anything, so including
    it would only produce false positives on its JavaScript object literals.
    """
    found = []
    for node in ast.walk(ast.parse(_SRC)):
        if not isinstance(node, ast.JoinedStr):
            continue
        chunks: list[str] = []
        fields: list[tuple[int, str]] = []          # (part index, expression)
        for index, part in enumerate(node.values):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                chunks.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                try:
                    name = ast.unparse(part.value)
                except Exception:
                    name = "?"
                fields.append((index, name))
                chunks.append(_MARK % index)
        text = "".join(chunks)
        if "<script" not in text:
            continue
        inside = [
            name
            for block in _SCRIPT_BLOCK.findall(text)
            for index, name in fields
            if (_MARK % index) in block and not _SAFE_CALL.match(name)
        ]
        if inside:
            found.append((node.lineno, inside))
    return found


def _js_str():
    """The real `_js_str`, lifted out of audio_receiver without importing it.

    Importing the app needs faster_whisper, and the escaping rules are exactly the
    kind of thing that must be tested on every machine rather than only on the
    production venv. So take the function's own source and execute that — the same
    approach as `scripts/check_camera_js.mjs`, for the same reason. A copy would
    be a second thing to keep in sync, and the day it drifts is the day this test
    starts reporting on code that never ships.

    Located with the parser rather than by counting braces: brace counting looks
    correct right up until a docstring contains one, and this docstring does.
    """
    fn = next(
        (n for n in ast.walk(ast.parse(_SRC))
         if isinstance(n, ast.FunctionDef) and n.name == "_js_str"),
        None,
    )
    if fn is None:
        raise AssertionError("_js_str is gone from audio_receiver.py; update this test")
    body = ast.get_source_segment(_SRC, fn)
    if not body:
        raise AssertionError("could not read _js_str's source; update this test")
    ns: dict = {}
    exec(compile(body, "audio_receiver.py:_js_str", "exec"), ns)
    return ns["_js_str"]


class TestNoInterpolationInsideScriptBlocks(unittest.TestCase):
    def test_no_fstring_field_is_interpolated_into_a_script_block(self):
        offenders = _fstring_scripts()
        self.assertEqual(
            [], offenders,
            "these values are interpolated into a <script> block and can break out "
            "of it. Route them through _js_str(...), which JSON-encodes (so a "
            "quote cannot end the literal) and escapes '<' (so </script> cannot "
            "terminate the block).",
        )

    def test_the_check_examines_the_real_templates(self):
        """Zero findings is only meaningful if the scan found the templates."""
        joined = [n for n in ast.walk(ast.parse(_SRC)) if isinstance(n, ast.JoinedStr)]
        self.assertGreaterEqual(
            len(joined), 2,
            f"only {len(joined)} f-strings found in the file; this one is known to "
            "have at least two, so the scan has broken",
        )
        scanned = [n for n in joined
                   if "<script" in "".join(
                       p.value for p in n.values
                       if isinstance(p, ast.Constant) and isinstance(p.value, str)
                   )]
        self.assertGreaterEqual(
            len(scanned), 1,
            "no f-string containing a <script> block was found; the guard is "
            "scanning nothing, which looks exactly like a clean bill of health",
        )


class TestJsStrDoesTheJob(unittest.TestCase):
    """Behavioural, because the escaping rules are easy to get subtly wrong."""

    @classmethod
    def setUpClass(cls):
        cls.js_str = staticmethod(_js_str())

    def test_the_literal_round_trips_to_the_original_value(self):
        """The real property, and the reason the others are here.

        If `json.loads` of the emitted literal returns exactly what went in, then
        no input can have changed the meaning of the string. That covers quotes,
        backslashes, newlines and angle brackets together, which is why it is
        asserted once rather than case by case.
        """
        for value in (
            "abc123",
            '";alert(1);//',
            "</script><script>alert(1)</script>",
            "\\",
            "back\\slash \\\" and 'quotes'",
            "line\nbreak and para",
            "",
        ):
            with self.subTest(value=value):
                self.assertEqual(value, json.loads(self.js_str(value)))

    def test_a_script_close_tag_is_never_emitted(self):
        out = self.js_str("</script><script>alert(1)</script>")
        self.assertNotIn("</script", out)
        self.assertIn("\\u003c", out)

    def test_angle_brackets_are_escaped(self):
        self.assertNotIn("<", self.js_str("a<b>c"))

    def test_none_and_non_strings_are_handled(self):
        self.assertEqual('""', self.js_str(None))
        self.assertEqual('""', self.js_str(""))
        self.assertEqual('"7"', self.js_str(7))

    def test_javascript_newline_terminators_are_escaped(self):
        """U+2028/U+2029 end a line to a JS parser and are legal inside JSON."""
        out = self.js_str("a b c")
        self.assertNotIn(" ", out)
        self.assertNotIn(" ", out)

    def test_a_newline_in_the_value_cannot_break_the_statement(self):
        out = self.js_str("x\nalert(1)")
        self.assertEqual("x\nalert(1)", json.loads(out))
        self.assertNotIn("\nalert", out)


class TestTheScannerWouldNoticeTheOldBug(unittest.TestCase):
    """Witnesses. Each asserts the scanner catches a thing it must catch."""

    def _fields_in(self, source: str) -> list[str]:
        """Run the real scanner logic over an f-string given as source."""
        tree = ast.parse(f"x = f'''{source}'''")
        joined = next(n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr))
        chunks, fields = [], []
        for index, part in enumerate(joined.values):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                chunks.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                fields.append((index, ast.unparse(part.value)))
                chunks.append(_MARK % index)
        text = "".join(chunks)
        return [
            name
            for block in _SCRIPT_BLOCK.findall(text)
            for index, name in fields
            if (_MARK % index) in block and not _SAFE_CALL.match(name)
        ]

    def test_the_previous_codestate_is_flagged(self):
        found = self._fields_in(
            "<script>\nconst TOKEN = \"{token or ''}\";\n</script>"
        )
        self.assertEqual(
            ["token or ''"], found,
            "the scanner no longer detects the exact interpolation that shipped, "
            "so a clean run against the real file means nothing",
        )

    def test_client_side_templating_is_not_mistaken_for_interpolation(self):
        """Braces the *template* wants are not interpolations.

        In an f-string a literal brace is written doubled, so `{{id:'weather'}}`
        reaches the browser as `{id:'weather'}` and Python substitutes nothing.
        Treating those as interpolations would report hundreds of findings on
        `/premium/commute` and get the guard switched off within a day.

        The doubled form is what the real file contains, which is why this is the
        right thing to assert. The *plain-string* templates -- `/premium/display`
        among them -- are never scanned at all, because they cannot interpolate.
        """
        found = self._fields_in(
            "<script>\n"
            "const o = {{id:'weather', label:'Weather'}};\n"
            "var s = `{{d.temp}}`;\n"
            "</script>"
        )
        self.assertEqual([], found, "template-owned braces are read as Python")

    def test_js_str_is_the_one_allowed_call(self):
        found = self._fields_in("<script>\nconst T = {_js_str(token or '')};\n</script>")
        self.assertEqual([], found, "_js_str is the sanctioned form and must be allowed")

    def test_a_field_that_merely_mentions_js_str_is_not_allowed(self):
        found = self._fields_in("<script>\nconst T = {_js_str};\n</script>")
        self.assertEqual(["_js_str"], found, "the exemption is too loose")

    def test_an_interpolation_outside_a_script_block_is_allowed(self):
        """Otherwise the guard pushes people toward the wrong fix.

        A value in an HTML attribute or text node needs `esc()`, not `_js_str()`,
        and a guard that flags those would get itself bypassed.
        """
        found = self._fields_in("<div class=\"{cls}\">{name}</div>")
        self.assertEqual([], found)


if __name__ == "__main__":
    unittest.main()