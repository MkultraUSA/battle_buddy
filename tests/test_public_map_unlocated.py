"""Tests for the public live map's placement honesty contract.

Issue #148: the live map counted every non-test active incident under "Active
now" but only drew a pin for incidents that passed an inline filter. When an
active incident had no verified location it was silently counted and never
shown, so the map looked complete when it was not — and the count could not be
reconciled against the pins.

Placement is three questions, and the first fix collapsed them into one:

* **located** — a real, non-approximate location or coordinate exists;
* **mappable** — located, *and* a listed incident type, *and* inside the Austin
  envelope. This is the only thing that gets a pin;
* **unlocated** — not located. Only these may be described as "verified
  location unavailable" and only these are counted by
  ``battlebuddy_active_incidents_unlocated``;
* **out-of-scope** — located but unplottable (unlisted type, or outside the
  envelope). Reported as its own generic "not shown on map" category, because
  calling it unlocated would claim we could not find a place we do have.

The three buckets partition the active set, so the public total reconciles
visibly: ``total = mappable + unlocated + out_of_scope``.

The two sides also had to stop measuring different populations. The page was
served ``active_incidents()`` (no test/press-release exclusion) while the gauge
counted its own narrower set and ignored the staleness window, so the number on
the map and the number in /metrics were two different questions. Both now read
``modules.database.ACTIVE_INCIDENT_POPULATION_SQL``.

These tests pin all of that from both sides:

* the front end, by executing the *shipped* inline script out of
  ``modules.public.PUBLIC_MAP_HTML`` under Node against stubbed Leaflet/DOM/
  fetch globals, so the assertions cover the JavaScript a browser actually runs
  rather than a restatement of it;
* the back end, by driving ``audio_receiver.prometheus_metrics()`` over a
  sandboxed SQLite file and reading the exported gauges;
* the wire, by driving ``modules.database.public_active_incidents()`` — the very
  query behind ``/api/incidents/active`` — and feeding that payload to the front
  end, so page and gauge are compared on one real population.

No database outside a temp dir, no network, no running service.
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
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_NODE = shutil.which("node") or shutil.which("nodejs")

# Fields that must never reach the public unlocated notice. The notice exists to
# be honest about a gap in the data, not to re-publish the data we are unsure of.
_FORBIDDEN_IN_NOTICE = (
    "transcript",
    "description",
    "location",
    "agencies",
    "tgid",
)

_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.DOTALL)

# Executes the live map's own inline script with stubbed globals and reports
# what it did: the pins it drew, the three counts it published, and the HTML of
# the unlocated notice. Deliberately minimal — every stub here only has to keep
# the script from reaching a real Leaflet, DOM, or network.
_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];
const dataPath = process.argv[3];
const source = fs.readFileSync(jsPath, 'utf8');
const fixtures = JSON.parse(fs.readFileSync(dataPath, 'utf8'));

// Pins actually handed to L.marker, in draw order.
const markers = [];
// Every textContent assignment the script made, keyed by element id.
const counts = {};

const chainable = function () {
  const o = {};
  o.addTo = function () { return o; };
  o.bindPopup = function () { return o; };
  o.removeLayer = function () { return o; };
  o.setView = function () { return o; };
  return o;
};

// Just enough leaflet: latLngBounds.contains() is the real bounds test the
// predicate relies on, so it is implemented rather than stubbed to always-true.
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
  }
};

function makeElement(id) {
  return {
    id: id,
    textContent: '',
    innerHTML: '',
    title: '',
    hidden: false,
    className: '',
    children: [],
    attrs: {},
    style: {},
    disabled: false,
    classList: { add: function () {}, remove: function () {}, toggle: function () {} },
    appendChild: function (child) { this.children.push(child); return child; },
    removeChild: function (child) {
      const i = this.children.indexOf(child);
      if (i >= 0) this.children.splice(i, 1);
      return child;
    },
    replaceChildren: function () { this.children = []; },
    setAttribute: function (k, v) { this.attrs[k] = String(v); },
    getAttribute: function (k) { return this.attrs[k]; },
    addEventListener: function () {},
    removeEventListener: function () {}
  };
}

function makeTextNode(text) {
  return {
    nodeType: 3,
    tag: '#text',
    textContent: String(text),
    children: [],
    className: '',
    attrs: {}
  };
}

const elements = {};
const document = {
  getElementById: function (id) {
    if (!(id in elements)) elements[id] = makeElement(id);
    return elements[id];
  },
  createElement: function (tag) {
    const el = makeElement('created-' + tag);
    el.tag = tag;
    return el;
  },
  createTextNode: function (text) { return makeTextNode(text); }
};

const counted = ['s-active', 's-active-mapped', 's-active-unlocated', 's-active-out-of-scope'];

// Serialize an element subtree the way innerHTML would: text nodes are escaped,
// so a string the script assigned through textContent comes back as inert
// markup. Without this the privacy assertions below would be reading a stub
// field instead of the bytes a browser would render.
function escapeText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function serialize(el) {
  if (!el) return '';
  const tag = el.tag || 'span';
  const attrs = el.className ? ' class="' + escapeText(el.className) + '"' : '';
  const inner = (el.children || []).map(serialize).join('');
  return '<' + tag + attrs + '>' + escapeText(el.textContent || '') + inner + '</' + tag + '>';
}

const sandbox = {
  L: L,
  document: document,
  console: console,
  setInterval: function () { return 0; },
  setTimeout: setTimeout,
  localStorage: { getItem: function () { return null; }, setItem: function () {} },
  speechSynthesis: { getVoices: function () { return []; }, cancel: function () {},
                     speak: function () {} },
  Date: Date,
  Math: Math,
  JSON: JSON,
  fetch: function (url) {
    let body = {};
    if (url === '/api/incidents/active') body = fixtures.active;
    else if (url === '/api/incidents') body = fixtures.all || fixtures.active;
    else if (url === '/api/calls') body = [];
    else if (url === '/api/stats') body = { calls_24h: 0, incidents_24h: 0, agencies_24h: 0 };
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
  }
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.addEventListener = function () {};

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'public-map-inline.js' });

// The script calls loadIncidents() itself at parse time; let its awaits settle,
// then drain once more in case the stubbed json() queued another microtask.
  setImmediate(function () {
    setImmediate(function () {
    for (const id of counted) counts[id] = String(elements[id].textContent);
    const notice = elements['unlocated-notice'];
    const head = elements['unl-head'];
    const list = elements['unlocated-list'];
    const oos = elements['out-of-scope-notice'];
    const oosHead = elements['oos-head'];
    const canProbe = typeof sandbox.isLocatedIncident === 'function'
                  && typeof sandbox.isMappableIncident === 'function';
    process.stdout.write(JSON.stringify({
      markers: markers,
      counts: counts,
      noticeHidden: notice ? notice.hidden : null,
      noticeHead: head ? head.textContent : null,
      noticeHtml: serialize(list),
      noticeNoteHtml: serialize(notice),
      oosHidden: oos ? oos.hidden : null,
      oosHead: oosHead ? oosHead.textContent : null,
      oosHtml: serialize(oos),
      // Reach the predicates directly so they can be probed in isolation.
      located: (canProbe && fixtures.probe)
        ? fixtures.probe.map(function (i) { return sandbox.isLocatedIncident(i); })
        : null,
      predicate: (canProbe && fixtures.probe)
        ? fixtures.probe.map(function (i) { return sandbox.isMappableIncident(i); })
        : null
    }));
  });
});
"""


def _live_map_script() -> str:
    """Return the real live-map script browsers run.

    The script lives in static/js/public_map.js (CSP forbids inline script);
    fall back to the inline <script> block so the test fails loudly on the
    vulnerable layout instead of silently passing on nothing.
    """
    import modules.public as public

    candidate = _ROOT / "static" / "js" / "public_map.js"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8")
    scripts = _SCRIPT_RE.findall(public.PUBLIC_MAP_HTML)
    assert scripts, "no live-map script found (neither static/js asset nor inline block)"
    return max(scripts, key=len)


def _live_map_script_source() -> str:
    """Where the shipped live-map script came from (for contract tests)."""
    candidate = _ROOT / "static" / "js" / "public_map.js"
    if candidate.exists():
        return str(candidate)
    return "PUBLIC_MAP_HTML inline <script>"


class _NoticeParser(HTMLParser):
    """Collect the text of each list row in the unlocated notice."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[str] = []
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self._depth += 1
            if self._depth == 1:
                self._buf = []

    def handle_endtag(self, tag):
        if tag == "li" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                self.rows.append("".join(self._buf).strip())

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def _rows(notice_html: str) -> list[str]:
    parser = _NoticeParser()
    parser.feed(notice_html)
    parser.close()
    return parser.rows


class LiveMapHarnessMixin:
    """Runs the real inline live-map script under Node."""

    def run_map(
        self,
        active: list[dict],
        all_incidents: list[dict] | None = None,
        probe: list[dict] | None = None,
    ) -> dict:
        if not _NODE:
            raise unittest.SkipTest("node is not installed; skipping live map execution")

        fixtures = {
            "active": active,
            "all": active if all_incidents is None else all_incidents,
        }
        if probe is not None:
            fixtures["probe"] = probe
        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / "live-map.js"
            data_path = Path(tmp) / "fixtures.json"
            harness_path = Path(tmp) / "harness.js"
            js_path.write_text(_live_map_script(), encoding="utf-8")
            data_path.write_text(json.dumps(fixtures), encoding="utf-8")
            harness_path.write_text(_HARNESS_JS, encoding="utf-8")
            proc = subprocess.run(
                [_NODE, str(harness_path), str(js_path), str(data_path)],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=_ROOT,
            )
        if proc.returncode != 0:
            raise AssertionError(
                f"live map harness failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        payload = json.loads(proc.stdout)
        # Parse the serialized notice back into row text so the assertions read
        # the markup a browser would build, not the harness's own field names.
        payload["noticeRows"] = _rows(payload["noticeHtml"] or "")
        return payload


def _incident(**overrides) -> dict:
    """A mappable incident unless a test says otherwise."""
    base = {
        "id": 1,
        "itype": "SHOOTING",
        "ts_start": 1_700_000_000.0,
        "ts_updated": 1_700_000_000.0,
        "status": "active",
        "is_test": 0,
        "location": "700 W 6th St",
        "lat": 30.2672,
        "lon": -97.7431,
        "agencies": '["APD"]',
        "description": "Shots fired, multiple units responding",
        "flagged": 0,
    }
    base.update(overrides)
    return base


def public_unlocated_notice() -> str:
    """The one phrase allowed to describe an incident with no real location."""
    import modules.public as public

    return public.UNLOCATED_LOCATION_NOTICE


def public_notice() -> str:
    """The generic phrase for a located incident that is not plotted."""
    import modules.public as public

    return public.OUT_OF_SCOPE_MAP_NOTICE


# Other suites in this repository install stub `modules.database` and
# `modules.talkgroups` entries into sys.modules at import time and never take
# them back out (tests/test_sitrep.py, tests/test_dm_alerts.py,
# tests/test_apd_cad_poller.py), so importing modules.database in-process can
# hand back a two-attribute fake whose own dependencies are faked too. The
# contract under test is the real file, so read the file; where a real import is
# needed, do it in a child process, which is also what proves it works in
# production.
def _database_source() -> str:
    return (_ROOT / "modules" / "database.py").read_text(encoding="utf-8")


class UnlocatedActiveCountTests(LiveMapHarnessMixin, unittest.TestCase):
    """0, 1 and many unlocated active incidents produce honest counts."""

    def test_zero_unlocated_keeps_the_notice_hidden(self):
        payload = self.run_map([_incident(), _incident(id=2, itype="PURSUIT")])

        self.assertEqual(payload["counts"]["s-active"], "2")
        self.assertEqual(payload["counts"]["s-active-mapped"], "2")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "0")
        self.assertTrue(payload["noticeHidden"], "notice must stay hidden at zero")
        self.assertEqual(payload["noticeRows"], [])

    def test_one_unlocated_is_counted_and_explained(self):
        payload = self.run_map(
            [
                _incident(),
                _incident(id=2, itype="EMS DISPATCH", location=None, lat=None, lon=None),
            ]
        )

        self.assertEqual(payload["counts"]["s-active"], "2")
        self.assertEqual(payload["counts"]["s-active-mapped"], "1")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "1")
        self.assertFalse(payload["noticeHidden"], "notice must appear for one unlocated")
        self.assertEqual(len(payload["noticeRows"]), 1)
        self.assertIn("EMS DISPATCH", payload["noticeRows"][0])
        self.assertIn("verified location unavailable", payload["noticeHead"])
        self.assertRegex(payload["noticeRows"][0], r"\d+[hm] ago", "expected a relative age")

    def test_many_unlocated_each_get_their_own_row(self):
        unlocated = [
            _incident(id=10 + n, itype="EMS DISPATCH", location=None, lat=None, lon=None)
            for n in range(4)
        ]
        payload = self.run_map([_incident()] + unlocated)

        self.assertEqual(payload["counts"]["s-active"], "5")
        self.assertEqual(payload["counts"]["s-active-mapped"], "1")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "4")
        self.assertEqual(len(payload["noticeRows"]), 4)
        for row in payload["noticeRows"]:
            self.assertIn("EMS DISPATCH", row)

    def test_all_unlocated_leaves_no_pins_and_says_so(self):
        payload = self.run_map(
            [
                _incident(id=1, location=None, lat=None, lon=None),
                _incident(id=2, location=None, lat=None, lon=None),
            ]
        )

        self.assertEqual(payload["counts"]["s-active"], "2")
        self.assertEqual(payload["counts"]["s-active-mapped"], "0")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "2")
        self.assertEqual(payload["markers"], [])

    def test_test_incidents_are_excluded_from_all_three_counts(self):
        payload = self.run_map(
            [
                _incident(),
                _incident(id=2, is_test=1, location=None, lat=None, lon=None),
            ]
        )

        self.assertEqual(payload["counts"]["s-active"], "1")
        self.assertEqual(payload["counts"]["s-active-mapped"], "1")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "0")
        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "0")


class CountMarkerConsistencyTests(LiveMapHarnessMixin, unittest.TestCase):
    """The count and the pins come from one predicate, so they cannot disagree."""

    CASES = {
        "plain": [_incident(), _incident(id=2, itype="PURSUIT")],
        "no_location": [
            _incident(),
            _incident(id=2, itype="WEAPONS", location=None, lat=None, lon=None),
        ],
        "fallback_coords": [
            _incident(),
            # What modules.database._fill_incident_coords() actually emits: real
            # numbers, agency-HQ centroid, flagged approximate.
            _incident(
                id=2, itype="WEAPONS", location=None, lat=30.2672, lon=-97.7431, _coords_approx=True
            ),
        ],
        "fallback_coords_with_location": [
            _incident(),
            # The dangerous one: a location string *and* an agency-HQ centroid.
            _incident(
                id=2,
                itype="WEAPONS",
                location="700 W 6th St",
                lat=30.2672,
                lon=-97.7431,
                _coords_approx=True,
            ),
        ],
        "zero_coords": [
            _incident(),
            _incident(id=2, itype="WEAPONS", location="Somewhere", lat=0, lon=0),
        ],
        "blank_location": [
            _incident(),
            # Whitespace only: the SQL side treats TRIM(location) = '' as no
            # location, so the page has to agree or the two populations split.
            _incident(id=2, itype="WEAPONS", location="   "),
        ],
        "wrong_itype": [
            _incident(),
            _incident(id=2, itype="CURFEW", location="700 W 6th St"),
        ],
        "outside_bounds": [
            _incident(),
            _incident(id=2, itype="WEAPONS", location="Dallas", lat=32.7767, lon=-96.7970),
        ],
        # Located, in bounds, but the type is not published, *and* located and
        # published but out of bounds: both out-of-scope, neither unlocated.
        "unlisted_type_and_out_of_bounds": [
            _incident(),
            _incident(id=2, itype="CURFEW", location="700 W 6th St"),
            _incident(id=3, itype="WEAPONS", location="Dallas", lat=32.7767, lon=-96.7970),
            _incident(
                id=4, itype="CURFEW", location="San Antonio", lat=29.4241, lon=-98.4936
            ),
        ],
        "mixed": [
            _incident(),
            _incident(id=2, itype="EMS DISPATCH", location=None, lat=None, lon=None),
            _incident(
                id=3, itype="WEAPONS", location=None, lat=30.2672, lon=-97.7431, _coords_approx=True
            ),
            _incident(id=4, itype="PURSUIT", location="300 W 6th St", lat=30.2701, lon=-97.7500),
        ],
        "all_three_categories_at_once": [
            _incident(id=1, itype="SHOOTING"),
            _incident(id=2, itype="EMS DISPATCH", location=None, lat=None, lon=None),
            _incident(id=3, itype="CURFEW", location="700 W 6th St"),
            _incident(id=4, itype="PURSUIT", location="Dallas", lat=32.7767, lon=-96.7970),
            _incident(id=5, itype="WEAPONS", location="Rural", lat=0, lon=0),
        ],
    }

    def test_mapped_count_equals_pins_drawn(self):
        for name, incidents in self.CASES.items():
            with self.subTest(case=name):
                payload = self.run_map(incidents)
                self.assertEqual(
                    int(payload["counts"]["s-active-mapped"]),
                    len(payload["markers"]),
                    "the 'Mapped on map' count disagrees with the pins drawn",
                )

    def test_counts_partition_the_active_set(self):
        """total = mapped + unlocated + not-shown, on every fixture."""
        for name, incidents in self.CASES.items():
            with self.subTest(case=name):
                payload = self.run_map(incidents)
                total = int(payload["counts"]["s-active"])
                mapped = int(payload["counts"]["s-active-mapped"])
                unlocated = int(payload["counts"]["s-active-unlocated"])
                out_of_scope = int(payload["counts"]["s-active-out-of-scope"])
                self.assertEqual(
                    mapped + unlocated + out_of_scope,
                    total,
                    "mapped + unlocated + not-shown must equal the active total",
                )
                self.assertEqual(total, len(incidents))

    def test_unlocated_rows_equal_the_unlocated_count(self):
        for name, incidents in self.CASES.items():
            with self.subTest(case=name):
                payload = self.run_map(incidents)
                self.assertEqual(
                    int(payload["counts"]["s-active-unlocated"]),
                    len(payload["noticeRows"]),
                    "the notice list and the unlocated count disagree",
                )

    def test_mapped_pins_sit_on_the_incidents_real_coordinates(self):
        payload = self.run_map(self.CASES["mixed"])
        pins = {(round(m["lat"], 4), round(m["lon"], 4)) for m in payload["markers"]}
        self.assertEqual(pins, {(30.2672, -97.7431), (30.2701, -97.75)})


class FallbackCoordinateTests(LiveMapHarnessMixin, unittest.TestCase):
    """Fallback coordinates must never become a pin."""

    def test_fallback_coordinates_are_never_plotted(self):
        payload = self.run_map(
            [
                _incident(
                    id=1,
                    itype="WEAPONS",
                    location=None,
                    lat=30.2672,
                    lon=-97.7431,
                    _coords_approx=True,
                ),
            ]
        )

        self.assertEqual(payload["markers"], [], "a fallback coordinate became a pin")
        self.assertEqual(payload["counts"]["s-active-mapped"], "0")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "1")

    def test_fallback_coordinates_are_reported_as_unlocated(self):
        payload = self.run_map(
            [
                _incident(
                    id=1,
                    itype="WEAPONS",
                    location=None,
                    lat=30.2672,
                    lon=-97.7431,
                    _coords_approx=True,
                ),
            ]
        )

        self.assertEqual(len(payload["noticeRows"]), 1)
        self.assertIn("WEAPONS", payload["noticeRows"][0])
        # Not out-of-scope: a fallback centroid is not a place we chose to hide,
        # it is a place we do not have.
        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "0")

    def test_a_location_string_with_fallback_coordinates_is_still_not_plotted(self):
        """The realistic fake-pin case, end to end through the marker path.

        modules.database._fill_incident_coords() stamps agency-HQ coordinates on
        any row whose lat or lon is NULL — including rows that do carry a
        location string. A predicate that only asks "is there a location?" would
        happily pin that incident at APD headquarters. This is the one case a
        test built on location-less fixtures cannot catch, so it gets its own.
        """
        payload = self.run_map(
            [
                _incident(
                    id=1,
                    itype="SHOOTING",
                    location="700 W 6th St",
                    lat=30.2672,
                    lon=-97.7431,
                    _coords_approx=True,
                ),
            ]
        )

        self.assertEqual(
            payload["markers"], [], "fallback coordinates became a pin despite a location string"
        )
        self.assertEqual(payload["counts"]["s-active-mapped"], "0")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "1")
        self.assertEqual(payload["counts"]["s-active"], "1")

    def test_fallback_agency_hq_centroids_are_never_plotted(self):
        """The real CAT_COORDS fallback table — several agencies, all inside bounds."""
        for cat, lat, lon in (
            ("APD", 30.2672, -97.7431),
            ("DPS", 30.2747, -97.7404),
            ("Pflugerville", 30.4394, -97.6200),
            ("Unknown", 30.2672, -97.7431),
        ):
            with self.subTest(agency=cat):
                payload = self.run_map(
                    [
                        _incident(
                            id=1,
                            itype="WEAPONS",
                            location=None,
                            lat=lat,
                            lon=lon,
                            agencies=json.dumps([cat]),
                            _coords_approx=True,
                        ),
                    ]
                )
                self.assertEqual(payload["markers"], [], f"{cat} fallback coordinate became a pin")
                self.assertEqual(payload["counts"]["s-active-unlocated"], "1")
                self.assertEqual(payload["counts"]["s-active-out-of-scope"], "0")

    def test_predicate_rejects_fallback_coordinates_directly(self):
        probes = [
            _incident(location=None, lat=30.2672, lon=-97.7431, _coords_approx=True),
            _incident(location="700 W 6th St", lat=30.2672, lon=-97.7431, _coords_approx=True),
            _incident(location="700 W 6th St"),
        ]
        payload = self.run_map([], probe=probes)

        self.assertIsNotNone(payload["predicate"], "isMappableIncident is not reachable")
        self.assertEqual(
            payload["predicate"],
            [False, False, True],
            "the predicate must reject fallback coordinates even with a location",
        )
        self.assertEqual(
            payload["located"],
            [False, False, True],
            "a fallback centroid must not make a row located",
        )

    def test_predicate_boundary_conditions(self):
        probes = [
            _incident(location=None, lat=None, lon=None),  # nothing
            _incident(location="", lat=30.2672, lon=-97.7431),  # empty location
            _incident(itype="CURFEW"),  # unlisted type
            _incident(lat=29.85, lon=-98.25),  # SW corner, inclusive
            _incident(lat=30.70, lon=-97.25),  # NE corner, inclusive
            _incident(lat=29.84, lon=-98.25),  # just south
            _incident(lat=30.70, lon=-97.24),  # just east
            _incident(lat=0, lon=0),  # zero sentinels
        ]
        payload = self.run_map([], probe=probes)

        self.assertEqual(
            payload["predicate"],
            [False, False, False, True, True, False, False, False],
        )

    def test_located_predicate_splits_unlocated_from_out_of_scope(self):
        """The two predicates must disagree on exactly the out-of-scope rows.

        ``isLocatedIncident`` asks "do we have a real place?", so an unlisted
        type and a Dallas coordinate are both located. ``isMappableIncident``
        additionally asks "may we plot it?". Anything else means the two
        questions have been collapsed again.
        """
        probes = [
            _incident(location=None, lat=None, lon=None),  # unlocated
            _incident(location="   ", lat=30.2672, lon=-97.7431),  # unlocated (blank)
            _incident(lat=0, lon=0),  # unlocated (zero sentinels)
            _incident(itype="CURFEW"),  # located, out-of-scope (unlisted type)
            _incident(location="Dallas", lat=32.7767, lon=-96.7970),  # located, outside
            _incident(),  # located and mappable
        ]
        payload = self.run_map([], probe=probes)

        self.assertEqual(
            payload["located"],
            [False, False, False, True, True, True],
            "located must mean a real place, independent of type and bounds",
        )
        self.assertEqual(
            payload["predicate"],
            [False, False, False, False, False, True],
            "mappable must additionally require a listed type inside the bounds",
        )


class _TagCollector(HTMLParser):
    """Record every tag name and attribute name in a fragment of markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, _value in attrs:
            self.attrs.append(name)

    handle_startendtag = handle_starttag


class UnlocatedNoticePrivacyTests(LiveMapHarnessMixin, unittest.TestCase):
    """The notice is honest about the gap without leaking the sensitive parts."""

    SECRET = "SUPERSECRETPRIVATE"

    def test_notice_leaks_no_transcript_address_id_or_coordinates(self):
        secret_incident = _incident(
            id=4242,
            itype="SHOOTING",
            location=None,
            lat=None,
            lon=None,
            agencies=json.dumps([self.SECRET]),
            tgids=json.dumps([self.SECRET]),
            description=f"{self.SECRET} caller said shots at {self.SECRET}",
        )
        payload = self.run_map([secret_incident])

        self.assertEqual(len(payload["noticeRows"]), 1, "the row must still be shown")
        self.assertNotIn(self.SECRET, payload["noticeHtml"], "the notice leaked a private field")
        self.assertNotIn(
            self.SECRET, payload["noticeNoteHtml"], "the notice region leaked a private field"
        )
        self.assertNotIn(
            self.SECRET, payload["noticeHead"], "the notice headline leaked a private field"
        )
        # The incident id is what would let anyone join this row back to the
        # transcript, so it must not appear in any form.
        self.assertNotIn("4242", payload["noticeHtml"], "the notice leaked the incident id")

    def test_notice_shows_only_type_and_relative_age(self):
        payload = self.run_map(
            [
                _incident(id=7, itype="VEHICLE FIRE", location=None, lat=None, lon=None),
            ]
        )

        self.assertEqual(len(payload["noticeRows"]), 1)
        row = payload["noticeRows"][0]
        self.assertIn("VEHICLE FIRE", row)
        self.assertRegex(row, r"\d+[hm] ago")
        # Nothing beyond the type and the age.
        self.assertEqual(
            re.sub(r"[A-Z /]+", "", re.sub(r"\d+[hm] ago", "", row)).strip(),
            "",
            f"unexpected extra content in the notice row: {row!r}",
        )

    def test_notice_never_prints_coordinates(self):
        payload = self.run_map(
            [
                _incident(
                    id=1,
                    itype="WEAPONS",
                    location=None,
                    lat=30.2672,
                    lon=-97.7431,
                    _coords_approx=True,
                ),
            ]
        )

        for leak in ("30.2672", "-97.7431", "30.26", "97.74"):
            self.assertNotIn(
                leak, payload["noticeHtml"], f"the notice leaked a coordinate fragment {leak!r}"
            )

    def test_notice_carries_the_generic_location_message_not_a_private_one(self):
        import modules.public as public

        payload = self.run_map(
            [
                _incident(id=1, itype="WEAPONS", location="1100 Congress Ave", lat=None, lon=None),
            ]
        )

        self.assertIn(public.UNLOCATED_LOCATION_NOTICE, payload["noticeHead"])
        self.assertNotIn("1100 Congress Ave", payload["noticeHtml"])

    def test_markup_in_an_incident_type_cannot_break_the_notice(self):
        """itype reaches the row as text, so hostile markup stays inert text."""
        payload = self.run_map(
            [
                _incident(
                    id=1, itype="<img src=x onerror=alert(1)>", location=None, lat=None, lon=None
                ),
            ]
        )

        parsed = _TagCollector()
        parsed.feed(payload["noticeHtml"])
        parsed.close()
        self.assertNotIn("img", parsed.tags, "incident type was parsed as an element, not as text")
        self.assertNotIn(
            "onerror", parsed.attrs, "incident type injected an event-handler attribute"
        )
        # The type still reaches the reader, escaped.
        self.assertIn(
            "<img src=x onerror=alert(1)>",
            payload["noticeRows"][0],
            "the type should survive as text content",
        )
        self.assertIn(
            "&lt;img", payload["noticeHtml"], "the type was not escaped in the serialized markup"
        )


class OutOfScopeCategoryTests(LiveMapHarnessMixin, unittest.TestCase):
    """Located-but-unplottable incidents get their own honest category.

    The bug this guards: a pursuit in Dallas and a curfew notice on 6th Street
    were both described as having a "verified location unavailable". Dallas has a
    verified location — it is simply not the one this map covers — so that copy
    was false, and a reader who caught it had reason to distrust the real
    unlocated count too.
    """

    def test_unlisted_type_is_counted_as_not_shown_not_unlocated(self):
        payload = self.run_map(
            [
                _incident(id=1, itype="SHOOTING"),
                _incident(id=2, itype="CURFEW", location="700 W 6th St"),
            ]
        )

        self.assertEqual(payload["counts"]["s-active"], "2")
        self.assertEqual(payload["counts"]["s-active-mapped"], "1")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "0")
        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "1")
        self.assertEqual(len(payload["markers"]), 1)

    def test_out_of_bounds_incident_is_counted_as_not_shown_not_unlocated(self):
        payload = self.run_map(
            [
                _incident(id=1, itype="SHOOTING"),
                _incident(id=2, itype="PURSUIT", location="Dallas", lat=32.7767, lon=-96.7970),
            ]
        )

        self.assertEqual(payload["counts"]["s-active"], "2")
        self.assertEqual(payload["counts"]["s-active-mapped"], "1")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "0")
        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "1")
        self.assertEqual(len(payload["markers"]), 1)

    def test_both_out_of_bounds_and_unlisted_type_land_in_the_same_bucket(self):
        payload = self.run_map(
            [
                _incident(id=1, itype="CURFEW", location="700 W 6th St"),
                _incident(id=2, itype="PURSUIT", location="Dallas", lat=32.7767, lon=-96.7970),
                _incident(
                    id=3, itype="CURFEW", location="San Antonio", lat=29.4241, lon=-98.4936
                ),
            ]
        )

        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "3")
        self.assertEqual(payload["counts"]["s-active-mapped"], "0")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "0")
        self.assertEqual(payload["markers"], [])

    def test_out_of_scope_never_borrows_the_unlocated_wording(self):
        """The whole point: these rows are located, so do not say otherwise."""
        payload = self.run_map([_incident(id=1, itype="CURFEW", location="700 W 6th St")])

        self.assertFalse(payload["oosHidden"], "the not-shown notice must appear")
        self.assertIn(public_notice(), payload["oosHead"])
        self.assertNotIn(
            public_unlocated_notice(),
            payload["oosHead"],
            "a located incident was described as having no verified location",
        )
        self.assertNotIn(public_unlocated_notice(), payload["oosHtml"])

    def test_out_of_scope_notice_publishes_no_per_incident_detail(self):
        """These rows are located, so a type/age row would be defensible, but a
        count is all the page promises — assert it stays a count."""
        secret = "OUTOFSCOPESECRET"
        payload = self.run_map(
            [
                _incident(
                    id=99,
                    itype="CURFEW",
                    location=f"{secret} Rd",
                    lat=32.7767,
                    lon=-96.7970,
                    agencies=json.dumps([secret]),
                    description=f"{secret} details",
                ),
            ]
        )

        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "1")
        self.assertNotIn(secret, payload["oosHtml"], "the notice leaked a private field")
        self.assertNotIn("99", payload["oosHtml"], "the notice leaked the incident id")
        self.assertNotIn("32.7767", payload["oosHtml"], "the notice leaked a coordinate")

    def test_zero_out_of_scope_hides_the_notice(self):
        payload = self.run_map([_incident(id=1), _incident(id=2, itype="PURSUIT")])

        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "0")
        self.assertTrue(payload["oosHidden"], "the notice must stay hidden at zero")
        self.assertEqual(payload["oosHead"], "")

    def test_unlocated_and_out_of_scope_are_counted_separately(self):
        payload = self.run_map(
            [
                _incident(id=1, itype="SHOOTING"),
                _incident(id=2, itype="EMS DISPATCH", location=None, lat=None, lon=None),
                _incident(id=3, itype="CURFEW", location="700 W 6th St"),
                _incident(id=4, itype="PURSUIT", location="Dallas", lat=32.7767, lon=-96.7970),
            ]
        )

        self.assertEqual(payload["counts"]["s-active"], "4")
        self.assertEqual(payload["counts"]["s-active-mapped"], "1")
        self.assertEqual(payload["counts"]["s-active-unlocated"], "1")
        self.assertEqual(payload["counts"]["s-active-out-of-scope"], "2")
        # The unlocated notice lists only the one row with no location.
        self.assertEqual(len(payload["noticeRows"]), 1)
        self.assertIn("EMS DISPATCH", payload["noticeRows"][0])
        self.assertFalse(payload["oosHidden"])

    def test_the_two_notices_never_use_each_others_wording(self):
        payload = self.run_map(
            [
                _incident(id=1, itype="EMS DISPATCH", location=None, lat=None, lon=None),
                _incident(id=2, itype="CURFEW", location="700 W 6th St"),
            ]
        )

        self.assertNotIn(
            public_notice(),
            payload["noticeHtml"] + (payload["noticeHead"] or ""),
            "the unlocated notice claimed its rows were a scoping decision",
        )
        self.assertNotIn(
            public_unlocated_notice(),
            payload["oosHtml"] + (payload["oosHead"] or ""),
            "the out-of-scope notice claimed its rows had no verified location",
        )


class UnlocatedNoticeMarkupTests(unittest.TestCase):
    """Static contract on the shipped page, independent of Node."""

    @classmethod
    def setUpClass(cls):
        import modules.public as public

        cls.html = public.PUBLIC_MAP_HTML

    def test_page_publishes_all_four_counts(self):
        for element_id in (
            "s-active",
            "s-active-mapped",
            "s-active-unlocated",
            "s-active-out-of-scope",
        ):
            self.assertIn(
                f'id="{element_id}"', self.html, f"the live map never publishes {element_id}"
            )

    def test_page_has_the_out_of_scope_notice_region(self):
        self.assertIn('id="out-of-scope-notice"', self.html)
        self.assertIn('id="oos-head"', self.html)

    def test_both_notices_start_hidden(self):
        self.assertRegex(self.html, r'<div id="unlocated-notice" hidden>')
        self.assertRegex(self.html, r'<div id="out-of-scope-notice" hidden>')

    def test_page_shows_the_reconciliation_arithmetic(self):
        """The total has to be checkable by eye, not taken on trust."""
        self.assertIn("stat-reconcile", self.html)
        self.assertIn(public_notice(), self.html)
        self.assertIn("Active now = on the map + location unverified +", self.html)

    def test_page_has_the_unlocated_notice_region(self):
        self.assertIn('id="unlocated-notice"', self.html)
        self.assertIn('id="unlocated-list"', self.html)
        self.assertIn('id="unl-head"', self.html)

    def test_notice_starts_hidden(self):
        self.assertRegex(self.html, r'<div id="unlocated-notice" hidden>')

    def test_markers_and_counts_share_one_predicate(self):
        """The old bug was a hand-written inline filter; forbid it coming back."""
        script = _live_map_script()
        self.assertIn("function isLocatedIncident", script)
        self.assertIn("function isMappableIncident", script)
        # The pins are the mapped list itself, so a count cannot disagree with
        # what is drawn, and every bucket is expressed with the shared
        # predicates.
        self.assertIn("const mappableActive   = realActive.filter(isMappableIncident)", script)
        self.assertIn("const unlocatedActive  = realActive.filter(i => !isLocatedIncident(i))", script)
        self.assertIn(
            "const outOfScopeActive = realActive.filter(i => isLocatedIncident(i) "
            "&& !isMappableIncident(i))",
            script,
        )
        self.assertIn("mappableActive.forEach(inc =>", script)
        self.assertNotRegex(
            script,
            r"filter\(i => i\.location &&",
            "a hand-written mappable filter reappeared next to the shared predicate",
        )

    def test_unlocated_is_not_defined_as_not_mappable(self):
        """The conflation this branch exists to prevent, as a source contract."""
        script = _live_map_script()
        self.assertNotIn(
            "filter(i => !isMappableIncident(i))",
            script,
            "unlocated was defined as 'not mappable', which sweeps located "
            "out-of-scope incidents into the unverified-location bucket",
        )
        unlocated_line = next(
            line
            for line in script.splitlines()
            if line.strip().startswith("const unlocatedActive")
        )
        self.assertIn("!isLocatedIncident(i)", unlocated_line)

    def test_predicate_refuses_fallback_coordinates(self):
        script = _live_map_script()
        self.assertRegex(
            script,
            r"function isLocatedIncident\(i\)\s*\{[^}]*_coords_approx",
            "the located predicate no longer rejects fallback coordinates",
        )
        self.assertRegex(
            script,
            r"function isMappableIncident\(i\)\s*\{\s*if \(!isLocatedIncident\(i\)\)",
            "the mappable predicate no longer requires a located incident",
        )

    def test_injected_type_list_matches_the_python_contract(self):
        import modules.public as public

        # The script is external now (CSP); the list it ships must still equal
        # the Python contract or pins and counts drift apart.
        script = _live_map_script()
        match = re.search(
            r"(?:const|var) MAP_ITYPES = new Set\((\[.*?\])\);", script, re.DOTALL
        )
        self.assertIsNotNone(
            match, f"the MAP_ITYPES set is missing ({_live_map_script_source()})"
        )
        self.assertEqual(json.loads(match.group(1)), list(public.MAP_INCIDENT_TYPES))

    def test_notice_strings_match_the_python_contract(self):
        import modules.public as public

        script = _live_map_script()
        for name, value in (
            ("UNLOCATED_LOCATION_NOTICE", public.UNLOCATED_LOCATION_NOTICE),
            ("OUT_OF_SCOPE_MAP_NOTICE", public.OUT_OF_SCOPE_MAP_NOTICE),
        ):
            self.assertIn(
                f'var {name} = "{value}"',
                script,
                f"{name} drifted from modules.public ({_live_map_script_source()})",
            )

    def test_no_placeholder_survives_into_the_rendered_page(self):
        self.assertNotIn("__MAP_INCIDENT_TYPES__", self.html)
        self.assertNotIn("__UNLOCATED_LOCATION_NOTICE__", self.html)
        self.assertNotIn("__OUT_OF_SCOPE_MAP_NOTICE__", self.html)

    def test_notice_source_mentions_no_private_field(self):
        """Guard the renderer itself: it may not reach for the sensitive fields."""
        script = _live_map_script()
        start = script.index("function renderUnlocatedNotice")
        end = script.index("\n}", start)
        body = script[start:end]
        for field in _FORBIDDEN_IN_NOTICE:
            self.assertNotIn(
                f"inc.{field}",
                body,
                f"renderUnlocatedNotice reads the private field {field!r}",
            )

    def test_out_of_scope_renderer_reads_no_incident_field(self):
        """The out-of-scope notice is a count, so it needs no row at all."""
        script = _live_map_script()
        start = script.index("function renderOutOfScopeNotice")
        end = script.index("\n}", start)
        body = script[start:end]
        for field in _FORBIDDEN_IN_NOTICE + ("lat", "lon", "id", "itype"):
            self.assertNotIn(
                f"inc.{field}",
                body,
                f"renderOutOfScopeNotice reads the field {field!r}",
            )


# ---------------------------------------------------------------------------
# The exported Prometheus gauge
# ---------------------------------------------------------------------------

_METRICS_CHILD = textwrap.dedent(
    """
    import json
    import os
    import sqlite3
    import sys
    import time
    from unittest import mock

    sys.modules["stripe"] = mock.MagicMock()

    import audio_receiver

    db_path = os.environ["DB_PATH"]
    now = float(os.environ["TEST_NOW"])
    rows = json.loads(os.environ["TEST_ROWS"])

    # Build the real schema rather than a one-table stub: the metrics generator
    # walks several tables before it reaches `incidents`, and a missing earlier
    # table aborts the whole collector — which would silently look like a zero.
    conn = sqlite3.connect(db_path)
    with open(os.path.join(os.getcwd(), "schema.sql"), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    for i, row in enumerate(rows):
        conn.execute(
            "INSERT INTO incidents (id, ts_start, ts_updated, itype, description, "
            "agencies, location, lat, lon, status, is_test) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                i + 1,
                row.get("ts_start", now),
                row.get("ts_updated", now),
                row.get("itype"),
                row.get("description"),
                row.get("agencies"),
                row.get("location"),
                row.get("lat"),
                row.get("lon"),
                row.get("status", "active"),
                row.get("is_test", 0),
            ),
        )
    conn.commit()
    conn.close()

    body, status, _headers = audio_receiver.prometheus_metrics()
    text = body.decode()

    def sample(name):
        for line in text.splitlines():
            if line.startswith(name + " ") or line.startswith(name + "{"):
                return float(line.rsplit(" ", 1)[1])
        return None

    def labelled_lines(name):
        return [l for l in text.splitlines() if l.startswith(name + "{")]

    print(json.dumps({
        "status": status,
        "active": sample("battlebuddy_active_incidents"),
        "unlocated": sample("battlebuddy_active_incidents_unlocated"),
        "out_of_scope": sample("battlebuddy_active_incidents_out_of_scope"),
        "unlocated_lines": labelled_lines("battlebuddy_active_incidents_unlocated"),
        "out_of_scope_lines": labelled_lines("battlebuddy_active_incidents_out_of_scope"),
        "help": [l for l in text.splitlines()
                 if l.startswith("# HELP battlebuddy_active_incidents_unlocated")],
        "active_help": [l for l in text.splitlines()
                        if l.startswith("# HELP battlebuddy_active_incidents ")],
        "out_of_scope_help": [l for l in text.splitlines()
                              if l.startswith("# HELP battlebuddy_active_incidents_out_of_scope")],
        "body": text,
    }))
    """
)


# Reads the seeded rows back through the production read path, so the
# agency-HQ fallback coordinates and the _coords_approx stamp are applied by the
# same code that serves /api/incidents/active. public_active_incidents() is that
# reader: the gauge and the page are compared on the rows the page is served.
_READ_ACTIVE_CHILD = textwrap.dedent(
    """
    import json
    import sys
    from unittest import mock

    sys.modules["stripe"] = mock.MagicMock()

    from modules.database import public_active_incidents

    print(json.dumps(public_active_incidents()))
    """
)


def _mappable_row(**overrides) -> dict:
    base = {
        "itype": "SHOOTING",
        "location": "700 W 6th St",
        "lat": 30.2672,
        "lon": -97.7431,
        "status": "active",
        "is_test": 0,
    }
    base.update(overrides)
    return base


class UnlocatedMetricTests(unittest.TestCase):
    """battlebuddy_active_incidents_unlocated: one bounded series, one scan."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _run(self, rows: list[dict]) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "DB_PATH": str(self.base / "calls.db"),
                "BATTLE_BUDDY_RAW_QUEUE_DIR": str(self.base / "raw_audio_queue"),
                "BB_RAW_AUDIO_QUEUE_DIR": str(self.base / "raw_audio_queue"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "SMOKE_TEST_BASE_URL": "",
                # A live timestamp, not a fixed epoch: the active population is
                # now filtered on ACTIVE_INCIDENT_WINDOW_S, so rows seeded in
                # 2023 are stale and would legitimately drop out. A fixture that
                # aged out would read as a passing zero.
                "TEST_NOW": repr(time.time()),
                "TEST_ROWS": json.dumps(rows),
            }
        )
        for key in ("BATTLE_BUDDY_HOME", "BATTLE_BUDDY_DATA_DIR", "HOMICIDE_SEED_PATH"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-c", _METRICS_CHILD],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"metrics child failed ({result.returncode}): {result.stderr[-2000:]}"
            )
        # The generator swallows collector exceptions and still returns 200, so a
        # broken query would read as "no data" rather than as a failure. Make it
        # loud here instead of letting a missing gauge look like a zero.
        if "collector error" in result.stdout or "collector error" in result.stderr:
            noise = [
                line
                for line in (result.stdout + result.stderr).splitlines()
                if "collector error" in line
            ]
            raise AssertionError(f"metrics collector reported an error: {noise}")
        return json.loads(result.stdout.splitlines()[-1])

    def test_zero_unlocated_reports_zero(self):
        payload = self._run([_mappable_row(), _mappable_row(itype="PURSUIT")])

        self.assertEqual(payload["status"], 200)
        self.assertEqual(payload["active"], 2.0)
        self.assertEqual(payload["unlocated"], 0.0)

    def test_one_unlocated_reports_one(self):
        payload = self._run(
            [
                _mappable_row(),
                _mappable_row(itype="EMS DISPATCH", location=None, lat=None, lon=None),
            ]
        )

        self.assertEqual(payload["active"], 2.0)
        self.assertEqual(payload["unlocated"], 1.0)

    def test_many_unlocated_report_many(self):
        payload = self._run(
            [_mappable_row()]
            + [
                _mappable_row(itype="EMS DISPATCH", location=None, lat=None, lon=None)
                for _ in range(5)
            ]
        )

        self.assertEqual(payload["active"], 6.0)
        self.assertEqual(payload["unlocated"], 5.0)

    def test_fallback_coordinates_count_as_unlocated(self):
        """The row _fill_incident_coords() produces has numbers but no place."""
        payload = self._run(
            [
                _mappable_row(itype="WEAPONS", location=None, lat=30.2672, lon=-97.7431),
            ]
        )

        self.assertEqual(payload["active"], 1.0)
        self.assertEqual(
            payload["unlocated"], 1.0, "a fallback coordinate was treated as a verified location"
        )
        self.assertEqual(payload["out_of_scope"], 0.0)

    def test_blank_location_and_zero_coordinates_count_as_unlocated(self):
        payload = self._run(
            [
                _mappable_row(itype="WEAPONS", location="   "),
                _mappable_row(itype="PURSUIT", lat=0, lon=0),
            ]
        )

        self.assertEqual(payload["active"], 2.0)
        self.assertEqual(payload["unlocated"], 2.0)
        self.assertEqual(payload["out_of_scope"], 0.0)

    def test_out_of_bounds_and_unlisted_types_are_not_unlocated(self):
        """These are located. Reporting them as unverified locations was the bug."""
        payload = self._run(
            [
                _mappable_row(itype="CURFEW"),  # unlisted type
                _mappable_row(itype="WEAPONS", lat=32.7767, lon=-96.7970),  # Dallas
            ]
        )

        self.assertEqual(payload["active"], 2.0)
        self.assertEqual(
            payload["unlocated"],
            0.0,
            "located out-of-scope rows were counted as having no verified location",
        )
        self.assertEqual(payload["out_of_scope"], 2.0)

    def test_the_three_gauges_reconcile_into_the_active_total(self):
        payload = self._run(
            [
                _mappable_row(),  # mappable
                _mappable_row(itype="EMS DISPATCH", location=None, lat=None, lon=None),  # unlocated
                _mappable_row(itype="CURFEW"),  # out-of-scope: unlisted type
                _mappable_row(itype="PURSUIT", lat=32.7767, lon=-96.7970),  # out-of-scope: bounds
            ]
        )

        self.assertEqual(payload["active"], 4.0)
        self.assertEqual(payload["unlocated"], 1.0)
        self.assertEqual(payload["out_of_scope"], 2.0)
        self.assertEqual(
            payload["active"],
            payload["unlocated"] + payload["out_of_scope"] + 1.0,
            "active = unlocated + out_of_scope + mappable does not hold",
        )

    def test_press_release_rows_are_outside_the_population(self):
        """The gauge already excluded them; the API now has to agree."""
        payload = self._run(
            [
                _mappable_row(),
                _mappable_row(
                    description="[APD Press Release] Shooting on 5th St. Multiple units."
                ),
            ]
        )

        self.assertEqual(
            payload["active"], 1.0, "a press-release summary was counted as an active incident"
        )
        self.assertEqual(payload["unlocated"], 0.0)
        self.assertEqual(payload["out_of_scope"], 0.0)

    def test_stale_active_rows_are_outside_the_population(self):
        """An incident nobody has touched for half an hour is not active now."""
        stale = time.time() - 31 * 60
        payload = self._run(
            [
                _mappable_row(),
                _mappable_row(itype="PURSUIT", ts_updated=stale),
            ]
        )

        self.assertEqual(
            payload["active"], 1.0, "a stale active row was counted as active right now"
        )

    def test_gauge_carries_no_per_incident_labels(self):
        """One series, always: cardinality must not grow with the incident count."""
        payload = self._run(
            [
                _mappable_row(itype="EMS DISPATCH", location=None, lat=None, lon=None)
                for _ in range(7)
            ]
        )

        self.assertEqual(payload["unlocated"], 7.0)
        self.assertEqual(payload["unlocated_lines"], [], "the gauge emitted labelled series")
        self.assertEqual(payload["out_of_scope_lines"], [], "the gauge emitted labelled series")

    def test_gauge_help_documents_the_contract(self):
        payload = self._run([_mappable_row(location=None, lat=None, lon=None)])

        self.assertEqual(len(payload["help"]), 1, "the gauge is missing its HELP line")
        self.assertIn("no verified location", payload["help"][0])
        self.assertEqual(
            len(payload["out_of_scope_help"]), 1, "the out-of-scope gauge has no HELP line"
        )
        self.assertIn("not shown on the public map", payload["out_of_scope_help"][0])
        self.assertEqual(len(payload["active_help"]), 1, "the active gauge has no HELP line")
        self.assertIn("_unlocated + _out_of_scope", payload["active_help"][0])

    def test_cleared_and_test_rows_are_outside_both_gauges(self):
        payload = self._run(
            [
                _mappable_row(),
                _mappable_row(location=None, lat=None, lon=None, status="cleared"),
                _mappable_row(location=None, lat=None, lon=None, is_test=1),
            ]
        )

        self.assertEqual(
            payload["active"], 1.0, "cleared/test rows leaked into the active population"
        )
        self.assertEqual(payload["unlocated"], 0.0)
        self.assertEqual(payload["out_of_scope"], 0.0)

    def test_gauge_name_is_the_one_the_runbook_expects(self):
        payload = self._run([_mappable_row()])

        self.assertIn("battlebuddy_active_incidents_unlocated", payload["body"])
        self.assertIn("battlebuddy_active_incidents_out_of_scope", payload["body"])
        self.assertEqual(payload["unlocated"], 0.0)


class FrontEndBackEndParityTests(LiveMapHarnessMixin, unittest.TestCase):
    """The exported gauges and the live-map counts, measured on the same rows.

    The two answers are produced by different code — SQL in the metrics
    generator, JavaScript in the page — so "they agree" is a claim that has to be
    tested, not assumed. These rows go through the real
    ``modules.database.public_active_incidents()`` read path, which is what
    applies the agency-HQ fallback coordinates and stamps ``_coords_approx``, so
    the browser sees exactly the payload production serves.
    """

    # Every row is active, non-test, non-press-release and fresh, i.e. inside the
    # shared population, so the two sides are comparable. The rows that sit
    # *outside* it get their own class below.
    ROWS = [
        # genuinely geocoded -> mappable
        {
            "itype": "SHOOTING",
            "location": "700 W 6th St",
            "lat": 30.2672,
            "lon": -97.7431,
            "agencies": '["APD"]',
        },
        # location string but geocoding failed -> fallback coords at run time
        {
            "itype": "WEAPONS",
            "location": "Some Rural Rd",
            "lat": None,
            "lon": None,
            "agencies": '["APD"]',
        },
        # nothing at all
        {
            "itype": "EMS DISPATCH",
            "location": None,
            "lat": None,
            "lon": None,
            "agencies": '["TCEMS"]',
        },
        # unlisted incident type
        {
            "itype": "CURFEW",
            "location": "700 W 6th St",
            "lat": 30.2672,
            "lon": -97.7431,
            "agencies": '["APD"]',
        },
        # outside the Austin envelope
        {
            "itype": "PURSUIT",
            "location": "Dallas",
            "lat": 32.7767,
            "lon": -96.7970,
            "agencies": '["DPS"]',
        },
        # another agency fallback, outside the type list
        {"itype": "HAZMAT", "location": None, "lat": None, "lon": None, "agencies": '["Kerr"]'},
        # whitespace-only location: SQL says unlocated, so the page must too
        {"itype": "FLOODING", "location": "  ", "lat": 30.2, "lon": -97.7, "agencies": '["TCFD"]'},
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _seeded_payload_and_gauge(self):
        now = time.time()
        env = os.environ.copy()
        env.update(
            {
                "DB_PATH": str(self.base / "calls.db"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "SMOKE_TEST_BASE_URL": "",
                "TEST_NOW": str(now),
                "TEST_ROWS": json.dumps(
                    [
                        dict(row, status="active", is_test=0, ts_start=now, ts_updated=now)
                        for row in self.ROWS
                    ]
                ),
            }
        )
        for key in ("BATTLE_BUDDY_HOME", "BATTLE_BUDDY_DATA_DIR", "HOMICIDE_SEED_PATH"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-c", _METRICS_CHILD],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"metrics child failed ({result.returncode}): {result.stderr[-2000:]}"
            )
        gauge = json.loads(result.stdout.splitlines()[-1])

        # Read the rows back through the real query the browser is served, so the
        # fallback-coordinate stamping happens exactly as it does in production.
        read_env = os.environ.copy()
        read_env.update({"DB_PATH": str(self.base / "calls.db"), "PYTHONDONTWRITEBYTECODE": "1"})
        for key in ("BATTLE_BUDDY_HOME", "BATTLE_BUDDY_DATA_DIR"):
            read_env.pop(key, None)
        payload = subprocess.run(
            [sys.executable, "-c", _READ_ACTIVE_CHILD],
            cwd=_ROOT,
            env=read_env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if payload.returncode != 0:
            raise AssertionError(
                f"public_active_incidents child failed ({payload.returncode}): "
                f"{payload.stderr[-2000:]}"
            )
        return json.loads(payload.stdout.splitlines()[-1]), gauge

    def test_the_fallback_stamp_is_what_separates_the_two_populations(self):
        payload, _gauge = self._seeded_payload_and_gauge()

        # Sanity: the read path really did inject fallback coordinates, so this
        # fixture is exercising the fake-pin case rather than bypassing it.
        approx = [i for i in payload if i.get("_coords_approx")]
        self.assertTrue(approx, "no row was stamped _coords_approx; fixture is not testing it")
        for row in approx:
            self.assertIsNotNone(row["lat"], "an approximate row should still carry coordinates")

    def test_gauge_matches_the_live_map_unlocated_count(self):
        payload, gauge = self._seeded_payload_and_gauge()
        ui = self.run_map(payload)

        self.assertEqual(
            int(ui["counts"]["s-active-unlocated"]),
            int(gauge["unlocated"]),
            "the exported gauge and the live map disagree about unlocated incidents",
        )
        self.assertEqual(
            int(ui["counts"]["s-active"]),
            int(gauge["active"]),
            "the exported gauge and the live map disagree about active incidents",
        )

    def test_gauge_matches_the_live_map_out_of_scope_count(self):
        payload, gauge = self._seeded_payload_and_gauge()
        ui = self.run_map(payload)

        self.assertEqual(
            int(ui["counts"]["s-active-out-of-scope"]),
            int(gauge["out_of_scope"]),
            "the exported gauge and the live map disagree about not-shown incidents",
        )

    def test_gauge_matches_the_live_map_mapped_count(self):
        payload, gauge = self._seeded_payload_and_gauge()
        ui = self.run_map(payload)

        self.assertEqual(
            int(gauge["active"]) - int(gauge["unlocated"]) - int(gauge["out_of_scope"]),
            int(ui["counts"]["s-active-mapped"]),
            "active minus unlocated minus out-of-scope does not reconcile with mappable",
        )

    def test_every_fallback_coordinate_row_lands_in_the_unlocated_bucket(self):
        payload, gauge = self._seeded_payload_and_gauge()
        approx_ids = {i["id"] for i in payload if i.get("_coords_approx")}
        ui = self.run_map(payload, probe=payload)

        located_ids = {i["id"] for i, ok in zip(payload, ui["located"]) if ok}
        self.assertFalse(
            approx_ids & located_ids,
            "a fallback coordinate was treated as a real location on the front end",
        )
        self.assertEqual(
            len(payload) - len(located_ids),
            int(gauge["unlocated"]),
            "the SQL gauge and the JS predicate disagree on the fallback rows",
        )

    def test_both_sides_agree_row_for_row_on_which_bucket_each_row_is_in(self):
        """Stronger than matching totals: the same rows must land in the same bucket."""
        payload, gauge = self._seeded_payload_and_gauge()
        ui = self.run_map(payload, probe=payload)

        located = [ok for ok in ui["located"]]
        mappable = [ok for ok in ui["predicate"]]
        self.assertEqual(
            sum(1 for ok in mappable if ok),
            int(gauge["active"]) - int(gauge["unlocated"]) - int(gauge["out_of_scope"]),
        )
        self.assertEqual(sum(1 for ok in located if not ok), int(gauge["unlocated"]))
        self.assertEqual(
            sum(1 for loc, ok in zip(located, mappable) if loc and not ok),
            int(gauge["out_of_scope"]),
            "the located-but-unplottable rows disagree between SQL and JavaScript",
        )
        self.assertEqual(
            len(payload),
            int(gauge["active"]),
            "the gauge and the API disagree on how many rows are active at all",
        )


class SharedActivePopulationTests(unittest.TestCase):
    """The page and the gauge must read the *same rows*, not merely agree.

    They used not to. The page was served every active row the 30-minute window
    kept, while the gauge counted active rows minus test minus press-release and
    ignored the window entirely. On a database holding a press release or a stale
    incident — the normal case — the number on the map and the number in /metrics
    were answers to two different questions.

    Both now read ``modules.database.ACTIVE_INCIDENT_POPULATION_SQL``, so this
    seeds rows that the *old* page included and the *old* gauge excluded, plus
    rows the *old* gauge included and the *old* page dropped, and requires that
    each side now drops all of them.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _seed_and_read(self, rows: list[dict]):
        """Seed `rows`, then return (API payload, exported gauges)."""
        now = time.time()
        env = os.environ.copy()
        env.update(
            {
                "DB_PATH": str(self.base / "calls.db"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "SMOKE_TEST_BASE_URL": "",
                "TEST_NOW": str(now),
                "TEST_ROWS": json.dumps(rows),
            }
        )
        for key in ("BATTLE_BUDDY_HOME", "BATTLE_BUDDY_DATA_DIR", "HOMICIDE_SEED_PATH"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-c", _METRICS_CHILD],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"metrics child failed ({result.returncode}): {result.stderr[-2000:]}"
            )
        gauge = json.loads(result.stdout.splitlines()[-1])

        read_env = os.environ.copy()
        read_env.update({"DB_PATH": str(self.base / "calls.db"), "PYTHONDONTWRITEBYTECODE": "1"})
        for key in ("BATTLE_BUDDY_HOME", "BATTLE_BUDDY_DATA_DIR"):
            read_env.pop(key, None)
        payload = subprocess.run(
            [sys.executable, "-c", _READ_ACTIVE_CHILD],
            cwd=_ROOT,
            env=read_env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if payload.returncode != 0:
            raise AssertionError(
                f"public_active_incidents child failed ({payload.returncode}): "
                f"{payload.stderr[-2000:]}"
            )
        return json.loads(payload.stdout.splitlines()[-1]), gauge

    def test_press_release_test_cleared_and_stale_rows_are_excluded_from_both(self):
        now = time.time()
        keep = _mappable_row(itype="SHOOTING")
        rows = [
            dict(keep, ts_start=now, ts_updated=now),
            # The old page counted this; the old gauge did not.
            {
                **_mappable_row(itype="WEAPONS"),
                "description": "[APD Press Release] Shots fired downtown.",
                "ts_start": now,
                "ts_updated": now,
            },
            # The old page counted this too (it only filtered is_test in JS).
            {**_mappable_row(itype="PURSUIT"), "is_test": 1, "ts_start": now, "ts_updated": now},
            # The old gauge counted this; the old page dropped it.
            {**_mappable_row(itype="EMS DISPATCH"), "ts_start": now, "ts_updated": now - 3600},
            {**_mappable_row(itype="HAZMAT"), "status": "cleared", "ts_start": now, "ts_updated": now},
        ]

        payload, gauge = self._seed_and_read(rows)

        self.assertEqual(
            len(payload), 1, f"the API served rows outside the population: {payload}"
        )
        self.assertEqual(payload[0]["itype"], "SHOOTING")
        self.assertEqual(
            gauge["active"], 1.0, "the gauge counted rows outside the population"
        )
        self.assertEqual(gauge["unlocated"], 0.0)
        self.assertEqual(gauge["out_of_scope"], 0.0)

    def test_the_api_route_serves_the_shared_reader(self):
        """Guard against /api/incidents/active drifting back to the wider query."""
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        start = source.index("def api_incidents_active()")
        end = source.index("\n@app.route(", start)
        # Strip comments: the explanatory comment is allowed to name the wider
        # reader; the code is not allowed to call it.
        code = "\n".join(
            line for line in source[start:end].splitlines() if not line.strip().startswith("#")
        )

        self.assertIn("public_active_incidents()", code)
        self.assertNotIn(
            " active_incidents()",
            code,
            "the public API went back to the wider operational reader",
        )

    def test_the_population_filter_is_defined_once(self):
        source = _database_source()
        start = source.index("ACTIVE_INCIDENT_POPULATION_SQL = (")
        clause = source[start : source.index(")\n", start)]
        for needle in (
            "status = 'active'",
            "is_test IS NULL OR is_test = 0",
            "%[APD Press Release]%",
            "ts_updated >=",
            "ACTIVE_INCIDENT_TIMEOUT_S_SQL",
        ):
            self.assertIn(needle, clause, f"{needle!r} left the shared population filter")
        # The staleness window is per-row from the engine's own timeout map,
        # not a flat cutoff: the CASE must derive from INCIDENT_TIMEOUT_MINUTES
        # with the default fallback, so the two mechanisms cannot drift apart.
        for needle in (
            "CASE itype",
            "INCIDENT_TIMEOUT_MINUTES",
            "_INCIDENT_TIMEOUT_DEFAULT",
        ):
            self.assertIn(needle, source, f"{needle!r} left the shared population filter")
        # The evaluated CASE must actually carry the engine's timeouts: the
        # 45-minute HOSTAGE/BARRICADE exception and the 10-minute default.
        # Child process: other suites stub modules.database in-process (see
        # the note at _database_source), so an in-process import here can
        # hand back a fake without the new attribute.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import modules.database as d;"
                "print(d.ACTIVE_INCIDENT_TIMEOUT_S_SQL)",
            ],
            cwd=_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"database import child failed ({result.returncode}): {result.stderr[-2000:]}"
            )
        case_sql = result.stdout.splitlines()[-1]
        self.assertIn("HOSTAGE/BARRICADE", case_sql)
        self.assertIn("2700", case_sql)
        self.assertIn("ELSE 600", case_sql)
        self.assertEqual(
            source.count("ACTIVE_INCIDENT_POPULATION_SQL ="),
            1,
            "the shared population filter is defined more than once",
        )

        # Exactly one definition: the metrics collector must reference the shared
        # constant, not spell the filter out again, and must not use the legacy
        # flat window.
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        anchor = source.index("_itype_ph =")
        block = source[anchor : source.index("g_out_of_scope = GaugeMetricFamily", anchor)]
        self.assertIn("ACTIVE_INCIDENT_POPULATION_SQL", block)
        for needle in ("status='active'", "%[APD Press Release]%"):
            self.assertNotIn(
                needle,
                block,
                "the metrics collector re-spelled the shared population filter",
            )
        self.assertNotIn(
            "ACTIVE_INCIDENT_WINDOW_S",
            block,
            "the metrics collector went back to the flat staleness window",
        )

    def test_the_two_readers_are_separate_functions(self):
        """active_incidents() backs the sitreps and is a different question."""
        source = _database_source()
        self.assertRegex(source, r"\ndef active_incidents\(\) -> list:")
        self.assertRegex(source, r"\ndef public_active_incidents\(\) -> list:")

        # And the real module really does expose both: a child process gets a
        # clean sys.modules, so this cannot be satisfied by a stub.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import modules.database as d, json;"
                "print(json.dumps([callable(d.active_incidents),"
                "callable(d.public_active_incidents),"
                "d.ACTIVE_INCIDENT_WINDOW_S]))",
            ],
            cwd=_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"database import child failed ({result.returncode}): {result.stderr[-2000:]}"
            )
        active_ok, public_ok, window = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(active_ok, "the operational reader is gone")
        self.assertTrue(public_ok, "the published reader is gone")
        self.assertEqual(window, 30 * 60, "the staleness window is not the documented 30 minutes")


class UnlocatedMetricQueryCostTests(unittest.TestCase):
    def test_active_gauges_share_one_statement(self):
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        start = source.index("cur.execute(", source.index("_itype_ph ="))
        statement = source[start : source.index("cur.fetchone()", start)]

        # One statement serves all three gauges: a second COUNT would be a new scan.
        self.assertEqual(statement.count("FROM incidents"), 1)
        self.assertEqual(statement.count("cur.execute"), 1)
        self.assertIn("COUNT(*)", statement)
        # Two bucket sums: unlocated, and located-but-not-mappable.
        self.assertEqual(statement.count("COALESCE(SUM(CASE WHEN"), 2)
        self.assertIn("SUM(CASE WHEN", statement)

    def test_the_three_gauges_are_yielded_from_that_one_scan(self):
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        start = source.index("_itype_ph =")
        end = source.index("cur.execute(", source.index("cur.fetchone()", start))
        block = source[start:end]
        for name in (
            "battlebuddy_active_incidents",
            "battlebuddy_active_incidents_unlocated",
            "battlebuddy_active_incidents_out_of_scope",
        ):
            self.assertIn(f'"{name}"', block, f"{name} is not exported from the active scan")
        self.assertIn(
            "(active_count, unlocated_count, out_of_scope_count) = cur.fetchone()",
            block,
            "the three gauges are not unpacked from the single row",
        )

    def test_sql_predicate_is_built_from_the_shared_python_contract(self):
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        start = source.rindex("_itype_ph =", 0, source.index("g_unlocated = GaugeMetricFamily"))
        block = source[start : source.index("g_unlocated = GaugeMetricFamily", start)]

        self.assertIn(
            "len(MAP_INCIDENT_TYPES)",
            block,
            "the itype allowlist is no longer built from the shared contract",
        )
        for const in ("MAP_LAT_MIN", "MAP_LAT_MAX", "MAP_LON_MIN", "MAP_LON_MAX"):
            self.assertIn(const, block, f"{const} is missing from the SQL predicate")

    def test_the_two_sql_predicates_are_written_once_and_referenced(self):
        """Retyping the located test into the mappable test is how they drift."""
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        start = source.rindex("_itype_ph =", 0, source.index("g_unlocated = GaugeMetricFamily"))
        block = source[start : source.index("g_unlocated = GaugeMetricFamily", start)]

        self.assertEqual(
            block.count("location IS NOT NULL"),
            1,
            "the located predicate is spelled out more than once",
        )
        self.assertIn("_located_sql", block)
        self.assertIn("{_located_sql}", block)
        self.assertIn("{_mappable_sql}", block)
        # mappable must be defined in terms of located, not independently.
        self.assertIn("_mappable_sql = (", block)
        self.assertRegex(block, r"_mappable_sql = \(\s*f?\"\(\{_located_sql\}")

    def test_sql_predicate_rejects_blank_locations_and_zero_coordinates(self):
        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        block = source[
            source.index("_located_sql = (") : source.index("g_unlocated = GaugeMetricFamily")
        ]

        self.assertIn("TRIM(location) <> ''", block)
        self.assertIn("lat <> 0", block)
        self.assertIn("lon <> 0", block)

    def test_public_page_and_metric_agree_on_the_bounds(self):
        import modules.public as public

        self.assertEqual(
            (public.MAP_LAT_MIN, public.MAP_LAT_MAX, public.MAP_LON_MIN, public.MAP_LON_MAX),
            (29.85, 30.70, -98.25, -97.25),
            "the Python bounds drifted from the ones the live map ships",
        )
        # The script is external now (CSP); assert on what the browser runs.
        script = _live_map_script()
        self.assertIn("L.latLng(29.85, -98.25)", script)
        self.assertIn("L.latLng(30.70, -97.25)", script)

    def test_every_contract_type_is_accepted_by_the_sql_allowlist(self):
        """A type in the JS set but absent from SQL would read as out-of-scope."""
        import modules.public as public

        source = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
        self.assertIn("from modules.public import", source)
        block = source[
            source.index("from modules.public import") : source.index(
                "public_bp,", source.index("from modules.public import")
            )
        ]
        for name in (
            "MAP_INCIDENT_TYPES",
            "MAP_LAT_MIN",
            "MAP_LAT_MAX",
            "MAP_LON_MIN",
            "MAP_LON_MAX",
        ):
            self.assertIn(name, block)
        # The allowlist is the whole tuple, not a subset literal.
        self.assertIn("*MAP_INCIDENT_TYPES,", source)
        start = source.index("_itype_ph =")
        scan = source[start : source.index("cur.fetchone()", start)]
        for itype in public.MAP_INCIDENT_TYPES:
            self.assertNotIn(
                f"'{itype}'", scan, f"{itype} was hardcoded instead of using the contract"
            )
        self.assertTrue(public.MAP_INCIDENT_TYPES)


class PerTypeStalenessTests(unittest.TestCase):
    """The public staleness window is per-row, not a flat 30 minutes.

    The incident engine clears a row when ``now - ts_updated`` exceeds that
    row's own timeout from ``modules.config.INCIDENT_TIMEOUT_MINUTES``
    (default ``_INCIDENT_TIMEOUT_DEFAULT``). The published population must
    never exclude a row the engine still considers active, so the read-time
    window has to be evaluated per row from the row itype. The flat 30-minute
    cutoff dropped HOSTAGE/BARRICADE rows (timeout 45) for 15 minutes while
    the engine still carried them as active.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _seed_and_read(self, rows: list[dict]):
        """Seed `rows`, then return (API payload, exported gauges)."""
        now = time.time()
        env = os.environ.copy()
        env.update(
            {
                "DB_PATH": str(self.base / "calls.db"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "SMOKE_TEST_BASE_URL": "",
                "TEST_NOW": str(now),
                "TEST_ROWS": json.dumps(rows),
            }
        )
        for key in ("BATTLE_BUDDY_HOME", "BATTLE_BUDDY_DATA_DIR", "HOMICIDE_SEED_PATH"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, "-c", _METRICS_CHILD],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"metrics child failed ({result.returncode}): {result.stderr[-2000:]}"
            )
        gauge = json.loads(result.stdout.splitlines()[-1])

        read_env = os.environ.copy()
        read_env.update({"DB_PATH": str(self.base / "calls.db"), "PYTHONDONTWRITEBYTECODE": "1"})
        for key in ("BATTLE_BUDDY_HOME", "BATTLE_BUDDY_DATA_DIR"):
            read_env.pop(key, None)
        payload = subprocess.run(
            [sys.executable, "-c", _READ_ACTIVE_CHILD],
            cwd=_ROOT,
            env=read_env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if payload.returncode != 0:
            raise AssertionError(
                f"public_active_incidents child failed ({payload.returncode}): "
                f"{payload.stderr[-2000:]}"
            )
        return json.loads(payload.stdout.splitlines()[-1]), gauge

    def test_hostage_barricade_inside_own_timeout_stays_published(self):
        """40 minutes is past the old flat 30 but inside the 45-minute timeout."""
        now = time.time()
        fresh = dict(_mappable_row(itype="SHOOTING"), ts_start=now, ts_updated=now)
        # HOSTAGE/BARRICADE is not in MAP_INCIDENT_TYPES, so a located row is
        # out-of-scope while a location-less row is unlocated: one of each.
        unlocated = dict(
            _mappable_row(itype="HOSTAGE/BARRICADE", location=None, lat=None, lon=None),
            ts_start=now - 40 * 60,
            ts_updated=now - 40 * 60,
        )
        out_of_scope = dict(
            _mappable_row(itype="HOSTAGE/BARRICADE"),
            ts_start=now - 40 * 60,
            ts_updated=now - 40 * 60,
        )
        payload, gauge = self._seed_and_read([fresh, unlocated, out_of_scope])

        itypes = sorted(i["itype"] for i in payload)
        self.assertEqual(len(payload), 3, f"40-minute HOSTAGE rows dropped: {itypes}")
        self.assertEqual(itypes.count("HOSTAGE/BARRICADE"), 2)
        self.assertEqual(gauge["active"], 3.0)
        self.assertEqual(gauge["unlocated"], 1.0)
        self.assertEqual(gauge["out_of_scope"], 1.0)
        # Partition invariant still holds on this population.
        self.assertEqual(
            gauge["active"],
            gauge["unlocated"] + gauge["out_of_scope"] + 1.0,
            "active = unlocated + out_of_scope + mappable does not hold",
        )

    def test_rows_stale_beyond_own_timeout_stay_excluded(self):
        """A row older than its own per-type timeout is genuinely stale."""
        now = time.time()
        rows = [
            dict(_mappable_row(itype="SHOOTING"), ts_start=now, ts_updated=now),
            # SHOOTING times out after 20 minutes, so 25 minutes is stale even
            # though the old flat 30-minute window kept it.
            dict(
                _mappable_row(itype="SHOOTING"),
                ts_start=now - 25 * 60,
                ts_updated=now - 25 * 60,
            ),
            # HOSTAGE/BARRICADE times out after 45 minutes, so 50 is stale.
            dict(
                _mappable_row(
                    itype="HOSTAGE/BARRICADE", location=None, lat=None, lon=None
                ),
                ts_start=now - 50 * 60,
                ts_updated=now - 50 * 60,
            ),
        ]
        payload, gauge = self._seed_and_read(rows)

        self.assertEqual(len(payload), 1, f"stale rows leaked: {[i['itype'] for i in payload]}")
        self.assertEqual(payload[0]["itype"], "SHOOTING")
        self.assertEqual(gauge["active"], 1.0)
        self.assertEqual(gauge["unlocated"], 0.0)
        self.assertEqual(gauge["out_of_scope"], 0.0)

    def test_unlisted_itype_uses_default_timeout(self):
        """An itype outside INCIDENT_TIMEOUT_MINUTES falls back to 10 minutes."""
        now = time.time()
        rows = [
            dict(_mappable_row(itype="SHOOTING"), ts_start=now, ts_updated=now),
            # CURFEW is not a key in INCIDENT_TIMEOUT_MINUTES, so the default
            # (10 minutes) applies: 5 minutes is fresh, 15 is stale.
            dict(
                _mappable_row(itype="CURFEW"),
                ts_start=now - 5 * 60,
                ts_updated=now - 5 * 60,
            ),
            dict(
                _mappable_row(itype="CURFEW"),
                ts_start=now - 15 * 60,
                ts_updated=now - 15 * 60,
            ),
        ]
        payload, gauge = self._seed_and_read(rows)

        self.assertEqual(len(payload), 2, f"unexpected population: {[i['itype'] for i in payload]}")
        self.assertEqual(gauge["active"], 2.0)
        # Both CURFEW rows are located but unlisted types, so both fresh ones
        # would be out-of-scope; only the 5-minute one survives.
        self.assertEqual(gauge["out_of_scope"], 1.0)
        self.assertEqual(gauge["unlocated"], 0.0)


if __name__ == "__main__":
    unittest.main()
