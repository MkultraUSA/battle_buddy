// Battle Buddy homicide map — presentation layer only.
// Classification (means) and verification filtering inputs come from
// GET /api/homicides, which computes them server-side in
// modules/homicide_count.py (covered by tests/test_homicide_means.py).
// Keep regexes OUT of this file: inline JS inside Python triple-quoted
// strings silently eats backslash escapes at import time.

// Basemap: Esri World_Street_Map, the provider used by the other public maps
// (modules/public.py PUBLIC_MAP_HTML, templates/aircraft.html). Esri's tile
// service is {z}/{y}/{x} — the opposite of the OpenStreetMap leaflet
// {z}/{x}/{y} order. Keep this template and its credits intact: the tile
// order and the attribution string are a preservation contract asserted by
// tests/test_homicide_map_js.py.
const map = L.map('map', {center: [30.307, -97.735], zoom: 11});
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles &copy; Esri — Esri, HERE, Garmin, &copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);

let heatLayer = null, markerGroup = L.layerGroup(), mode = 'heat';
let allPoints = [];

// Marker colors by means of death (server-computed `means` field).
const MEANS_COLORS = {SHOOTING:'#ef4444', STABBING:'#818cf8', OTHER:'#a8a29e', UNKNOWN:'#a8a29e'};

// --- Safe HTML helpers for the runtime-generated popup -----------------------
// The popup is assembled by string concatenation and handed to Leaflet's
// bindPopup as raw HTML, so every value spliced into it has to be made
// attribute/text safe first. Deliberately written without regexes or
// backslash escapes, per the file header: this stays safe even if the script
// is ever inlined into a Python triple-quoted string.

const HTML_ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};

function escapeHtml(value) {
  const s = (value === null || value === undefined) ? '' : String(value);
  let out = '';
  for (let i = 0; i < s.length; i++) {
    const ch = s.charAt(i);
    out += Object.prototype.hasOwnProperty.call(HTML_ESCAPES, ch) ? HTML_ESCAPES[ch] : ch;
  }
  return out;
}

// Return a normalised http(s) URL, or '' when the value is not safe to use as
// a link target. A `javascript:`/`data:`/other-scheme value returns '' so the
// caller omits the anchor entirely rather than emitting a live XSS sink.
function safeHttpUrl(value) {
  if (typeof value !== 'string') return '';
  const raw = value.trim();
  if (!raw) return '';
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (e) {
    return '';
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
  return parsed.href;
}

function setMode(m) {
  mode = m;
  ['heat','markers','both'].forEach(id => {
    document.getElementById('btn-'+id).classList.toggle('active', id === m);
  });
  render();
}

function render() {
  if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
  markerGroup.clearLayers();

  if (mode === 'heat' || mode === 'both') {
    heatLayer = L.heatLayer(allPoints.map(p => [p.lat, p.lon, 1.0]), {
      radius: 35, blur: 25, maxZoom: 14,
      gradient: {0.2:'#1d4ed8', 0.4:'#7c3aed', 0.6:'#dc2626', 0.8:'#ea580c', 1.0:'#fbbf24'}
    }).addTo(map);
  }

  if (mode === 'markers' || mode === 'both') {
    allPoints.forEach(p => {
      const means = p.means || 'UNKNOWN';
      const icon = L.divIcon({
        className: '',
        html: '<div style="background:' + (p.source==='scanner' ? '#f59e0b' : (MEANS_COLORS[means]||'#ef4444')) +
              ';width:12px;height:12px;border-radius:50%;border:2px solid rgba(255,255,255,.4)"></div>',
        iconSize: [12, 12], iconAnchor: [6, 6]
      });
      // Validate the scheme first, then attribute-escape: a hostile
      // `javascript:` URL is dropped (no anchor at all) and a URL carrying a
      // quote cannot break out of the href attribute.
      const pressRelease = safeHttpUrl(p.url);
      const popup = '<div class="incident-popup">' +
        '<h3>#' + (p.n||'') + ' ' + means + '</h3>' +
        '<p><b>Date:</b> ' + p.date + '</p>' +
        (p.victim ? '<p><b>Victim:</b> ' + p.victim + '</p>' : '') +
        '<p><b>Location:</b> ' + (p.address||'Unknown') + '</p>' +
        '<p>' + (p.summary||'') + '</p>' +
        (pressRelease ? '<a href="' + escapeHtml(pressRelease) +
          '" target="_blank" rel="noopener">APD Press Release &#8599;</a>' : '') +
        '</div>';
      L.marker([p.lat, p.lon], {icon}).addTo(markerGroup).bindPopup(popup);
    });
    markerGroup.addTo(map);
  }
}

async function load() {
  const r = await fetch('/api/homicides');
  if (!r.ok) {
    // Seed unavailable (503): show the fault instead of a fake total of 0.
    document.getElementById('total').textContent = 'unavailable';
    return;
  }
  const d = await r.json();
  // Verified APD press releases ONLY. Unverified scanner detections stay
  // in the API for internal review but must never appear on a page
  // titled "confirmed homicides".
  const seed = (d.homicides||[]).filter(h => h.lat && h.lon && (h.source||'') !== 'scanner');
  allPoints = seed;

  document.getElementById('total').textContent = allPoints.length;
  if (seed.length) {
    const latest = seed.slice().sort((a,b) => b.date.localeCompare(a.date))[0];
    document.getElementById('latest').textContent = latest.date + ' — ' + (latest.address||'');
  }

  // Find hottest neighborhood (rough grid cell with most hits)
  const grid = {};
  allPoints.forEach(p => {
    const key = (Math.round(p.lat*20)/20).toFixed(2) + ',' + (Math.round(p.lon*20)/20).toFixed(2);
    grid[key] = (grid[key]||0) + 1;
  });
  const hot = Object.entries(grid).sort((a,b) => b[1]-a[1])[0];
  if (hot && hot[1] > 1) document.getElementById('hotzone').textContent = hot[1] + ' incidents near ' + hot[0];

  render();
}

load();
