"""C3 public-feed stored-XSS regression tests (slate item C3).

The public feed is reachable by every visitor and untrusted input reaches its
HTML unescaped, in two halves:

  INGEST  POST /receive accepts an unauthenticated ``tag`` and stores it
          almost verbatim (only ``TGID <digits>`` is discarded).
  OUTPUT  modules/public.py builds the incident/call/tip feed by raw template
          interpolation (``${inc.itype}``, ``${inc.location}``,
          ``${agencies}``, ``${inc.description}``, call tags/transcripts,
          tip titles/urls/...) into innerHTML.

A previous esc() shipped identity mappings (escaping nothing) and another was
test-only, so every test below asserts on the ESCAPED STRING / rendered output
itself, never merely that a page loaded. The JS harnesses drive the REAL
shipped script (external static/js asset when present, falling back to the
inline <script> block so the test fails loudly on the vulnerable layout),
under Node with stubbed DOM globals — the same approach as
tests/test_public_map_unlocated.py and tests/test_homicide_map_js.py.

No database outside temp state, no network, no running service. The ingest
half runs in a child process and pins the synchronous backlog path of
POST /receive, which performs no DB write and no transcription.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from html.parser import HTMLParser
from pathlib import Path

import modules.public as public

_ROOT = Path(__file__).resolve().parent.parent
_NODE = shutil.which("node") or shutil.which("nodejs")

# Payload must contain < > " ' & plus an event handler: anything that
# survives to innerHTML unescaped executes in every visitor's browser.
XSS_PAYLOAD = (
    '\'><img src=x onerror=alert(1)>&"\''
    "<script>alert('xss')</script>"
    '<svg onload=alert(2)>'
)

_BENIGN = {
    "itype": "SHOOTING",
    "status": "active",
    "location": "700 W 6th St",
    "agencies": '["APD", "TCEMS"]',
    "description": "Shots fired, multiple units responding.",
    "tag": "AFD Firecom N",
    "category": "AFD",
    "transcript": "Engine 4 on scene, nothing showing.",
}


# ---------------------------------------------------------------------------
# Shipped-script loaders (external asset first, inline fallback)
# ---------------------------------------------------------------------------

def _feed_script() -> str:
    """Return the real feed-page script browsers run."""
    for name in ("public_feed.js", "feed.js"):
        p = _ROOT / "static" / "js" / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", public.PUBLIC_FEED_HTML, re.S)
    assert scripts, "no feed script found (neither static/js asset nor inline block)"
    return max(scripts, key=len)


def _map_script() -> str:
    """Return the real live-map script browsers run."""
    for name in ("public_map.js", "map.js"):
        p = _ROOT / "static" / "js" / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", public.PUBLIC_MAP_HTML, re.S)
    assert scripts, "no map script found (neither static/js asset nor inline block)"
    return max(scripts, key=len)


# ---------------------------------------------------------------------------
# Minimal DOM stub shared by the feed/map harnesses.
#
# Elements support the DOM-construction APIs the fixed scripts must use
# (createElement / createTextNode / textContent / setAttribute /
# appendChild / replaceChildren / addEventListener). Setting innerHTML is
# still supported so the harness can also execute the VULNERABLE layout and
# observe the raw payload reaching it.
# ---------------------------------------------------------------------------

_HARNESS_PREAMBLE = r"""
const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
const dataPath = process.argv[3];
const source = fs.readFileSync(jsPath, 'utf8');
const fixtures = JSON.parse(fs.readFileSync(dataPath, 'utf8'));

function escAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function makeNode(tag, text) {
  const node = {
    nodeType: tag === '#text' ? 3 : 1,
    tag: tag,
    children: [],
    attrs: {},
    className: '',
    style: {},
    _text: text || '',
    _innerHTML: null,
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    getAttribute: function (k) { return this.attrs[k]; },
    appendChild: function (c) { this.children.push(c); return c; },
    classList: { add: function () {}, remove: function () {}, toggle: function () {} },
    removeChild: function (c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    replaceChildren: function () { this.children = []; this._text = ''; this._innerHTML = null; },
    addEventListener: function () {},
    removeEventListener: function () {},
  };
  Object.defineProperty(node, 'textContent', {
    get: function () { return this._text; },
    set: function (v) { this._text = String(v); this.children = []; this._innerHTML = null; },
  });
  Object.defineProperty(node, 'innerHTML', {
    get: function () { return this._innerHTML; },
    set: function (v) { this._innerHTML = String(v); this.children = []; this._text = ''; },
  });
  Object.defineProperty(node, 'firstChild', {
    get: function () { return this.children.length ? this.children[0] : null; },
  });
  return node;
}

function serialize(node) {
  if (!node) return '';
  if (node.nodeType === 3) return escText(node._text);
  if (node._innerHTML !== null && node._innerHTML !== undefined) return node._innerHTML;
  const attrs = Object.keys(node.attrs).map(k => ' ' + k + '="' + escAttr(node.attrs[k]) + '"').join('');
  const cls = node.className ? ' class="' + escAttr(node.className) + '"' : '';
  // Inline styles are allowlisted constants in the fixed scripts; serialize
  // them so a hostile value smuggled into style would be visible.
  const styleKeys = Object.keys(node.style || {});
  const style = styleKeys.length
    ? ' style="' + escAttr(styleKeys.map(k => k + ':' + node.style[k]).join(';')) + '"' : '';
  const inner = escText(node._text) + node.children.map(serialize).join('');
  return '<' + (node.tag || 'div') + cls + attrs + style + '>' + inner + '</' + (node.tag || 'div') + '>';
}

const elements = {};
const document = {
  getElementById: function (id) {
    if (!(id in elements)) {
      elements[id] = makeNode('div');
      elements[id].id = id;
    }
    return elements[id];
  },
  createElement: function (tag) { return makeNode(tag); },
  createTextNode: function (t) { return makeNode('#text', String(t)); },
};
"""

_FEED_HARNESS = _HARNESS_PREAMBLE + r"""
const sandbox = {
  document: document,
  console: console,
  setInterval: function () { return 0; },
  setTimeout: setTimeout,
  Date: Date,
  Math: Math,
  JSON: JSON,
  URL: URL,
  fetch: function (url) {
    let body = [];
    if (url === '/api/calls') body = fixtures.calls || [];
    else if (url === '/api/incidents/active') body = fixtures.active || [];
    else if (url === '/api/incidents') body = fixtures.all || [];
    else if (url === '/api/reddit_tips') body = fixtures.tips || [];
    else if (url === '/api/stats') body = { calls_24h: 0, incidents_24h: 0 };
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'public-feed-under-test.js' });

setImmediate(function () {
  setImmediate(function () {
    const out = {};
    for (const id of Object.keys(elements)) out[id] = serialize(elements[id]);
    let probes = null;
    if (fixtures.probe !== undefined && fixtures.probe !== null) {
      probes = {};
      if (typeof sandbox.esc === 'function') {
        try { probes.escaped = sandbox.esc(fixtures.probe); } catch (e) { probes.escaped = 'THREW:' + e; }
      }
      if (typeof sandbox.safeUrl === 'function') {
        try { probes.safe = sandbox.safeUrl(fixtures.probe); } catch (e) { probes.safe = 'THREW:' + e; }
      }
    }
    process.stdout.write(JSON.stringify({ html: out, probes: probes }));
  });
});
"""

_MAP_POPUP_HARNESS = _HARNESS_PREAMBLE + r"""
const popups = [];
const markers = [];

const chainable = function () {
  const o = {};
  o.addTo = function () { return o; };
  o.bindPopup = function (h) {
    popups.push(typeof h === 'string' ? h : serialize(h));
    return o;
  };
  o.removeLayer = function () { return o; };
  o.setView = function () { return o; };
  o.setLatLng = function () { return o; };
  o.setIcon = function () { return o; };
  o.setPopupContent = function (h) {
    popups.push(typeof h === 'string' ? h : serialize(h));
    return o;
  };
  return o;
};

const L = {
  latLng: function (lat, lon) { return { lat: lat, lng: lon }; },
  latLngBounds: function (sw, ne) {
    return {
      contains: function (p) {
        const lat = p[0], lon = p[1];
        return lat >= sw.lat && lat <= ne.lat && lon >= sw.lng && lon <= ne.lng;
      }
    };
  },
  map: function () { return chainable(); },
  tileLayer: function () { return chainable(); },
  heatLayer: function () { return chainable(); },
  divIcon: function (options) { return options; },
  marker: function (latlng) {
    markers.push({ lat: latlng[0], lon: latlng[1] });
    return chainable();
  },
};

const sandbox = {
  L: L,
  document: document,
  console: console,
  setInterval: function () { return 0; },
  setTimeout: setTimeout,
  localStorage: { getItem: function () { return null; }, setItem: function () {} },
  speechSynthesis: { getVoices: function () { return []; }, cancel: function () {}, speak: function () {} },
  Date: Date,
  Math: Math,
  JSON: JSON,
  URL: URL,
  fetch: function (url) {
    let body = {};
    if (url === '/api/incidents/active') body = fixtures.active || [];
    else if (url === '/api/incidents') body = fixtures.all || [];
    else if (url === '/api/calls') body = fixtures.calls || [];
    else if (url === '/api/stats') body = { calls_24h: 0, incidents_24h: 0, agencies_24h: 0 };
    else if (url === '/api/voice_sitrep') body = { text: 'ok' };
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.addEventListener = function () {};

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'public-map-under-test.js' });

setImmediate(function () {
  setImmediate(function () {
    let probes = null;
    if (fixtures.probe !== undefined && fixtures.probe !== null) {
      probes = {};
      if (typeof sandbox.esc === 'function') {
        try { probes.escaped = sandbox.esc(fixtures.probe); } catch (e) { probes.escaped = 'THREW:' + e; }
      }
      if (typeof sandbox.safeUrl === 'function') {
        try { probes.safe = sandbox.safeUrl(fixtures.probe); } catch (e) { probes.safe = 'THREW:' + e; }
      }
    }
    process.stdout.write(JSON.stringify({ popups: popups, markers: markers, probes: probes }));
  });
});
"""


def _scratch_dir():
    # Never touch /tmp in this environment: keep all scratch inside the repo
    # worktree (deleted before commit; never committed).
    d = _ROOT / ".test-scratch-c3"
    d.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(d))


def _run_js(script_source: str, harness: str, fixtures: dict) -> dict:
    if not _NODE:
        raise unittest.SkipTest("node is not installed; skipping shipped-JS execution")
    with _scratch_dir() as tmp:
        js_path = Path(tmp) / "script.js"
        data_path = Path(tmp) / "fixtures.json"
        harness_path = Path(tmp) / "harness.js"
        js_path.write_text(script_source, encoding="utf-8")
        data_path.write_text(json.dumps(fixtures), encoding="utf-8")
        harness_path.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [_NODE, str(harness_path), str(js_path), str(data_path)],
            capture_output=True, text=True, timeout=60, cwd=_ROOT,
        )
    if proc.returncode != 0:
        raise AssertionError(f"JS harness failed ({proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _run_feed(fixtures: dict) -> dict:
    return _run_js(_feed_script(), _FEED_HARNESS, fixtures)


def _run_map(fixtures: dict) -> dict:
    return _run_js(_map_script(), _MAP_POPUP_HARNESS, fixtures)


def _evil_incident(iid: int = 7) -> dict:
    return {
        "id": iid,
        "itype": f"SHOOTING{XSS_PAYLOAD}",
        "ts_start": 1_700_000_000.0,
        "status": "active",
        "is_test": 0,
        "location": f"downtown{XSS_PAYLOAD}",
        "lat": 30.2672,
        "lon": -97.7431,
        "agencies": json.dumps([f"APD{XSS_PAYLOAD}"]),
        "description": f"shots fired{XSS_PAYLOAD}",
    }


def _evil_call() -> dict:
    return {
        "ts": 1_700_000_000.0,
        "tgid": 4242,
        "tag": f"AFD Firecom{XSS_PAYLOAD}",
        "category": f"AFD{XSS_PAYLOAD}",
        "transcript": f"engine on scene{XSS_PAYLOAD}",
        "location": f"6th street{XSS_PAYLOAD}",
        "lat": 30.2672,
        "lon": -97.7431,
    }


def _benign_incident() -> dict:
    return {
        "id": 1,
        "itype": _BENIGN["itype"],
        "ts_start": 1_700_000_000.0,
        "status": "active",
        "is_test": 0,
        "location": _BENIGN["location"],
        "lat": 30.2672,
        "lon": -97.7431,
        "agencies": _BENIGN["agencies"],
        "description": _BENIGN["description"],
    }


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.anchors.append({k: (v or "") for k, v in attrs})


def _anchors(html: str) -> list[dict[str, str]]:
    p = _AnchorParser()
    p.feed(html or "")
    p.close()
    return p.anchors


# ---------------------------------------------------------------------------
# A. Ingest: the client-supplied tag must not reach storage with markup.
# ---------------------------------------------------------------------------

_INGEST_CHILD = textwrap.dedent(
    """
    import base64
    import io
    import json
    import os
    import sys
    import wave
    from unittest import mock

    sys.modules["stripe"] = mock.MagicMock()

    import audio_receiver
    from modules import transcription as tr

    def make_wav_b64():
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(os.urandom(2 * 8000 * 6 // 10))  # 0.6s, unique hash
        return base64.b64encode(buf.getvalue()).decode()

    # Force the synchronous backlog path (no DB write, no transcription):
    # with the broadcastify semaphore exhausted, /receive queues the item.
    held = 0
    while tr._broadcastify_sem.acquire(blocking=False):
        held += 1

    client = audio_receiver.app.test_client()
    payload = json.loads(os.environ["TEST_TAGS_JSON"])
    out = []
    for entry in payload:
        before = len(audio_receiver._backlog_queue)
        r = client.post("/receive", json={
            "audio_b64": make_wav_b64(),
            "tgid": 4242,
            "tag": entry,
            "node": "test-node",
        })
        stored = None
        if len(audio_receiver._backlog_queue) > before:
            stored = audio_receiver._backlog_queue[-1]["tag"]
        out.append({"status": r.status_code, "stored": stored})
    print(json.dumps(out))
    """
)


def _drive_ingest(tags: list) -> list:
    with _scratch_dir() as tmp:
        child = Path(tmp) / "ingest_child.py"
        child.write_text(_INGEST_CHILD, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(child)],
            capture_output=True, text=True, timeout=180, cwd=_ROOT,
            env={**os.environ, "TEST_TAGS_JSON": json.dumps(tags),
                 "PYTHONPATH": str(_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )
    if proc.returncode != 0:
        raise AssertionError(f"ingest child failed ({proc.returncode}): {proc.stderr[-3000:]}")
    return json.loads(proc.stdout.splitlines()[-1])


class IngestTagTests(unittest.TestCase):
    """POST /receive is unauthenticated; the client tag must be allowlisted."""

    def test_markup_tag_is_neutralised_before_storage(self):
        tags = [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            f"AFD Firecom{XSS_PAYLOAD}",
        ]
        results = _drive_ingest(tags)
        # Aggregate: a parent that passes while children fail is false
        # evidence, so every subtest failure fails this test.
        failures = []
        for entry, result in zip(tags, results):
            with self.subTest(tag=entry[:40]):
                try:
                    self.assertEqual(result["status"], 202)
                    stored = result["stored"]
                    self.assertIsNotNone(stored, "expected the item to be queued with a tag")
                    for ch in ("<", ">", '"', "'", "&"):
                        self.assertNotIn(ch, stored, f"markup char {ch!r} reached storage")
                    self.assertNotIn("onerror", stored)
                    self.assertNotIn("script", stored.lower())
                except AssertionError as e:
                    failures.append(f"{entry[:40]!r}: {e}")
        self.assertEqual(failures, [], f"{len(failures)} subtest(s) failed")

    def test_overlong_tag_is_discarded_to_server_label(self):
        results = _drive_ingest(["A" * 500, "x" * 65])
        for result in results:
            self.assertEqual(result["status"], 202)
            stored = result["stored"] or ""
            self.assertLessEqual(len(stored), 64, "unbounded attacker string reached storage")
            self.assertNotIn("A" * 10, stored)

    def test_legitimate_tag_still_passes_through(self):
        results = _drive_ingest(["AFD Firecom N", "Austin PD Tac 2", "Blanco FD/EMS"])
        failures = []
        for tag, result in zip(["AFD Firecom N", "Austin PD Tac 2", "Blanco FD/EMS"], results):
            with self.subTest(tag=tag):
                try:
                    self.assertEqual(result["status"], 202)
                    self.assertEqual(result["stored"], tag)
                except AssertionError as e:
                    failures.append(f"{tag!r}: {e}")
        self.assertEqual(failures, [], f"{len(failures)} subtest(s) failed")


# ---------------------------------------------------------------------------
# B. Output: every attacker-influenced interpolation renders as inert text.
# ---------------------------------------------------------------------------

class FeedEscapingTests(unittest.TestCase):
    """Malicious incident/call/tip fields must render as inert text."""

    def test_malicious_incident_fields_are_inert_in_feed(self):
        evil = _evil_incident()
        result = _run_feed({"all": [evil], "active": [evil], "calls": [], "tips": []})
        rendered = (result["html"].get("incidents-section") or "")
        self.assertNotIn(XSS_PAYLOAD, rendered, "raw payload reached the incident card")
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("onerror=", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("<svg", rendered)
        # The data is still there, in escaped form — escaping blanked nothing.
        self.assertIn("&lt;", rendered)
        self.assertIn("&gt;", rendered)
        self.assertIn("SHOOTING", rendered)

    def test_malicious_call_tag_transcript_location_are_inert(self):
        call = _evil_call()
        result = _run_feed({"all": [], "active": [], "calls": [call], "tips": []})
        rendered = (result["html"].get("feed-section") or "")
        self.assertNotIn(XSS_PAYLOAD, rendered, "raw payload reached the call row")
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("onerror=", rendered)
        self.assertIn("&lt;", rendered)
        self.assertIn("AFD Firecom", rendered)

    def test_malicious_tip_title_location_summary_are_inert(self):
        tips = [{
            "ts": 1_700_000_000.0,
            "post_id": "x1",
            "subreddit": f"Austin{XSS_PAYLOAD}",
            "title": f"big incident{XSS_PAYLOAD}",
            "url": "https://www.reddit.com/r/Austin/comments/x1/test/",
            "author": "someone",
            "tip_status": "matched",
            "tip_location": f"6th st{XSS_PAYLOAD}",
            "tip_summary": f"heard shots{XSS_PAYLOAD}",
        }]
        result = _run_feed({"all": [], "active": [], "calls": [], "tips": tips})
        rendered = (result["html"].get("tips-section") or "")
        self.assertNotIn(XSS_PAYLOAD, rendered, "raw payload reached the tip card")
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("onerror=", rendered)
        self.assertIn("&lt;", rendered)

    def test_feed_renders_normal_incident_correctly(self):
        """Escaping must not blank the page: a normal incident stays readable."""
        inc = _benign_incident()
        call = {
            "ts": 1_700_000_000.0, "tgid": 101, "tag": _BENIGN["tag"],
            "category": _BENIGN["category"], "transcript": _BENIGN["transcript"],
            "location": _BENIGN["location"], "lat": 30.26, "lon": -97.74,
        }
        result = _run_feed({"all": [inc], "active": [inc], "calls": [call], "tips": []})
        inc_html = result["html"].get("incidents-section") or ""
        feed_html = result["html"].get("feed-section") or ""
        for needle in ("SHOOTING", "700 W 6th St", "Shots fired", "APD"):
            self.assertIn(needle, inc_html, f"normal incident lost {needle!r}")
        for needle in ("AFD Firecom N", "Engine 4 on scene"):
            self.assertIn(needle, feed_html, f"normal call lost {needle!r}")

    def test_feed_escaper_maps_every_html_significant_char(self):
        """The esc() helper must escape, never identity-map (the old trap)."""
        result = _run_feed({"all": [], "active": [], "calls": [],
                            "tips": [], "probe": XSS_PAYLOAD})
        probes = result.get("probes") or {}
        escaped = probes.get("escaped")
        self.assertIsNotNone(escaped, "feed script exposes no esc() helper")
        self.assertNotIn("<script>", escaped)
        self.assertNotIn("<img", escaped)
        for ch in ("<", ">", '"', "'"):
            self.assertNotIn(ch, escaped, f"esc() left a raw {ch!r}")
        for entity in ("&lt;", "&gt;", "&quot;", "&amp;"):
            self.assertIn(entity, escaped, f"esc() missing entity {entity}")


class MapPopupEscapingTests(unittest.TestCase):
    """The map popup (modules/public.py:750-755 region) must not execute."""

    def test_malicious_popup_fields_are_inert(self):
        evil = _evil_incident()
        result = _run_map({"active": [evil], "all": [evil], "calls": []})
        self.assertTrue(result["popups"], "expected a popup for the mappable incident")
        failures = []
        for popup in result["popups"]:
            with self.subTest(popup=popup[:60]):
                try:
                    self.assertNotIn(XSS_PAYLOAD, popup)
                    self.assertNotIn("<script>", popup)
                    self.assertNotIn("onerror=", popup)
                    self.assertNotIn("<img", popup)
                    self.assertNotIn("<svg", popup)
                except AssertionError as e:
                    failures.append(f"{popup[:60]!r}: {e}")
        self.assertEqual(failures, [], f"{len(failures)} subtest(s) failed")
        joined = "\n".join(result["popups"])
        self.assertIn("&lt;", joined)
        self.assertIn("SHOOTING", joined)

    def test_map_popup_has_no_inline_event_handler(self):
        evil = _evil_incident()
        result = _run_map({"active": [evil], "all": [evil], "calls": []})
        for popup in result["popups"]:
            self.assertNotIn("onclick=", popup.lower(), "inline handler in map popup")
            self.assertNotIn("onmouseover=", popup.lower())


# ---------------------------------------------------------------------------
# C. URLs: javascript:/data: must never appear in an emitted href/src.
# ---------------------------------------------------------------------------

class FeedUrlValidationTests(unittest.TestCase):
    def test_hostile_tip_urls_produce_no_usable_href(self):
        failures = []
        for hostile in (
            "javascript:alert(1)",
            "  javascript:alert(1)  ",
            "JaVaScRiPt:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
        ):
            with self.subTest(url=hostile):
                try:
                    tips = [{
                        "ts": 1_700_000_000.0, "post_id": "x1", "subreddit": "Austin",
                        "title": "tip", "url": hostile, "author": "a",
                        "tip_status": "matched", "tip_summary": "match",
                    }]
                    result = _run_feed({"all": [], "active": [], "calls": [], "tips": tips})
                    rendered = (result["html"].get("tips-section") or "").lower()
                    self.assertNotIn("javascript:", rendered)
                    self.assertNotIn("data:text/html", rendered)
                    for anchor in _anchors(result["html"].get("tips-section") or ""):
                        scheme = anchor.get("href", "").split(":", 1)[0].lower()
                        self.assertIn(scheme, ("http", "https", ""),
                                      f"non-http(s) href emitted: {anchor.get('href')!r}")
                        self.assertNotIn("javascript", anchor.get("href", "").lower())
                except AssertionError as e:
                    failures.append(f"{hostile!r}: {e}")
        self.assertEqual(failures, [], f"{len(failures)} subtest(s) failed")

    def test_safe_url_helper_rejects_non_http_schemes(self):
        result = _run_feed({"all": [], "active": [], "calls": [],
                            "tips": [], "probe": "javascript:alert(1)"})
        probes = result.get("probes") or {}
        self.assertEqual(probes.get("safe"), "",
                         "safeUrl must return '' for a javascript: URL")
        result = _run_feed({"all": [], "active": [], "calls": [],
                            "tips": [], "probe": "data:text/html,x"})
        self.assertEqual((result.get("probes") or {}).get("safe"), "",
                         "safeUrl must return '' for a data: URL")

    def test_no_javascript_or_data_href_in_map_popups(self):
        evil = _evil_incident()
        result = _run_map({"active": [evil], "all": [evil], "calls": []})
        for popup in result["popups"]:
            lowered = popup.lower()
            self.assertNotIn("javascript:", lowered)
            self.assertNotIn("data:text/html", lowered)


# ---------------------------------------------------------------------------
# D. CSP for the public surface: no inline script may run.
# ---------------------------------------------------------------------------

def _public_app():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(public.public_bp)
    return app


class PublicCspTests(unittest.TestCase):
    PAGES = ("/splash", "/public", "/public/feed", "/public/about", "/public/homicides")

    def test_csp_header_present_without_unsafe_inline_script(self):
        app = _public_app()
        client = app.test_client()
        failures = []
        for page in self.PAGES:
            with self.subTest(page=page):
                try:
                    r = client.get(page)
                    self.assertEqual(r.status_code, 200)
                    csp = r.headers.get("Content-Security-Policy", "")
                    self.assertTrue(csp, f"{page} sets no Content-Security-Policy")
                    m = re.search(r"script-src([^;]*)", csp)
                    self.assertIsNotNone(m, f"{page}: CSP has no script-src directive")
                    self.assertNotIn("'unsafe-inline'", m.group(1),
                                     f"{page}: CSP script-src allows inline script")
                except AssertionError as e:
                    failures.append(f"{page!r}: {e}")
        self.assertEqual(failures, [], f"{len(failures)} subtest(s) failed")

    def test_public_pages_carry_no_inline_script_or_handlers(self):
        failures = []
        for name in ("PUBLIC_SPLASH_HTML", "PUBLIC_MAP_HTML", "PUBLIC_FEED_HTML",
                     "PUBLIC_ABOUT_HTML", "HOMICIDE_MAP_HTML"):
            with self.subTest(page=name):
                try:
                    html = getattr(public, name)
                    inlines = [s for s in re.findall(r"<script>(.*?)</script>", html, re.S)
                               if s.strip()]
                    self.assertEqual(inlines, [], f"{name} still ships inline <script>")
                    self.assertNotIn("onclick=", html.lower(), f"{name} still has onclick=")
                except AssertionError as e:
                    failures.append(f"{name!r}: {e}")
        self.assertEqual(failures, [], f"{len(failures)} subtest(s) failed")


if __name__ == "__main__":
    unittest.main()
