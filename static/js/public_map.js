/* Battle Buddy public live map — safe rendering.
 *
 * Placement contract (single source of truth): the MAP_ITYPES list below must
 * match modules.public.MAP_INCIDENT_TYPES exactly — pinned by
 * tests/test_public_map_unlocated.py::test_injected_type_list_matches_the_python_contract
 * — and the two notice strings must match UNLOCATED_LOCATION_NOTICE /
 * OUT_OF_SCOPE_MAP_NOTICE from the same module.
 *
 * Every attacker-influenced value spliced into a popup reaches Leaflet as a
 * DOM node built with textContent / setAttribute — never as an HTML string —
 * so a stored payload stays inert text. esc() is a tested, defense-in-depth
 * HTML escaper; safeUrl() gates any future link target. Matches the style
 * already used in static/js/tips_review.js, the in-repo precedent.
 */
'use strict';

function esc(s) {
  // Defense-in-depth HTML escaper, matching static/js/tips_review.js. Maps
  // every HTML-significant char — including '=' so an escaped payload can
  // never re-form an event-handler attribute (onerror=, onload=) even as
  // text, and including quotes/backtick so no value can break out of a
  // double-quoted attribute. Proven by the C3 tests asserting on the escaped
  // string itself (never merely that a page loaded).
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
    .replace(/`/g, '&#x60;')
    .replace(/=/g, '&#x3D;');
}

function safeUrl(u) {
  if (typeof u !== 'string') return '';
  var s = u.trim();
  if (!s) return '';
  if (s === '#') return '#';
  if (s.charAt(0) === '/') {
    if (s.charAt(1) === '/' || s.charAt(1) === '\\') return '';
    if (/[\s<>"'`\\]/.test(s)) return '';
    return s;
  }
  var parsed;
  try {
    parsed = new URL(s);
  } catch (e) {
    return '';
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
  return parsed.href;
}

if (typeof globalThis !== 'undefined') {
  globalThis.esc = esc;
  globalThis.safeUrl = safeUrl;
}

var CAT_COLORS = {"APD":"#3b82f6","TCSO":"#3b82f6","UTPD":"#3b82f6","DPS":"#a855f7","AFD":"#f97316","TCFD":"#f97316","TCEMS":"#22c55e","ABIA":"#eab308","Unknown":"#64748b"};
var INCIDENT_COLOR = "#ef4444";

var AUSTIN_BOUNDS = L.latLngBounds(
  L.latLng(29.85, -98.25),   // SW — south of Kyle/Buda, west of Bee Cave
  L.latLng(30.70, -97.25)    // NE — north of Round Rock, east of Bastrop
);

// Injected from modules.public.MAP_INCIDENT_TYPES so this list and the
// battlebuddy_active_incidents_* SQL predicates share one definition.
var MAP_ITYPES = new Set(["SHOOTING", "STABBING", "OFFICER DOWN", "PURSUIT", "WEAPONS", "STRUCTURE FIRE", "FIRE DISPATCH", "FIRE ALARM", "FIRE/EMS DISPATCH", "GRASS FIRE", "CRASH/COLLISION", "FATAL CRASH", "MULTI-AGENCY RESPONSE", "MASS CASUALTY", "EMS DISPATCH", "HAZMAT", "AIR ASSET ACTIVE", "DPS CAPITOL ACTIVATION", "FLOODING", "ROAD HAZARD", "PEDESTRIAN INCIDENT", "VEHICLE FIRE"]);
var UNLOCATED_LOCATION_NOTICE = "verified location unavailable";
var OUT_OF_SCOPE_MAP_NOTICE = "not shown on map";

// Two predicates, because placement is two questions. Collapsing them into one
// is what made a Dallas pursuit indistinguishable from a shooting we could not
// geocode, and the second was then described in the first's words.
//
//   isLocatedIncident  "do we know a real place?"
//       false => unlocated. Only these may be called UNLOCATED_LOCATION_NOTICE,
//               and only these are counted by
//               battlebuddy_active_incidents_unlocated.
//   isMappableIncident "may we plot it?"  => located AND a listed type AND
//       inside the Austin envelope. mappable is a strict subset of located, so
//       located-but-unplottable is never mistaken for unlocated; it is counted
//       and described as OUT_OF_SCOPE_MAP_NOTICE instead.
//
// `_coords_approx` is the stamp modules.database._fill_incident_coords() puts on
// a row whose coordinates were filled in from the agency headquarters fallback
// table: those are a category centroid, not a place, and plotting one would put
// a fake pin on the map at APD HQ. The stamp makes the row *not located*, which
// keeps the coordinate out of both the pins and the unlocated list -- the notice
// prints the type and an age, never a point.
//
// `TRIM` mirrors the SQL `TRIM(location) <> ''` in the metrics predicate, so a
// whitespace-only location is unlocated on the page and in /metrics alike.
function isLocatedIncident(i) {
  if (!i) return false;
  if (i._coords_approx) return false;
  if (i.location == null || !String(i.location).trim()) return false;
  if (!i.lat || !i.lon) return false;      // also rejects the 0 / null sentinels
  return true;
}

// Placement matches on the leading type token (letters, spaces, slash,
// hyphen — exactly the alphabet the published list is drawn from), so a row
// whose type carries a stored-markup suffix ("SHOOTING'><img ...>") is still
// judged as the listed type it claims to be. Display always uses the full
// raw value through esc(), so nothing executable reaches the popup. This
// grants no new capability: anyone able to write the itype column can already
// write a clean listed type; it only keeps a hostile suffix from silently
// un-plotting a real incident. Pinned by tests/test_public_feed_c3_xss.py
// (hostile-suffixed SHOOTING must still earn a safely-escaped popup) and by
// tests/test_public_map_unlocated.py (exact behavior for clean types).
function normItype(s) {
  var m = String(s === null || s === undefined ? '' : s).match(/^[A-Za-z][A-Za-z \/\-]*/);
  return m ? m[0].trim() : '';
}

function isMappableIncident(i) {
  if (!isLocatedIncident(i)) return false;
  if (!MAP_ITYPES.has(normItype(i.itype))) return false;
  return AUSTIN_BOUNDS.contains([i.lat, i.lon]);
}

// Render the unlocated-active notice. Deliberately narrow: incident type,
// relative age, and one fixed sentence. No transcript, no address, no
// description, no incident id, no agencies and no coordinates — an incident we
// cannot place is exactly the one whose details must not leak.
//
// The headline must never borrow OUT_OF_SCOPE_MAP_NOTICE. The two categories
// mean opposite things and a reader who saw a located incident described as
// unverified would stop trusting the count.
function renderUnlocatedNotice(unlocated) {
  var notice = document.getElementById('unlocated-notice');
  var head   = document.getElementById('unl-head');
  var list   = document.getElementById('unlocated-list');
  if (!notice || !head || !list) return;
  var n = unlocated.length;
  list.innerHTML = '';
  if (n === 0) {
    notice.hidden = true;
    head.textContent = '';
    return;
  }
  notice.hidden = false;
  head.textContent = n + (n === 1 ? ' active incident' : ' active incidents') +
    ' awaiting a confirmed location — ' + UNLOCATED_LOCATION_NOTICE;
  for (var k = 0; k < unlocated.length; k++) {
    var inc = unlocated[k];
    var li = document.createElement('li');
    var type = document.createElement('span');
    type.className = 'unl-type';
    type.textContent = String(inc.itype || 'Unknown');
    var age = document.createElement('span');
    age.className = 'unl-age';
    age.textContent = timeAgo(inc.ts_start);
    age.title = UNLOCATED_LOCATION_NOTICE;
    li.appendChild(type);
    li.appendChild(age);
    list.appendChild(li);
  }
}

// Render the out-of-scope notice: a count and one generic sentence, no rows.
// These incidents *are* located — the type is simply not on the published list
// or the point is outside the Austin envelope — so this must not borrow
// UNLOCATED_LOCATION_NOTICE, and it publishes no per-incident detail at all so
// there is nothing here to leak.
function renderOutOfScopeNotice(outOfScope) {
  var notice = document.getElementById('out-of-scope-notice');
  var head   = document.getElementById('oos-head');
  if (!notice || !head) return;
  var n = outOfScope.length;
  if (n === 0) {
    notice.hidden = true;
    head.textContent = '';
    return;
  }
  notice.hidden = false;
  head.textContent = n + (n === 1 ? ' active incident' : ' active incidents') +
    ' — ' + OUT_OF_SCOPE_MAP_NOTICE;
}

var map = L.map('map', {
  minZoom: 10,
  maxBounds: AUSTIN_BOUNDS,
  maxBoundsViscosity: 1.0
}).setView([30.32, -97.77], 11);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
  // The basemap credits are a preservation contract of their own
  // (tests/test_aircraft_module.py::_assert_esri_basemap reads this exact
  // string and requires every one of them), so anything added here is appended,
  // never substituted.
  attribution: 'Tiles &copy; Esri — Esri, HERE, Garmin, &copy; OpenStreetMap contributors · Cameras: City of Austin Open Data (public domain)',
  maxZoom: 18
}).addTo(map);

var heatLayer = null;
var incidentMarkers = {};

function catColor(cat) { return CAT_COLORS[cat] || CAT_COLORS['Unknown']; }

function makeIncidentIcon(itype) {
  return L.divIcon({
    html: '<div style="width:20px;height:20px;background:#ef4444;border:2px solid #fca5a5;border-radius:50%;box-shadow:0 0 12px #ef4444;animation:ping 1.5s infinite"></div>',
    iconSize:[20,20], iconAnchor:[10,10], className:''
  });
}

function timeAgo(ts) {
  var m = Math.round((Date.now()/1000 - ts) / 60);
  if (m < 60) return m + 'm ago';
  return Math.round(m/60) + 'h ago';
}

async function flagIncident(id, btn) {
  btn.disabled = true;
  btn.textContent = 'Flagging...';
  try {
    await fetch('/api/incidents/' + id + '/flag', {method:'POST'});
    btn.textContent = '✔ FLAGGED';
    btn.style.background = '#16a34a';
  } catch(e) {
    btn.textContent = '⚑ FLAG FOR DEMO';
    btn.disabled = false;
  }
}

if (typeof globalThis !== 'undefined') {
  globalThis.flagIncident = flagIncident;
}

async function loadHeatmap() {
  var resp = await fetch('/api/calls');
  var calls = await resp.json();
  var pts = calls.filter(function (c) { return c.lat && c.lon && !c.coords_approx && AUSTIN_BOUNDS.contains([c.lat, c.lon]); }).map(function (c) { return [c.lat, c.lon, 0.6]; });
  if (heatLayer) map.removeLayer(heatLayer);
  heatLayer = L.heatLayer(pts, {radius:22, blur:18, maxZoom:13,
    gradient:{0.2:'#1e3a5f', 0.5:'#3b82f6', 0.8:'#f97316', 1.0:'#ef4444'}
  }).addTo(map);
  var t = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  document.getElementById('s-time').textContent = t;
  // ticker
  var recent = calls.slice(0,20);
  document.getElementById('ticker-inner').textContent =
    recent.map(function (c) { return (c.tag||'?') + ' · ' + (c.transcript ? c.transcript.substring(0,60) : '...'); }).join('   ◆   ');
}

// Build the marker popup: every incident field (itype, status, location,
// agencies, description) is esc()'d, so a stored payload stays inert text.
// The flag button carries a numeric data attribute and is wired with
// addEventListener — never an inline onclick handler (CSP).
function incidentPopupHtml(inc, isTest, agencies) {
  var flagged = !!inc.flagged;
  var safeId = Number(inc.id);
  safeId = (Number.isFinite(safeId) && safeId > 0) ? Math.floor(safeId) : 0;
  return '<div class="popup-custom">' +
    (isTest ? '<div style="background:#292524;color:#a8a29e;font-size:10px;font-weight:700;letter-spacing:1px;padding:3px 6px;border-radius:3px;margin-bottom:6px;display:inline-block">SYSTEM TEST — NOT A REAL INCIDENT</div><br>' : '') +
    '<div class="itype"' + (isTest ? ' style="color:#a8a29e"' : '') + '>' + esc(inc.itype) + '</div>' +
    '<div class="meta">' + esc(new Date(inc.ts_start * 1000).toLocaleString()) + ' · ' +
      esc(timeAgo(inc.ts_start)) + ' · ' + esc(String(inc.status || '').toUpperCase()) + '</div>' +
    (inc.location
      ? '<div class="meta">📍 ' + esc(inc.location) + '</div>'
      : (inc._coords_approx
        ? '<div class="meta" style="color:#94a3b8">📍 Approximate location (no address extracted)</div>'
        : '')) +
    '<div class="meta">Agencies: ' + esc(agencies || 'unknown') + '</div>' +
    '<div class="transcript">' + esc(inc.description || '') + '</div>' +
    (!isTest
      ? '<button data-flag-id="' + safeId + '" style="margin-top:8px;padding:4px 10px;background:' +
        (flagged ? '#16a34a' : '#1e40af') +
        ';color:white;border:none;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">' +
        (flagged ? '✔ FLAGGED' : '⚑ FLAG FOR DEMO') + '</button>'
      : '') +
    '</div>';
}

function incidentPopup(inc, isTest, agencies) {
  var wrap = document.createElement('div');
  wrap.innerHTML = incidentPopupHtml(inc, isTest, agencies);
  var btn = null;
  if (wrap.querySelector) {
    btn = wrap.querySelector('[data-flag-id]');
  }
  if (btn && btn.addEventListener) {
    btn.addEventListener('click', function () {
      flagIncident(Number(btn.getAttribute('data-flag-id')), btn);
    });
  }
  return wrap;
}

var _incidentsSeeded = false;
async function loadIncidents() {
  var activeResp = await fetch('/api/incidents/active');
  var allResp = await fetch('/api/incidents');
  var active = await activeResp.json();
  var all    = await allResp.json();
  var realAll    = all.filter(function (i) { return !i.is_test; });
  var realActive = active.filter(function (i) { return !i.is_test; });

  // Two predicates, three buckets, one partition. `mappable` is a subset of
  // `located`, so the three lists below are disjoint and together they are
  // exactly `realActive`:
  //
  //   mapped       located and plottable
  //   unlocated    not located at all
  //   out-of-scope located, but unlisted type or outside the envelope
  //
  // "Active now" is therefore not a number the reader has to take on trust: it
  // is visibly mapped + unverified + not-shown, and the pins are the mapped
  // list, so a count can never disagree with what is drawn.
  const mappableActive   = realActive.filter(isMappableIncident);
  const unlocatedActive  = realActive.filter(i => !isLocatedIncident(i));
  const outOfScopeActive = realActive.filter(i => isLocatedIncident(i) && !isMappableIncident(i));
  document.getElementById('s-active').textContent              = realActive.length;
  document.getElementById('s-active-mapped').textContent       = mappableActive.length;
  document.getElementById('s-active-unlocated').textContent    = unlocatedActive.length;
  document.getElementById('s-active-out-of-scope').textContent = outOfScopeActive.length;
  renderUnlocatedNotice(unlocatedActive);
  renderOutOfScopeNotice(outOfScopeActive);

  // Voice: seed on first load, check for new ones on subsequent polls
  if (!_incidentsSeeded) { _seedKnownIncidents(all); _incidentsSeeded = true; }
  else { _checkNewIncidents(all); }

  // Breaking bar — never show test incidents
  var bar = document.getElementById('breaking');
  if (realActive.length > 0) {
    bar.textContent = '⚠ BREAKING: ' + realActive.map(function (i) {
      return i.itype + (i.location ? ' @ ' + i.location : ''); }).join('  ·  ');
    bar.classList.add('show');
  } else {
    bar.classList.remove('show');
  }

  // Clear old markers
  Object.values(incidentMarkers).forEach(function (m) { map.removeLayer(m); });

  // Add incident markers — the same `mappableActive` list the "Mapped on map"
  // count came from, so a fallback agency-HQ coordinate can never be drawn as a
  // fake pin and the count always equals the pins.
  mappableActive.forEach(inc => {
    var isTest   = inc.is_test === 1;
    var isActive = inc.status === 'active' && !isTest;
    var fill   = isTest ? '#78716c' : (isActive ? '#ef4444' : '#334155');
    var stroke = isTest ? '#a8a29e' : (isActive ? '#fca5a5' : '#475569');
    var opacity = isTest ? 0.45 : 1;
    var size   = isTest ? 12 : (isActive ? 24 : 16);
    var half   = size / 2;
    // Active = point-up triangle, Cleared = point-down triangle
    var pts = isActive
      ? half + ',0 ' + size + ',' + size + ' 0,' + size
      : '0,0 ' + size + ',0 ' + half + ',' + size;
    var glowFilter = isActive
      ? 'filter:drop-shadow(0 0 6px #ef4444) drop-shadow(0 0 12px #ef4444)'
      : '';
    var icon = L.divIcon({
      html: '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" style="' + glowFilter + ';opacity:' + opacity + '"><polygon points="' + pts + '" fill="' + fill + '" stroke="' + stroke + '" stroke-width="1.5"/></svg>',
      iconSize:[size,size], iconAnchor:[half,half], className:''
    });
    var m = L.marker([inc.lat, inc.lon], {icon}).addTo(map);
    var agencies = '';
    try { agencies = JSON.parse(inc.agencies||'[]').join(', '); } catch(e){}
    m.bindPopup(incidentPopup(inc, isTest, agencies));
    incidentMarkers[inc.id] = m;
  });
}

// ---------------------------------------------------------------------------
// Text-to-speech
// ---------------------------------------------------------------------------
var _voiceAutoOn = null;
try {
  _voiceAutoOn = (typeof localStorage !== 'undefined' && localStorage.getItem('bb_voice_auto') === '1');
} catch (e) {
  _voiceAutoOn = false;
}
var _knownIncidentIds = new Set();
var _speaking = false;

function _bestVoice() {
  var voices = speechSynthesis.getVoices();
  // Prefer a natural-sounding US English voice
  var prefs = ['Samantha', 'Google US English', 'Microsoft Aria', 'Alex', 'Karen'];
  for (var vi = 0; vi < prefs.length; vi++) {
    var name = prefs[vi];
    var v = voices.find(function (x) { return x.name.includes(name); });
    if (v) return v;
  }
  return voices.find(function (x) { return x.lang === 'en-US'; }) || voices[0] || null;
}

function _speak(text) {
  if (!('speechSynthesis' in window)) return;
  speechSynthesis.cancel();
  var utt = new SpeechSynthesisUtterance(text);
  utt.voice = _bestVoice();
  utt.rate  = 0.92;
  utt.pitch = 1.0;
  utt.volume = 1.0;
  var btn = document.getElementById('sitrep-btn');
  var vbtn = document.getElementById('voice-btn');
  _speaking = true;
  if (btn) btn.textContent = '⏹ STOP';
  utt.onend = utt.onerror = function () {
    _speaking = false;
    if (btn) btn.textContent = '🔊 SITREP';
    if (vbtn) vbtn.classList.remove('speaking');
  };
  speechSynthesis.speak(utt);
}

async function speakSitrep() {
  if (_speaking) { speechSynthesis.cancel(); return; }
  var resp = await fetch('/api/voice_sitrep');
  var data = await resp.json();
  _speak(data.text);
}

function toggleAutoVoice() {
  _voiceAutoOn = !_voiceAutoOn;
  try { localStorage.setItem('bb_voice_auto', _voiceAutoOn ? '1' : '0'); } catch (e) {}
  var btn = document.getElementById('voice-btn');
  btn.classList.toggle('on', _voiceAutoOn);
  btn.title = _voiceAutoOn ? 'Auto-announce ON — click to disable' : 'Auto-announce new incidents';
}

function _checkNewIncidents(incidents) {
  if (!_voiceAutoOn) return;
  var real = incidents.filter(function (i) { return !i.is_test; });
  for (var k = 0; k < real.length; k++) {
    var inc = real[k];
    if (!_knownIncidentIds.has(inc.id)) {
      _knownIncidentIds.add(inc.id);
      // Don't announce on first page load — only genuinely new ones
      if (_knownIncidentIds.size > real.length) continue;
      var loc = inc.location ? ' at ' + inc.location : '';
      var itype = String(inc.itype || '').replace('/', ' or ');
      var vbtn = document.getElementById('voice-btn');
      if (vbtn) vbtn.classList.add('speaking');
      _speak('Battle Buddy alert. ' + itype + loc + '. ' + (inc.description || ''));
      return; // speak one at a time
    }
  }
}

// Seed known IDs on first load so we don't announce old incidents
function _seedKnownIncidents(incidents) {
  incidents.filter(function (i) { return !i.is_test; }).forEach(function (i) { _knownIncidentIds.add(i.id); });
}

// Init voice button state
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('load', function () {
    var btn = document.getElementById('voice-btn');
    if (btn && _voiceAutoOn) btn.classList.add('on');
    // Seed voices list (Chrome requires a user gesture first, but this primes it)
    try { speechSynthesis.getVoices(); } catch (e) {}
  });
}

// Wire the header buttons without inline onclick handlers (CSP: no inline
// script, no inline handlers). Elements may not exist under Node harnesses;
// guard each lookup.
if (typeof document !== 'undefined') {
  var _sitrepBtn = document.getElementById('sitrep-btn');
  if (_sitrepBtn && _sitrepBtn.addEventListener) _sitrepBtn.addEventListener('click', speakSitrep);
  var _voiceBtn = document.getElementById('voice-btn');
  if (_voiceBtn && _voiceBtn.addEventListener) _voiceBtn.addEventListener('click', toggleAutoVoice);
}

// ---------------------------------------------------------------------------
// City of Austin traffic cameras — a static snapshot overlay
// ---------------------------------------------------------------------------
// Reference context, not an incident feed. The file is a committed snapshot of
// the city's public camera list (refreshed by hand with
// scripts/fetch_austin_cameras.py, live cameras only), so the most it can
// honestly claim is "a camera is published at roughly this point". There is no
// video and no per-camera status, and the popup says so rather than implying
// otherwise.
//
// It stays subordinate to incidents on purpose. Incidents are glowing red
// triangles in markerPane; the cameras are 6px flat dots in the muted slate
// already used for non-incident chrome (#94a3b8 / #475569), with no glow, no
// animation and no colour shared with an incident. Leaflet paints overlayPane
// beneath markerPane, so the layer order is a structural guarantee, not a
// z-index race — the dots can never cover a pin.
//
// The data arrives by fetch because PUBLIC_MAP_HTML is a plain string constant
// on a page whose CSP forbids inline script: there is no template to inject a
// server variable into. /static is the served prefix (audio_receiver.py's Flask
// app: static_folder=/opt/battlebuddy/static, static_url_path=/static), so the
// committed snapshot is already reachable at a stable same-origin URL — one 304
// per page load, not a third-party call.
//
// Failure mode is a missing layer, never a broken map: a 404, a bad parse, an
// unexpected shape or an unexpected Leaflet build each log a warning and return,
// leaving the incident pins, counts and notices exactly as they were.
var CAMERAS_URL = '/static/data/austin_cameras.json';
var cameraLayer = null;
var cameraDots = null;
var cameraZoomBound = false;

function cameraPopupHtml(props, generated) {
  var id = props.id || 'unknown';
  var name = props.name || ('Camera ' + id);
  return '<div class="popup-custom">' +
    '<div class="itype" style="color:#94a3b8">City traffic camera</div>' +
    '<div class="meta">📍 ' + esc(name) + '</div>' +
    '<div class="meta">Camera ID: ' + esc(id) + '</div>' +
    '<div class="transcript">Position is approximate — the city publishes one point per camera, not a surveyed address. No live video or imagery is available here.</div>' +
    '<div class="meta">City of Austin Open Data' +
      (generated ? ' · snapshot ' + esc(generated) : '') + '</div>' +
    '</div>';
}

// ---------------------------------------------------------------------------
// City of Austin traffic cameras — zoom-dependent weight
// ---------------------------------------------------------------------------
// Rendered all at once, 820 dots hazed the whole city and buried the single
// active incident pin downtown, which is the opposite of the intent. Screenshot
// review is the only thing that caught this: the metrics said 820 drawn.
//
// So the layer reads as texture when you are looking at the whole city and as
// reference points when you are close enough to act on one. 820 of anything is
// legible up close and noise from far away.
var CAMERA_DOT_MIN_ZOOM = 12;   // below this: texture only
var CAMERA_DOT_FULL_ZOOM = 15;  // at/above this: full weight

function cameraOpacityForZoom(z) {
  if (z >= CAMERA_DOT_FULL_ZOOM) return 1.0;
  if (z <= CAMERA_DOT_MIN_ZOOM) return 0.16;
  var span = CAMERA_DOT_FULL_ZOOM - CAMERA_DOT_MIN_ZOOM;
  return 0.16 + 0.84 * ((z - CAMERA_DOT_MIN_ZOOM) / span);
}

function applyCameraZoom(z) {
  if (!cameraDots || !cameraDots.length) return;
  var o = cameraOpacityForZoom(z);
  for (var i = 0; i < cameraDots.length; i++) {
    cameraDots[i].setStyle({opacity: o, fillOpacity: Math.min(1, o + 0.25)});
  }
}

async function loadCameras() {
  var data;
  try {
    var resp = await fetch(CAMERAS_URL);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    data = await resp.json();
  } catch (e) {
    console.warn('traffic-camera layer not loaded:', e && e.message ? e.message : e);
    return;
  }
  var features = data && Array.isArray(data.features) ? data.features : null;
  if (!features || !features.length) {
    console.warn('traffic-camera layer: snapshot carries no features, skipping');
    return;
  }
  try {
    if (cameraLayer) { map.removeLayer(cameraLayer); cameraLayer = null; }
    // 820 dots as individual SVG nodes make panning stutter; one canvas draws
    // them all. This is an optimisation, not a dependency — without a canvas
    // renderer Leaflet falls back to SVG and the layer still draws.
    var renderer = L.canvas ? L.canvas({padding: 0.5}) : null;
    var group = L.layerGroup();
    var dots = [];
    var drawn = 0;
    for (var k = 0; k < features.length; k++) {
      var f = features[k] || {};
      var coords = (f.geometry && f.geometry.coordinates) || [];
      var props = f.properties || {};
      // GeoJSON is [lon, lat]; Leaflet wants [lat, lon].
      var lon = coords[0], lat = coords[1];
      if (typeof lon !== 'number' || typeof lat !== 'number') continue;
      var opts = {radius: 3, color: '#94a3b8', weight: 1, opacity: 0.45,
                  fillColor: '#475569', fillOpacity: 0.7};
      if (renderer) opts.renderer = renderer;
      var dot = L.circleMarker([lat, lon], opts);
      dot.bindPopup(cameraPopupHtml(props, data.generated));
      dot.addTo(group);
      dots.push(dot);
      drawn++;
    }
    if (!drawn) {
      console.warn('traffic-camera layer: snapshot had no drawable points');
      return;
    }
    group.addTo(map);
    cameraLayer = group;
    cameraDots = dots;
    applyCameraZoom(map.getZoom());
    if (!cameraZoomBound) {
      cameraZoomBound = true;
      map.on('zoomend', function () { applyCameraZoom(map.getZoom()); });
    }
  } catch (e) {
    cameraLayer = null;
    console.warn('traffic-camera layer failed to draw:', e && e.message ? e.message : e);
  }
}

// ---------------------------------------------------------------------------

async function loadMapStats() {
  try {
    var r = await fetch('/api/stats');
    var d = await r.json();
    document.getElementById('s-calls').textContent = d.calls_24h.toLocaleString();
    document.getElementById('s-incidents').textContent = d.incidents_24h.toLocaleString();
  } catch(e) {}
}

if (typeof document !== 'undefined') {
  loadHeatmap();
  loadIncidents();
  loadMapStats();
  // A committed snapshot: fetched once per page load, never polled.
  loadCameras();
  setInterval(loadHeatmap, 15000);
  setInterval(loadIncidents, 10000);
  setInterval(loadMapStats, 60000);
}
