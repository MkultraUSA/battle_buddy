#!/usr/bin/env python3
"""
Battle Buddy — Heatmap Generator
Reads incidents from SQLite DB and produces a standalone heatmap.html
Run manually or appended to run_parser.sh after each parse cycle.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH  = Path(__file__).parent / "logs" / "battle_buddy.db"
OUT_PATH = Path(__file__).parent / "logs" / "heatmap.html"

SEVERITY_COLOR = {
    "high":    "#e63946",
    "medium":  "#f4a261",
    "low":     "#2a9d8f",
    "unknown": "#adb5bd",
}

INCIDENT_ICON = {
    "welfare check":      "🏥",
    "medical":            "🏥",
    "mental health":      "🏥",
    "collision":          "🚗",
    "accident":           "🚗",
    "hit and run":        "🚗",
    "mvc":                "🚗",
    "fire":               "🔥",
    "shooting":           "🔫",
    "armed robbery":      "🔫",
    "disturbance":        "⚠️",
    "fight":              "⚠️",
    "suspicious person":  "🚔",
    "suspicious vehicle": "🚔",
    "suspicious":         "🚔",
    "arrest":             "🚔",
    "warrant":            "🚔",
    "theft":              "🚔",
    "burglary":           "🚔",
    "trespass":           "🚔",
    "pursuit":            "🚔",
    "dwi":                "🚔",
    "panic alarm":        "🚨",
    "alarm":              "🚨",
    "bomb threat":        "💣",
}

def get_icon(inc_type: str) -> str:
    t = inc_type.lower()
    for key, icon in INCIDENT_ICON.items():
        if key in t:
            return icon
    return "📍"

def load_incidents():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, type, address, severity, lat, lon, stream, talkgroup_raw
        FROM incidents
        WHERE lat IS NOT NULL AND lon IS NOT NULL AND deleted = 0
        ORDER BY timestamp DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def build_html(incidents: list[dict]) -> str:
    # Build data structures for Leaflet
    heat_points = []   # [lat, lon, intensity]
    markers     = []   # full incident detail for popups

    for inc in incidents:
        sev = inc.get("severity", "unknown").lower()
        intensity = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(sev, 0.4)
        heat_points.append([inc["lat"], inc["lon"], intensity])

        icon  = get_icon(inc.get("type", ""))
        color = SEVERITY_COLOR.get(sev, SEVERITY_COLOR["unknown"])
        markers.append({
            "lat":       inc["lat"],
            "lon":       inc["lon"],
            "title":     f"{icon} {inc.get('type','unknown').title()}",
            "address":   inc.get("address", ""),
            "severity":  sev,
            "timestamp": inc.get("timestamp", ""),
            "talkgroup": inc.get("talkgroup_raw", "") or "",
            "color":     color,
        })

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    incident_count = len(incidents)

    heat_json    = json.dumps(heat_points)
    markers_json = json.dumps(markers)

    # Count by type for summary
    type_counts: dict[str, int] = {}
    for inc in incidents:
        t = inc.get("type", "unknown") or "unknown"
        type_counts[t] = type_counts.get(t, 0) + 1
    type_summary = " &nbsp;|&nbsp; ".join(
        f"{get_icon(t)} {t.title()}: <b>{n}</b>"
        for t, n in sorted(type_counts.items(), key=lambda x: -x[1])
    ) or "No incidents yet"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Battle Buddy — Incident Heatmap</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #1a1a2e; color: #eee; }}
  #header {{
    padding: 10px 16px;
    background: #16213e;
    border-bottom: 2px solid #e63946;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }}
  #header h1 {{ font-size: 1.2rem; color: #e63946; letter-spacing: 1px; }}
  #header .meta {{ font-size: 0.75rem; color: #aaa; }}
  #summary {{
    padding: 6px 16px;
    background: #0f3460;
    font-size: 0.8rem;
    border-bottom: 1px solid #333;
  }}
  #controls {{
    padding: 8px 16px;
    background: #16213e;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
    border-bottom: 1px solid #333;
  }}
  #controls label {{ font-size: 0.8rem; color: #ccc; }}
  #controls select, #controls input {{
    background: #1a1a2e;
    color: #eee;
    border: 1px solid #444;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 0.8rem;
  }}
  #map {{ height: calc(100vh - 130px); width: 100%; }}
  .legend {{
    background: rgba(22,33,62,0.92);
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 0.75rem;
    line-height: 1.8;
    border: 1px solid #444;
  }}
  .legend-dot {{
    display: inline-block;
    width: 12px; height: 12px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
  }}
</style>
</head>
<body>
<div id="header">
  <h1>⚡ Battle Buddy — Incident Heatmap</h1>
  <span class="meta">Austin Metro &nbsp;|&nbsp; {incident_count} incidents &nbsp;|&nbsp; Generated: {generated}</span>
</div>
<div id="summary">{type_summary}</div>
<div id="controls">
  <label>Severity:
    <select id="sevFilter" onchange="applyFilters()">
      <option value="all">All</option>
      <option value="high">High</option>
      <option value="medium">Medium</option>
      <option value="low">Low</option>
    </select>
  </label>
  <label>Type:
    <select id="typeFilter" onchange="applyFilters()">
      <option value="all">All Types</option>
    </select>
  </label>
  <label><input type="checkbox" id="showMarkers" checked onchange="applyFilters()"> Show markers</label>
  <label><input type="checkbox" id="showHeat" checked onchange="applyFilters()"> Show heatmap</label>
</div>
<div id="map"></div>

<script>
const ALL_INCIDENTS = {markers_json};
const ALL_HEAT      = {heat_json};

// Init map centered on Austin
const map = L.map('map').setView([30.2672, -97.7431], 11);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OpenStreetMap &copy; CARTO',
  subdomains: 'abcd', maxZoom: 19
}}).addTo(map);

// Populate type filter
const types = [...new Set(ALL_INCIDENTS.map(i => i.title.replace(/^.+? /, '')))].sort();
const typeSelect = document.getElementById('typeFilter');
types.forEach(t => {{
  const o = document.createElement('option');
  o.value = t; o.textContent = t;
  typeSelect.appendChild(o);
}});

// Layer groups
let markerGroup = L.layerGroup().addTo(map);
let heatLayer   = L.heatLayer(ALL_HEAT, {{radius: 35, blur: 25, maxZoom: 17, max: 1.0}}).addTo(map);

function makeMarker(inc) {{
  const circle = L.circleMarker([inc.lat, inc.lon], {{
    radius: 8,
    fillColor: inc.color,
    color: '#fff',
    weight: 1.5,
    opacity: 1,
    fillOpacity: 0.85
  }});
  circle.bindPopup(`
    <b>${{inc.title}}</b><br>
    <b>Address:</b> ${{inc.address}}<br>
    <b>Severity:</b> ${{inc.severity}}<br>
    <b>Time:</b> ${{inc.timestamp}}<br>
    ${{inc.talkgroup ? '<b>Talkgroup:</b> ' + inc.talkgroup : ''}}
  `);
  return circle;
}}

function applyFilters() {{
  const sev      = document.getElementById('sevFilter').value;
  const type     = document.getElementById('typeFilter').value;
  const showM    = document.getElementById('showMarkers').checked;
  const showH    = document.getElementById('showHeat').checked;

  markerGroup.clearLayers();

  const filtered = ALL_INCIDENTS.filter(i => {{
    const sevOk  = sev  === 'all' || i.severity === sev;
    const typeOk = type === 'all' || i.title.includes(type);
    return sevOk && typeOk;
  }});

  if (showM) filtered.forEach(i => markerGroup.addLayer(makeMarker(i)));

  if (heatLayer) {{ map.removeLayer(heatLayer); heatLayer = null; }}
  if (showH) {{
    const heatData = filtered.map(i => [i.lat, i.lon,
      {{high:1.0, medium:0.6, low:0.3}}[i.severity] || 0.4]);
    heatLayer = L.heatLayer(heatData, {{radius: 35, blur: 25, maxZoom: 17, max: 1.0}}).addTo(map);
  }}
}}

// Initial render
applyFilters();

// Legend
const legend = L.control({{position: 'bottomright'}});
legend.onAdd = () => {{
  const div = L.DomUtil.create('div','legend');
  div.innerHTML = `
    <b>Severity</b><br>
    <span class="legend-dot" style="background:#e63946"></span>High<br>
    <span class="legend-dot" style="background:#f4a261"></span>Medium<br>
    <span class="legend-dot" style="background:#2a9d8f"></span>Low<br>
  `;
  return div;
}};
legend.addTo(map);
</script>
</body>
</html>
"""

def main():
    incidents = load_incidents()
    html = build_html(incidents)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"[heatmap] {len(incidents)} incidents → {OUT_PATH}")

if __name__ == "__main__":
    main()
