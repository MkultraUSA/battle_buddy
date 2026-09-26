"""Tests for the homicide map's real front-end surface, static/js/homicides.js.

tests/test_aircraft_module.py pins the Esri basemap and the hardened nav links
on the *rendered HTML constants* in modules/public.py and templates/. The
homicide map is different: its basemap and its only runtime-generated link live
in an external script asset, so asserting on HOMICIDE_MAP_HTML alone proves
nothing about what the browser actually runs. These tests load the shipped
asset itself — statically for the tile contract, and by executing it under
Node against stubbed Leaflet/DOM/fetch globals for the popup HTML.

No database, no network, no running service.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

import modules.public as public

_ROOT = Path(__file__).parent.parent
_JS_PATH = _ROOT / "static" / "js" / "homicides.js"

# The preserved Esri basemap contract, identical to the one pinned for the
# other public maps: Esri's tile service is {z}/{y}/{x}, the opposite of the
# OpenStreetMap leaflet {z}/{x}/{y} order.
_ESRI_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
)
_ESRI_TILE_HOST = "server.arcgisonline.com"
_OSM_LEAFLET_HOST = "tile.openstreetmap.org"
_ESRI_CREDITS = ("Tiles &copy; Esri", "Esri", "HERE", "Garmin", "OpenStreetMap contributors")

# The unhardened anchor shape this change must never come back to, and the
# hardened shape that must replace it.
_UNHARDENED_ANCHOR = 'target="_blank">'
_HARDENED_ANCHOR = 'target="_blank" rel="noopener"'

_NODE = shutil.which("node") or shutil.which("nodejs")

# Executes the shipped script with stubbed Leaflet/DOM/fetch globals, feeds it
# the homicide records in argv[3], and prints the tile layers it requested plus
# the popup HTML it built. No regexes and no backslash escapes in the script
# under test are involved; this harness only has to provide its globals.
_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
const dataPath = process.argv[3];
const source = fs.readFileSync(jsPath, 'utf8');
const records = JSON.parse(fs.readFileSync(dataPath, 'utf8'));

const tileLayers = [];
const popups = [];

const chainable = () => ({ addTo: function () { return this; } });

const L = {
  map: function () { return Object.assign(chainable(), { removeLayer: function () {} }); },
  tileLayer: function (url, options) {
    tileLayers.push({ url: url, options: options || {} });
    return chainable();
  },
  layerGroup: function () {
    return Object.assign(chainable(), { clearLayers: function () {} });
  },
  heatLayer: function () { return chainable(); },
  divIcon: function (options) { return options; },
  marker: function () {
    const marker = chainable();
    marker.bindPopup = function (html) { popups.push(html); return marker; };
    return marker;
  }
};

const element = function () {
  return { textContent: '', classList: { toggle: function () {} } };
};

const sandbox = {
  L: L,
  document: { getElementById: function () { return element(); } },
  fetch: function () {
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ homicides: records }); } });
  },
  URL: URL,
  console: console
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'homicides.js' });

// The script calls load() itself; let its awaits settle, then drain again in
// case a further microtask was queued by the stubbed json().
setImmediate(function () {
  setImmediate(function () {
    // The map boots in heat mode, which builds no markers. Switch to markers
    // so the popup HTML is actually generated. setMode is a top-level function
    // declaration, so it is reachable on the vm global.
    popups.length = 0;
    if (typeof sandbox.setMode === 'function') {
      sandbox.setMode('markers');
    }

    // Optional probe (argv[4]) exercises the two helpers directly, so the
    // escaping layer is covered on its own and not only via URL normalisation.
    var probe = process.argv.length > 4 ? process.argv[4] : null;
    var probes = null;
    if (probe !== null) {
      probes = {
        escaped: typeof sandbox.escapeHtml === 'function' ? sandbox.escapeHtml(probe) : null,
        safe: typeof sandbox.safeHttpUrl === 'function' ? sandbox.safeHttpUrl(probe) : null
      };
    }

    process.stdout.write(JSON.stringify({ tileLayers: tileLayers, popups: popups, probes: probes }));
  });
});
"""


class _AnchorParser(HTMLParser):
    """Collect every anchor in a generated popup with its attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        self.anchors.append({k: (v or "") for k, v in attrs})


def _anchors(html: str) -> list[dict[str, str]]:
    parser = _AnchorParser()
    parser.feed(html)
    parser.close()
    return parser.anchors


def _attribution(js: str) -> str:
    match = re.search(r"attribution:\s*'([^']*)'", js)
    assert match, "no Leaflet tileLayer attribution string found in homicides.js"
    return match.group(1)


def _run_homicide_js(records: list[dict], probe: str | None = None) -> dict:
    """Execute the shipped homicides.js against *records* and capture its output.

    *probe*, when given, is also passed through the script's own escapeHtml and
    safeHttpUrl helpers so each layer can be asserted on its own.

    Raises unittest.SkipTest when Node is unavailable, so the static contract
    tests still run on a machine without it.
    """
    if not _NODE:
        raise unittest.SkipTest("node is not installed; skipping homicides.js execution")
    with tempfile.TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "records.json"
        harness_path = Path(tmp) / "harness.js"
        data_path.write_text(json.dumps(records), encoding="utf-8")
        harness_path.write_text(_HARNESS_JS, encoding="utf-8")
        argv = [_NODE, str(harness_path), str(_JS_PATH), str(data_path)]
        if probe is not None:
            argv.append(probe)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"homicides.js harness failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _render_popups(url: str | None) -> list[str]:
    """Run the real script with one incident whose press-release URL is *url*."""
    record = {
        "n": 1,
        "date": "2026-01-15",
        "lat": 30.2672,
        "lon": -97.7431,
        "means": "SHOOTING",
        "address": "700 W 6th St",
        "summary": "Test incident",
        "source": "apd",
    }
    if url is not None:
        record["url"] = url
    return _run_homicide_js([record])["popups"]


class HomicideMapBasemapTests(unittest.TestCase):
    """The shipped asset must keep the Esri basemap the other maps use."""

    def setUp(self):
        self.js = _JS_PATH.read_text(encoding="utf-8")

    def test_homicide_js_uses_the_esri_street_map_tile_template(self):
        self.assertIn(
            _ESRI_TILE_URL, self.js, "homicides.js: Esri tile URL is missing or reordered"
        )
        self.assertIn("{z}/{y}/{x}", self.js, "homicides.js: Esri tile path must be {z}/{y}/{x}")
        self.assertIn(_ESRI_TILE_HOST, self.js)
        self.assertNotIn(
            _OSM_LEAFLET_HOST,
            self.js,
            "homicides.js: the OpenStreetMap leaflet tile host came back; the basemap must stay Esri",
        )

    def test_homicide_js_keeps_the_full_esri_attribution(self):
        attribution = _attribution(self.js)
        for credit in _ESRI_CREDITS:
            self.assertIn(credit, attribution, f"homicides.js: attribution is missing {credit!r}")

    def test_executed_js_requests_the_esri_basemap_at_runtime(self):
        tile_layers = _run_homicide_js([])["tileLayers"]
        self.assertEqual(len(tile_layers), 1, "expected exactly one tile layer")
        layer = tile_layers[0]
        self.assertEqual(
            layer["url"], _ESRI_TILE_URL, "runtime tile URL host or {z}/{y}/{x} order changed"
        )
        self.assertNotIn(_OSM_LEAFLET_HOST, layer["url"])
        for credit in _ESRI_CREDITS:
            self.assertIn(
                credit,
                layer["options"].get("attribution", ""),
                f"runtime attribution missing {credit!r}",
            )

    def test_homicide_page_serves_the_tested_asset(self):
        """Tie the tested file to the page that actually ships it."""
        self.assertIn('src="/static/js/homicides.js', public.HOMICIDE_MAP_HTML)


class HomicideMapLinkHardeningTests(unittest.TestCase):
    """The runtime-generated APD link must be escaped and rel=noopener."""

    def setUp(self):
        self.js = _JS_PATH.read_text(encoding="utf-8")

    def test_no_unhardened_generated_blank_target_in_source(self):
        self.assertNotIn(
            _UNHARDENED_ANCHOR,
            self.js,
            "homicides.js: a generated target=_blank link is missing rel=noopener",
        )
        self.assertIn(
            _HARDENED_ANCHOR, self.js, "homicides.js: expected a rel=noopener generated link"
        )

    def test_valid_press_release_link_is_rendered_and_hardened(self):
        popups = _render_popups("https://www.austintexas.gov/news/test-press-release?id=1&x=2")
        self.assertEqual(len(popups), 1, "expected one popup for one incident")
        anchors = _anchors(popups[0])
        self.assertEqual(len(anchors), 1, "expected exactly one anchor in the popup")
        href = anchors[0]["href"]
        self.assertEqual(href, "https://www.austintexas.gov/news/test-press-release?id=1&x=2")
        self.assertEqual(anchors[0].get("target"), "_blank")
        self.assertIn(
            "noopener", anchors[0].get("rel", "").lower(), "generated link is missing rel=noopener"
        )

    def test_ampersands_in_the_url_are_html_escaped(self):
        """A raw & in an href must be emitted as &amp;, and decode back intact."""
        popups = _render_popups("https://www.austintexas.gov/news?a=1&b=2")
        self.assertIn("&amp;", popups[0], "ampersand was not escaped in the generated href")
        self.assertEqual(_anchors(popups[0])[0]["href"], "https://www.austintexas.gov/news?a=1&b=2")

    def test_quote_in_the_url_cannot_break_out_of_the_href(self):
        hostile = 'https://evil.example/x" onmouseover="alert(1)'
        popups = _render_popups(hostile)
        anchors = _anchors(popups[0])
        self.assertEqual(len(anchors), 1, "hostile URL changed the anchor count")
        # The quote may survive as percent-encoded text *inside* the href value,
        # so assert on the parsed attributes rather than on substrings: a broken
        # out URL would show up as extra attributes, not as a literal.
        self.assertEqual(
            set(anchors[0]),
            {"href", "target", "rel"},
            "hostile URL injected extra attributes into the anchor",
        )
        self.assertNotIn(
            '"', anchors[0]["href"], "raw double quote survived into the href attribute"
        )
        self.assertEqual(anchors[0].get("rel"), "noopener")
        # The href is one self-contained quoted value: exactly two quote
        # characters delimit it, so nothing can be smuggled past it.
        self.assertEqual(popups[0].count('href="https://evil.example/'), 1)
        self.assertIn("%22", anchors[0]["href"], "the quote was not neutralised at all")

    def test_escape_html_helper_neutralises_markup_on_its_own(self):
        """The escaping layer must stand alone, independent of URL parsing."""
        payload = "\"><img src=x onerror=alert(1)>&'"
        escaped = _run_homicide_js([], probe=payload)["probes"]["escaped"]
        self.assertIsNotNone(escaped, "escapeHtml is not reachable in homicides.js")
        for char in ('"', "'", "<", ">"):
            self.assertNotIn(char, escaped, f"escapeHtml left a raw {char!r} in its output")
        self.assertIn("&quot;", escaped)
        self.assertIn("&lt;", escaped)
        self.assertIn("&gt;", escaped)
        self.assertIn("&#39;", escaped)
        self.assertIn("&amp;", escaped)
        # Parsing the escaped output back must reproduce the payload exactly:
        # the entities are markup-safe yet lossless, so no data is corrupted.
        self.assertEqual(_anchors(f'<a href="{escaped}">x</a>')[0]["href"], payload)

    def test_safe_http_url_helper_rejects_non_http_schemes_on_its_own(self):
        for hostile in (
            "javascript:alert(1)",
            "data:text/html,x",
            "vbscript:x",
            "file:///etc/passwd",
            "",
        ):
            with self.subTest(url=hostile):
                self.assertEqual(
                    _run_homicide_js([], probe=hostile)["probes"]["safe"],
                    "",
                    "safeHttpUrl must return '' for a non-http(s) value",
                )

    def test_non_http_schemes_produce_no_link_at_all(self):
        for hostile in (
            "javascript:alert(1)",
            "  javascript:alert(1)  ",
            "JaVaScRiPt:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "//evil.example/protocol-relative",
            "not-a-url",
        ):
            with self.subTest(url=hostile):
                popups = _render_popups(hostile)
                self.assertTrue(popups, f"expected a popup for {hostile!r}")
                self.assertEqual(
                    _anchors(popups[0]),
                    [],
                    f"non-http(s) URL {hostile!r} must not produce an anchor",
                )
                self.assertNotIn("href=", popups[0], f"non-http(s) URL {hostile!r} emitted an href")

    def test_missing_or_non_string_url_produces_no_link(self):
        for value in (None, "", "   "):
            with self.subTest(url=value):
                popups = _render_popups(value)
                self.assertEqual(
                    _anchors(popups[0]), [], "an absent URL must not produce an anchor"
                )
        popups = _run_homicide_js(
            [{"n": 1, "date": "2026-01-15", "lat": 30.2672, "lon": -97.7431, "url": 12345}]
        )["popups"]
        self.assertEqual(_anchors(popups[0]), [], "a non-string URL must not produce an anchor")

    def test_every_generated_blank_target_is_hardened_across_a_batch(self):
        """No incident in a mixed batch may emit an unhardened blank target."""
        records = [
            {"n": i, "date": "2026-01-15", "lat": 30.2 + i / 100, "lon": -97.7, "url": url}
            for i, url in enumerate(
                [
                    "https://www.austintexas.gov/news/ok-1",
                    "javascript:alert(1)",
                    'https://evil.example/" onfocus="alert(2)',
                    "https://www.austintexas.gov/news/ok-2?a=1&b=2",
                ]
            )
        ]
        popups = _run_homicide_js(records)["popups"]
        self.assertEqual(len(popups), 4)
        for popup in popups:
            for anchor in _anchors(popup):
                if anchor.get("target") == "_blank":
                    self.assertIn(
                        "noopener",
                        anchor.get("rel", "").lower(),
                        f"generated target=_blank link {anchor.get('href')!r} is missing rel=noopener",
                    )


if __name__ == "__main__":
    unittest.main()
