"""Battle Buddy public-facing page routes.

Extracted from audio_receiver.py to keep the main file focused on the audio/incident
pipeline. Mounted as a Flask Blueprint named public_bp and registered in
audio_receiver.py.
"""

import json
import re
import sqlite3
import time
from datetime import datetime

from flask import Blueprint, jsonify

from modules.config import DB_PATH

public_bp = Blueprint("public", __name__)

# ---------------------------------------------------------------------------
# Public-facing pages
# ---------------------------------------------------------------------------

PUBLIC_SPLASH_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Public Safety Intelligence</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Real-time Austin public safety intelligence. AI-powered P25 radio monitoring, live incident map, and breaking alerts.">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #0a0f1e;
  color: #e2e8f0;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
#hero {
  position: relative;
  flex: 1;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  overflow: hidden;
}
#hero-bg {
  position: absolute;
  inset: 0;
  background-image: url('/static/bgbattlebuddy.png');
  background-size: cover;
  background-position: center;
  filter: brightness(0.35) saturate(0.8);
  z-index: 0;
}
#hero-overlay {
  position: absolute;
  inset: 0;
  background: linear-gradient(to bottom, rgba(10,15,30,0.3) 0%, rgba(10,15,30,0.7) 70%, rgba(10,15,30,1) 100%);
  z-index: 1;
}
#hero-content {
  position: relative;
  z-index: 2;
  max-width: 800px;
  padding: 40px 24px;
}
.logo {
  font-size: 0.8rem;
  letter-spacing: 6px;
  color: #3b82f6;
  text-transform: uppercase;
  margin-bottom: 20px;
}
h1 {
  font-size: clamp(2.2rem, 5vw, 3.8rem);
  font-weight: 800;
  color: #f8fafc;
  line-height: 1.15;
  margin-bottom: 20px;
}
h1 span { color: #3b82f6; }
.sub {
  font-size: clamp(0.95rem, 2vw, 1.2rem);
  color: #94a3b8;
  line-height: 1.6;
  max-width: 580px;
  margin: 0 auto 36px;
}
.cta-row {
  display: flex;
  gap: 14px;
  justify-content: center;
  flex-wrap: wrap;
  margin-bottom: 56px;
}
.btn-primary {
  background: #3b82f6;
  color: white;
  padding: 14px 32px;
  border-radius: 8px;
  text-decoration: none;
  font-weight: 700;
  font-size: 1rem;
  transition: background 0.2s;
}
.btn-primary:hover { background: #2563eb; }
.btn-secondary {
  background: transparent;
  color: #e2e8f0;
  padding: 14px 32px;
  border-radius: 8px;
  border: 1px solid #334155;
  text-decoration: none;
  font-weight: 600;
  font-size: 1rem;
  transition: border-color 0.2s;
}
.btn-secondary:hover { border-color: #3b82f6; color: #3b82f6; }
.live-badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: rgba(239,68,68,0.15);
  border: 1px solid rgba(239,68,68,0.4);
  border-radius: 20px;
  padding: 6px 16px;
  font-size: 0.78rem;
  color: #fca5a5;
  margin-bottom: 28px;
}
.live-dot { width: 8px; height: 8px; background: #ef4444; border-radius: 50%; animation: blink 1s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.stats-row {
  display: flex;
  gap: 40px;
  justify-content: center;
  flex-wrap: wrap;
}
.stat { text-align: center; }
.stat-num { font-size: 2rem; font-weight: 800; color: #3b82f6; }
.stat-label { font-size: 0.72rem; color: #64748b; text-transform: uppercase; letter-spacing: 1px; margin-top: 2px; }
#features {
  background: #0a0f1e;
  padding: 80px 24px;
  text-align: center;
}
#features h2 { font-size: 1.8rem; color: #f8fafc; margin-bottom: 12px; }
#features .sub { font-size: 0.95rem; color: #64748b; margin-bottom: 48px; }
.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 20px;
  max-width: 960px;
  margin: 0 auto 60px;
}
.feature {
  background: #0f1729;
  border: 1px solid #1e3a5f;
  border-radius: 10px;
  padding: 24px;
  text-align: left;
}
.feature .icon { font-size: 1.6rem; margin-bottom: 12px; }
.feature h3 { font-size: 0.9rem; color: #f8fafc; margin-bottom: 6px; }
.feature p { font-size: 0.78rem; color: #64748b; line-height: 1.5; }
.final-cta {
  background: linear-gradient(135deg, #0f1729, #1e3a5f);
  border-top: 1px solid #1e3a5f;
  padding: 60px 24px;
  text-align: center;
}
.final-cta h2 { font-size: 1.6rem; color: #f8fafc; margin-bottom: 10px; }
.final-cta p { color: #64748b; margin-bottom: 28px; font-size: 0.9rem; }
footer {
  background: #0a0f1e;
  border-top: 1px solid #0f1729;
  padding: 20px 24px;
  text-align: center;
  font-size: 0.72rem;
  color: #334155;
}
</style>
</head>
<body>
<section id="hero">
  <div id="hero-bg"></div>
  <div id="hero-overlay"></div>
  <div id="hero-content">
    <div class="logo">&#9652; Battle Buddy</div>
    <div class="live-badge"><span class="live-dot"></span> Live — Austin Metro</div>
    <h1>Austin's Public Safety<br><span>Intelligence Platform</span></h1>
    <p class="sub">AI-powered P25 radio monitoring that listens to every agency simultaneously — and surfaces what matters before any news article exists.</p>
    <div class="cta-row">
      <a href="/premium/" class="btn-primary" style="background:#ef4444;font-size:1.05rem;padding:16px 36px">Subscribe &mdash; from $4/mo &rarr;</a>
      <a href="/public" class="btn-secondary">View Live Map</a>
      <a href="/public/feed" class="btn-secondary">Live Feed</a>
    </div>
    <div class="stats-row" id="stats">
      <div class="stat"><div class="stat-num" id="s-calls">—</div><div class="stat-label">Calls Monitored</div></div>
      <div class="stat"><div class="stat-num" id="s-incidents">—</div><div class="stat-label">Incidents Detected</div></div>
      <div class="stat"><div class="stat-num" id="s-homicides">—</div><div class="stat-label">Homicides Tracked — 2026</div></div>
      <div class="stat"><div class="stat-num" id="s-agencies">—</div><div class="stat-label">Agencies Monitored</div></div>
    </div>
    <div style="font-size:0.72rem;color:#64748b;margin-top:8px;">Last 24 hours &nbsp;·&nbsp; <span id="s-updated">updating...</span></div>
  </div>
</section>

<section id="features">
  <h2>Built for Breaking News</h2>
  <p class="sub">No scanner. No waiting. No missed calls.</p>
  <div class="feature-grid">
    <div class="feature"><div class="icon">📡</div><h3>P25 Radio Monitoring</h3><p>Every APD, AFD, DPS, Travis County EMS, and UT Police transmission captured simultaneously, around the clock.</p></div>
    <div class="feature"><div class="icon">🤖</div><h3>AI Transcription</h3><p>OpenAI Whisper converts every radio transmission to searchable text in near real time.</p></div>
    <div class="feature"><div class="icon">🔍</div><h3>Incident Detection</h3><p>Shootings, structure fires, SWAT activations, air assets, and DPS Capitol responses detected automatically.</p></div>
    <div class="feature"><div class="icon">📈</div><h3>Escalation Tracking</h3><p>From welfare check to K-9 standoff — Battle Buddy tracks the full chain as an incident evolves.</p></div>
    <div class="feature"><div class="icon">🗺️</div><h3>Live Incident Map</h3><p>Every incident plotted in real time across the Austin metro with agency, type, and transcript detail.</p></div>
    <div class="feature"><div class="icon">⚡</div><h3>Instant Alerts</h3><p>Subscribers receive direct alerts the moment a critical incident is detected — before any public notification.</p></div>
    <div class="feature"><div class="icon">🚁</div><h3>Air Asset Tracking &amp; Agency ID</h3><p>ADS-B telemetry monitored continuously. Aircraft tail numbers are cross-referenced against a database of known LEO, EMS, and fire assets — APD Air1, STAR Flight, and other agency helicopters identified by registration and announced on air when active. Intelligence persists even when radio goes encrypted.</p></div>
    <div class="feature"><div class="icon">📰</div><h3>APD Press Release Monitor</h3><p>New APD press releases detected within 5 minutes. Homicides geocoded, mapped, and cross-referenced with scanner data automatically.</p></div>
    <div class="feature"><div class="icon">🔴</div><h3>Austin Homicide Map</h3><p>Every confirmed 2026 homicide from official APD press releases — geocoded, mapped, and linked to the source. Self-updating.</p></div>
    <div class="feature"><div class="icon">🗺️</div><h3>TAK Integration</h3><p>Every detected incident automatically pushed to FreeTAKServer as a CoT marker — appearing in real time on WinTAK, ATAK, and iTAK across your team. Markers auto-clear when the incident closes.</p></div>
    <div class="feature"><div class="icon">🛸</div><h3>FAA Remote ID Drone Detection <span style="font-size:0.58rem;background:#1e3a5f;color:#60a5fa;padding:2px 6px;border-radius:3px;vertical-align:middle;margin-left:4px">COMING SOON</span></h3><p>FAA Remote ID broadcasts from licensed drones captured via SDR and plotted on the live map in real time — adding aerial dimension to ground-level situational awareness.</p></div>
    <div class="feature"><div class="icon">📊</div><h3>Public Intelligence Dashboard</h3><p>Live Grafana dashboard showing shooting intel tiers (confirmed vs signal vs official press release), homicides YTD, structure fires, EMS callouts, radio volume by agency, and incident trends — with a full confidence model so you know exactly how to read each number.</p></div>
    <div class="feature"><div class="icon">🗞️</div><h3>Intel News Feed</h3><p>Every confirmed incident and APD press release delivered as a live RSS feed in Nextcloud News — auto-subscribed at signup. One scrollable feed covering radio detections, press releases, and homicide updates.</p></div>
    <div class="feature"><div class="icon">💬</div><h3>Talk Bot Database Queries</h3><p>Ask the Battle Buddy bot anything directly in Nextcloud Talk. Natural language queries against the live incident and transcript database — how many shootings this week, what radio said during the last homicide call, SWAT callouts in the last 30 days. Type !help to see all commands.</p></div>
  </div>
</section>

<section id="ecosystem" style="padding:64px 24px;background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f">
  <div style="max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center">
    <div style="border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)">
      <img src="/static/nextcloud_ecosystem.png" alt="Battle Buddy connected ecosystem across laptop, phone, and tablet" loading="lazy" style="width:100%;display:block"/>
    </div>
    <div>
      <div style="font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:12px">Connected Platform</div>
      <h2 style="font-size:1.6rem;color:#f8fafc;font-weight:800;line-height:1.25;margin-bottom:16px">Every Device.<br>One Intelligence Feed.</h2>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px">Battle Buddy runs on a private Nextcloud platform — giving subscribers a full connected app ecosystem alongside real-time incident intelligence. Access from any device, anywhere.</p>
      <ul style="list-style:none;margin:20px 0 0;padding:0">
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Talk — encrypted team chat with direct incident alert delivery</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Files — shared briefings, field docs, and ATAK data packages</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Calendar — event coordination and assignment scheduling</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Maps — offline maps for field deployment</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Notes — field intel synced across all your devices</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>News — live incident and press release feed, auto-subscribed at signup</li>
      </ul>
    </div>
  </div>
</section>

<section id="atak-showcase" style="padding:64px 24px;background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f">
  <div style="max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center">
    <div style="border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)">
      <img src="/static/atak_screenshot.png" alt="Battle Buddy incident markers on ATAK — Austin aerial view" loading="lazy" style="width:100%;display:block"/>
    </div>
    <div>
      <div style="font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:12px">TAK Integration</div>
      <h2 style="font-size:1.6rem;color:#f8fafc;font-weight:800;line-height:1.25;margin-bottom:16px">Live Incidents.<br>On Your Tactical Map.</h2>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px">Every incident Battle Buddy detects is automatically pushed to FreeTAKServer as a CoT marker — appearing in real time on WinTAK, ATAK, and iTAK displays across your team.</p>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px">No manual entry. The moment a shooting or structure fire is confirmed, a red marker hits the map at the geocoded address with incident type, timestamp, and description. Markers auto-clear when the incident closes.</p>
      <ul style="list-style:none;margin:20px 0 0;padding:0">
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>CoT markers auto-post on incident detection</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Markers auto-clear when incident closes</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Works with WinTAK, ATAK Phone, iTAK</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:6px 0;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span>Connects via FreeTAKServer SSL — tak.example.local</li>
      </ul>
    </div>
  </div>
</section>
<section class="final-cta">
  <h2>Know Before Anyone Else</h2>
  <p style="max-width:520px;margin:0 auto 10px">Battle Buddy listens to every Austin agency simultaneously and alerts you the moment something happens &mdash; before any news article exists. Basic access starts at $4/mo.</p>
  <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:28px">
    <a href="/premium/" class="btn-primary" style="background:#ef4444;font-size:1.05rem;padding:16px 36px">Subscribe Now &rarr;</a>
    <a href="/public" class="btn-secondary">Explore Free Map</a>
  </div>
  <p style="margin-top:18px;font-size:0.75rem;color:#475569">Basic $4/mo &nbsp;&middot;&nbsp; Premium $11/mo &nbsp;&middot;&nbsp; 7-day free trial &nbsp;&middot;&nbsp; Cancel anytime</p>
</section>

<footer>
  &copy; 2026 Battle Buddy &nbsp;·&nbsp; Austin Metro Public Safety Intelligence &nbsp;·&nbsp;
  <a href="/public" style="color:#3b82f6;text-decoration:none">Live Map</a> &nbsp;·&nbsp;
  <a href="/public/aircraft" style="color:#f59e0b;text-decoration:none">Aircraft</a> &nbsp;&middot;&nbsp;
  <a href="/public/homicides" style="color:#ef4444;text-decoration:none">Homicide Map</a> &nbsp;&middot;&nbsp;
  <a href="/public/feed" style="color:#3b82f6;text-decoration:none">Feed</a> &nbsp;·&nbsp;
  <a href="/public/about" style="color:#3b82f6;text-decoration:none">About</a> &nbsp;·&nbsp;
  <a href="https://kevinwatkins.grafana.net/public-dashboards/235baceac1774dfe8bd12c242acbd014" target="_blank" style="color:#10b981;text-decoration:none">📊 Stats</a>
</footer>

<script>
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('s-calls').textContent = d.calls_24h.toLocaleString();
    document.getElementById('s-incidents').textContent = d.incidents_24h.toLocaleString();
    // fetch homicide count separately
    try {
      const rh = await fetch('/api/homicides');
      const dh = await rh.json();
      const all = (dh.homicides || []).concat(dh.live || []);
      let total = 0; all.forEach(function(h){ total += (h.count || 1); });
      document.getElementById('s-homicides').textContent = total;
    } catch(eh) {}
    document.getElementById('s-agencies').textContent = d.agencies_24h.toLocaleString();
    document.getElementById('s-updated').textContent = 'Updated ' + new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  } catch(e) {}
}
loadStats();
setInterval(loadStats, 60000);
</script>
</body>
</html>
"""

PUBLIC_MAP_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Live Incident Map</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Real-time Austin public safety incident map powered by Battle Buddy. Live P25 radio intelligence for the Austin metro area.">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.heat/dist/leaflet-heat.js"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.heat/dist/leaflet-heat.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; display: flex; flex-direction: column; height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover { color: #3b82f6; }
#topbar .nav a.active { color: #3b82f6; }
#breaking { display: none; padding: 8px 20px; background: linear-gradient(90deg,#7f1d1d,#991b1b); border-bottom: 2px solid #ef4444; font-size: 0.8rem; color: #fca5a5; font-weight: 600; animation: pulse 2s infinite; }
#breaking.show { display: block; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.85} }
#map { flex: 1; }
#legend { position: absolute; bottom: 30px; left: 10px; z-index: 1000; background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f; border-radius: 8px; padding: 12px 16px; font-size: 0.72rem; }
#legend h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.leg-item { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.leg-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
#stats-bar { position: absolute; bottom: 30px; right: 10px; z-index: 1000; background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f; border-radius: 8px; padding: 12px 16px; font-size: 0.72rem; min-width: 160px; }
#stats-bar h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.stat-row { display: flex; justify-content: space-between; gap: 16px; margin-bottom: 3px; color: #cbd5e1; }
.stat-val { color: #3b82f6; font-weight: 600; }
#footer-ticker { background: #0f1729; border-top: 1px solid #1e3a5f; padding: 6px 20px; font-size: 0.7rem; color: #64748b; white-space: nowrap; overflow: hidden; }
#ticker-inner { display: inline-block; animation: scroll 40s linear infinite; }
@keyframes scroll { 0%{transform:translateX(100vw)} 100%{transform:translateX(-100%)} }
.popup-custom { font-family: -apple-system, sans-serif; font-size: 13px; }
.popup-custom .itype { font-weight: 700; color: #ef4444; font-size: 14px; margin-bottom: 4px; }
.popup-custom .meta { color: #64748b; font-size: 11px; margin-bottom: 4px; }
.popup-custom .transcript { color: #374151; font-size: 12px; line-height: 1.4; }
#voice-btn { background: none; border: 1px solid #1e3a5f; color: #64748b; border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; cursor: pointer; display: flex; align-items: center; gap: 5px; transition: all 0.2s; }
#voice-btn:hover { border-color: #3b82f6; color: #3b82f6; }
#voice-btn.on { border-color: #3b82f6; color: #3b82f6; background: rgba(59,130,246,0.1); }
#voice-btn.speaking { border-color: #ef4444; color: #ef4444; background: rgba(239,68,68,0.1); animation: pulse 1s infinite; }
#sitrep-btn { background: none; border: 1px solid #1e3a5f; color: #94a3b8; border-radius: 6px; padding: 4px 10px; font-size: 0.75rem; cursor: pointer; transition: all 0.2s; }
#sitrep-btn:hover { border-color: #3b82f6; color: #3b82f6; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public" class="active">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="https://kevinwatkins.grafana.net/public-dashboards/235baceac1774dfe8bd12c242acbd014" target="_blank">📊 Stats</a>
    <a href="/tip">Submit Tip</a>
  </nav>
  <button id="sitrep-btn" onclick="speakSitrep()" title="Read situation report aloud">&#128266; SITREP</button>
  <button id="voice-btn" onclick="toggleAutoVoice()" title="Auto-announce new incidents">&#128276; AUTO</button>
</div>
<div id="breaking"></div>
<div id="map"></div>
<div id="legend">
  <h4>Agencies</h4>
  <div class="leg-item"><div class="leg-dot" style="background:#3b82f6"></div><span>APD / Law Enforcement</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#f97316"></div><span>AFD / Fire</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#22c55e"></div><span>EMS</span></div>
  <div class="leg-item"><div class="leg-dot" style="background:#a855f7"></div><span>DPS / State</span></div>
  <div class="leg-item"><svg width="12" height="12" viewBox="0 0 12 12" style="filter:drop-shadow(0 0 4px #ef4444);flex-shrink:0"><polygon points="6,0 12,12 0,12" fill="#ef4444" stroke="#fca5a5" stroke-width="1.5"/></svg><span>Active Incident</span></div>
  <div class="leg-item"><svg width="12" height="12" viewBox="0 0 12 12" style="flex-shrink:0"><polygon points="0,0 12,0 6,12" fill="#334155" stroke="#475569" stroke-width="1.5"/></svg><span>Cleared Incident</span></div>
</div>
<div id="stats-bar">
  <h4>Last 48 Hours</h4>
  <div class="stat-row"><span>Calls monitored</span><span class="stat-val" id="s-calls">—</span></div>
  <div class="stat-row"><span>Incidents detected</span><span class="stat-val" id="s-incidents">—</span></div>
  <div class="stat-row"><span>Active now</span><span class="stat-val" id="s-active">—</span></div>
  <div class="stat-row"><span>Last update</span><span class="stat-val" id="s-time">—</span></div>
</div>
<div id="footer-ticker"><div id="ticker-inner">Loading live feed...</div></div>
<script>
const CAT_COLORS = {"APD":"#3b82f6","TCSO":"#3b82f6","UTPD":"#3b82f6","DPS":"#a855f7","AFD":"#f97316","TCFD":"#f97316","TCEMS":"#22c55e","ABIA":"#eab308","Unknown":"#64748b"};
const INCIDENT_COLOR = "#ef4444";

const AUSTIN_BOUNDS = L.latLngBounds(
  L.latLng(29.85, -98.25),   // SW — south of Kyle/Buda, west of Bee Cave
  L.latLng(30.70, -97.25)    // NE — north of Round Rock, east of Bastrop
);
const map = L.map('map', {
  minZoom: 10,
  maxBounds: AUSTIN_BOUNDS,
  maxBoundsViscosity: 1.0
}).setView([30.32, -97.77], 11);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; OpenStreetMap contributors',
  maxZoom: 18
}).addTo(map);

let heatLayer = null;
const incidentMarkers = {};

function catColor(cat) { return CAT_COLORS[cat] || CAT_COLORS['Unknown']; }

function makeIncidentIcon(itype) {
  return L.divIcon({
    html: `<div style="width:20px;height:20px;background:#ef4444;border:2px solid #fca5a5;border-radius:50%;box-shadow:0 0 12px #ef4444;animation:ping 1.5s infinite"></div>`,
    iconSize:[20,20], iconAnchor:[10,10], className:''
  });
}

function timeAgo(ts) {
  const m = Math.round((Date.now()/1000 - ts) / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.round(m/60)}h ago`;
}

async function flagIncident(id, btn) {
  btn.disabled = true;
  btn.textContent = 'Flagging...';
  try {
    await fetch(`/api/incidents/${id}/flag`, {method:'POST'});
    btn.textContent = '✔ FLAGGED';
    btn.style.background = '#16a34a';
  } catch(e) {
    btn.textContent = '⚑ FLAG FOR DEMO';
    btn.disabled = false;
  }
}

async function loadHeatmap() {
  const resp = await fetch('/api/calls');
  const calls = await resp.json();
  const pts = calls.filter(c => c.lat && c.lon && !c.coords_approx && AUSTIN_BOUNDS.contains([c.lat, c.lon])).map(c => [c.lat, c.lon, 0.6]);
  if (heatLayer) map.removeLayer(heatLayer);
  heatLayer = L.heatLayer(pts, {radius:22, blur:18, maxZoom:13,
    gradient:{0.2:'#1e3a5f', 0.5:'#3b82f6', 0.8:'#f97316', 1.0:'#ef4444'}
  }).addTo(map);
  const t = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  document.getElementById('s-time').textContent = t;
  // ticker
  const recent = calls.slice(0,20);
  document.getElementById('ticker-inner').textContent =
    recent.map(c => `${c.tag||'?'} · ${c.transcript ? c.transcript.substring(0,60) : '...'}`).join('   ◆   ');
}

let _incidentsSeeded = false;
async function loadIncidents() {
  const [activeResp, allResp] = await Promise.all([
    fetch('/api/incidents/active'), fetch('/api/incidents')]);
  const active = await activeResp.json();
  const all    = await allResp.json();
  const realAll    = all.filter(i => !i.is_test);
  const realActive = active.filter(i => !i.is_test);
  document.getElementById('s-active').textContent    = realActive.length;

  // Voice: seed on first load, check for new ones on subsequent polls
  if (!_incidentsSeeded) { _seedKnownIncidents(all); _incidentsSeeded = true; }
  else { _checkNewIncidents(all); }

  // Breaking bar — never show test incidents
  const bar = document.getElementById('breaking');
  if (realActive.length > 0) {
    bar.textContent = '⚠ BREAKING: ' + realActive.map(i =>
      i.itype + (i.location ? ' @ ' + i.location : '')).join('  ·  ');
    bar.classList.add('show');
  } else {
    bar.classList.remove('show');
  }

  // Clear old markers
  Object.values(incidentMarkers).forEach(m => map.removeLayer(m));

  // Only plot incidents we have a real address for, and only crime/fire types
  const MAP_ITYPES = new Set([
    "SHOOTING","STABBING","OFFICER DOWN","PURSUIT","WEAPONS",
    "STRUCTURE FIRE","FIRE DISPATCH","FIRE ALARM","FIRE/EMS DISPATCH","GRASS FIRE",
    "CRASH/COLLISION","FATAL CRASH","MULTI-AGENCY RESPONSE","MASS CASUALTY",
    "EMS DISPATCH","HAZMAT","AIR ASSET ACTIVE","DPS CAPITOL ACTIVATION",
    "FLOODING","ROAD HAZARD","PEDESTRIAN INCIDENT","VEHICLE FIRE"
  ]);
  // Add incident markers
  all.filter(i => i.location && i.lat && i.lon && MAP_ITYPES.has(i.itype) && AUSTIN_BOUNDS.contains([i.lat, i.lon])).forEach(inc => {
    const isTest   = inc.is_test === 1;
    const isActive = inc.status === 'active' && !isTest;
    const fill   = isTest ? '#78716c' : (isActive ? '#ef4444' : '#334155');
    const stroke = isTest ? '#a8a29e' : (isActive ? '#fca5a5' : '#475569');
    const opacity = isTest ? 0.45 : 1;
    const size   = isTest ? 12 : (isActive ? 24 : 16);
    const half   = size / 2;
    // Active = point-up triangle, Cleared = point-down triangle
    const pts = isActive
      ? `${half},0 ${size},${size} 0,${size}`
      : `0,0 ${size},0 ${half},${size}`;
    const glowFilter = isActive
      ? `filter:drop-shadow(0 0 6px #ef4444) drop-shadow(0 0 12px #ef4444)`
      : '';
    const icon = L.divIcon({
      html: `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="${glowFilter};opacity:${opacity}"><polygon points="${pts}" fill="${fill}" stroke="${stroke}" stroke-width="1.5"/></svg>`,
      iconSize:[size,size], iconAnchor:[half,half], className:''
    });
    const m = L.marker([inc.lat, inc.lon], {icon}).addTo(map);
    let agencies = '';
    try { agencies = JSON.parse(inc.agencies||'[]').join(', '); } catch(e){}
    m.bindPopup(`
      <div class="popup-custom">
        ${isTest ? `<div style="background:#292524;color:#a8a29e;font-size:10px;font-weight:700;letter-spacing:1px;padding:3px 6px;border-radius:3px;margin-bottom:6px;display:inline-block">SYSTEM TEST — NOT A REAL INCIDENT</div><br>` : ''}
        <div class="itype" style="${isTest?'color:#a8a29e':''}">${inc.itype}</div>
        <div class="meta">${new Date(inc.ts_start*1000).toLocaleString()} · ${timeAgo(inc.ts_start)} · ${inc.status.toUpperCase()}</div>
        ${inc.location ? `<div class="meta">📍 ${inc.location}</div>` : (inc._coords_approx ? `<div class="meta" style="color:#94a3b8">📍 Approximate location (no address extracted)</div>` : '')}
        <div class="meta">Agencies: ${agencies||'unknown'}</div>
        <div class="transcript">${inc.description||''}</div>
        ${!isTest ? `<button onclick="flagIncident(${inc.id},this)" style="margin-top:8px;padding:4px 10px;background:${inc.flagged?'#16a34a':'#1e40af'};color:white;border:none;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">${inc.flagged?'✔ FLAGGED':'⚑ FLAG FOR DEMO'}</button>` : ''}
      </div>
    `);
    incidentMarkers[inc.id] = m;
  });
}

// ---------------------------------------------------------------------------
// Text-to-speech
// ---------------------------------------------------------------------------
let _voiceAutoOn = localStorage.getItem('bb_voice_auto') === '1';
let _knownIncidentIds = new Set();
let _speaking = false;

function _bestVoice() {
  const voices = speechSynthesis.getVoices();
  // Prefer a natural-sounding US English voice
  const prefs = ['Samantha', 'Google US English', 'Microsoft Aria', 'Alex', 'Karen'];
  for (const name of prefs) {
    const v = voices.find(v => v.name.includes(name));
    if (v) return v;
  }
  return voices.find(v => v.lang === 'en-US') || voices[0] || null;
}

function _speak(text) {
  if (!('speechSynthesis' in window)) return;
  speechSynthesis.cancel();
  const utt = new SpeechSynthesisUtterance(text);
  utt.voice = _bestVoice();
  utt.rate  = 0.92;
  utt.pitch = 1.0;
  utt.volume = 1.0;
  const btn = document.getElementById('sitrep-btn');
  const vbtn = document.getElementById('voice-btn');
  _speaking = true;
  if (btn) btn.textContent = '⏹ STOP';
  utt.onend = utt.onerror = () => {
    _speaking = false;
    if (btn) btn.textContent = '🔊 SITREP';
    if (vbtn) vbtn.classList.remove('speaking');
  };
  speechSynthesis.speak(utt);
}

async function speakSitrep() {
  if (_speaking) { speechSynthesis.cancel(); return; }
  const resp = await fetch('/api/voice_sitrep');
  const data = await resp.json();
  _speak(data.text);
}

function toggleAutoVoice() {
  _voiceAutoOn = !_voiceAutoOn;
  localStorage.setItem('bb_voice_auto', _voiceAutoOn ? '1' : '0');
  const btn = document.getElementById('voice-btn');
  btn.classList.toggle('on', _voiceAutoOn);
  btn.title = _voiceAutoOn ? 'Auto-announce ON — click to disable' : 'Auto-announce new incidents';
}

function _checkNewIncidents(incidents) {
  if (!_voiceAutoOn) return;
  const real = incidents.filter(i => !i.is_test);
  for (const inc of real) {
    if (!_knownIncidentIds.has(inc.id)) {
      _knownIncidentIds.add(inc.id);
      // Don't announce on first page load — only genuinely new ones
      if (_knownIncidentIds.size > real.length) continue;
      const loc = inc.location ? ` at ${inc.location}` : '';
      const itype = inc.itype.replace('/', ' or ');
      const vbtn = document.getElementById('voice-btn');
      if (vbtn) vbtn.classList.add('speaking');
      _speak(`Battle Buddy alert. ${itype}${loc}. ${inc.description || ''}`);
      return; // speak one at a time
    }
  }
}

// Seed known IDs on first load so we don't announce old incidents
function _seedKnownIncidents(incidents) {
  incidents.filter(i => !i.is_test).forEach(i => _knownIncidentIds.add(i.id));
}

// Init voice button state
window.addEventListener('load', () => {
  const btn = document.getElementById('voice-btn');
  if (btn && _voiceAutoOn) btn.classList.add('on');
  // Seed voices list (Chrome requires a user gesture first, but this primes it)
  speechSynthesis.getVoices();
});

// ---------------------------------------------------------------------------

async function loadMapStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('s-calls').textContent = d.calls_24h.toLocaleString();
    document.getElementById('s-incidents').textContent = d.incidents_24h.toLocaleString();
  } catch(e) {}
}

loadHeatmap();
loadIncidents();
loadMapStats();
setInterval(loadHeatmap, 15000);
setInterval(loadIncidents, 10000);
setInterval(loadMapStats, 60000);
</script>
</body>
</html>
"""

PUBLIC_FEED_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Live Feed</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; min-height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover, #topbar .nav a.active { color: #3b82f6; }
#breaking { display: none; padding: 8px 20px; background: linear-gradient(90deg,#7f1d1d,#991b1b); border-bottom: 2px solid #ef4444; font-size: 0.8rem; color: #fca5a5; font-weight: 600; }
#breaking.show { display: block; }
#content { max-width: 860px; margin: 0 auto; padding: 24px 16px; }
.section-title { font-size: 0.7rem; letter-spacing: 2px; color: #3b82f6; text-transform: uppercase; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #1e3a5f; }
.incident-card { background: #0f1729; border: 1px solid #1e3a5f; border-left: 4px solid #ef4444; border-radius: 6px; padding: 14px 16px; margin-bottom: 12px; }
.incident-card.cleared { border-left-color: #334155; opacity: 0.7; }
.incident-card .itype { font-weight: 700; color: #ef4444; font-size: 1rem; margin-bottom: 4px; }
.incident-card.cleared .itype { color: #64748b; }
.incident-card .meta { font-size: 0.72rem; color: #64748b; margin-bottom: 6px; }
.incident-card .desc { font-size: 0.82rem; color: #94a3b8; }
.call-row { padding: 10px 0; border-bottom: 1px solid #0f1729; display: flex; gap: 12px; align-items: flex-start; }
.call-row .time { font-size: 0.7rem; color: #475569; min-width: 50px; padding-top: 2px; }
.call-row .tag { font-size: 0.72rem; font-weight: 600; min-width: 140px; }
.call-row .body { flex: 1; }
.call-row .transcript { font-size: 0.78rem; color: #94a3b8; line-height: 1.4; }
.call-row .loc { font-size: 0.68rem; color: #22c55e; margin-top: 2px; }
.cat-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.62rem; margin-left: 4px; vertical-align: middle; }
#live-dot { width: 8px; height: 8px; background: #22c55e; border-radius: 50%; display: inline-block; margin-right: 6px; animation: blink 1s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.tip-card { background: #0f1729; border: 1px solid #1e3a5f; border-left: 4px solid #eab308; border-radius: 6px; padding: 12px 14px; margin-bottom: 10px; }
.tip-card.matched { border-left-color: #22c55e; }
.tip-card.no_data { border-left-color: #475569; opacity: 0.75; }
.tip-card .tip-title { font-size: 0.88rem; font-weight: 600; margin-bottom: 4px; }
.tip-card .tip-title a { color: #e2e8f0; text-decoration: none; }
.tip-card .tip-title a:hover { color: #3b82f6; text-decoration: underline; }
.tip-card .tip-meta { font-size: 0.7rem; color: #64748b; margin-bottom: 6px; }
.tip-card .tip-summary { font-size: 0.78rem; line-height: 1.45; }
.tip-card.matched .tip-summary { color: #4ade80; }
.tip-card.no_data .tip-summary { color: #64748b; }
.tip-card.investigating .tip-summary { color: #94a3b8; }
.tip-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.62rem; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; margin-left: 6px; vertical-align: middle; }
.tip-badge.investigating { background: #422006; color: #fbbf24; }
.tip-badge.investigating .pulse { display:inline-block; width:6px; height:6px; background:#fbbf24; border-radius:50%; margin-right:5px; animation: blink 1.2s infinite; vertical-align: middle; }
.tip-badge.matched { background: #064e3b; color: #4ade80; }
.tip-badge.no_data { background: #1e293b; color: #94a3b8; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed" class="active">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="https://kevinwatkins.grafana.net/public-dashboards/235baceac1774dfe8bd12c242acbd014" target="_blank">📊 Stats</a>
    <a href="/tip">Submit Tip</a>
  </nav>
</div>
<div id="breaking"></div>
<div id="content">
  <div class="section-title">Community Tips</div>
  <div id="tips-section"><p style="color:#475569;font-size:0.8rem">Loading community tips...</p></div>
  <div class="section-title" style="margin-top:28px"><span id="live-dot"></span>Active Incidents</div>
  <div id="incidents-section"></div>
  <div class="section-title" style="margin-top:28px">Recent Radio Activity</div>
  <div id="feed-section"></div>
</div>
<script>
const CAT_COLORS = {"APD":"#3b82f6","TCSO":"#3b82f6","UTPD":"#3b82f6","DPS":"#a855f7","AFD":"#f97316","TCFD":"#f97316","TCEMS":"#22c55e","ABIA":"#eab308","Unknown":"#475569"};

function timeStr(ts) { return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); }
function timeAgo(ts) { const m=Math.round((Date.now()/1000-ts)/60); return m<60?`${m}m ago`:`${Math.round(m/60)}h ago`; }

function tipBadge(status) {
  if (status === 'investigating') return '<span class="tip-badge investigating"><span class="pulse"></span>Investigating</span>';
  if (status === 'matched')      return '<span class="tip-badge matched">Radio Match Found</span>';
  if (status === 'no_data')      return '<span class="tip-badge no_data">Nothing on Radio</span>';
  return '';
}
function tipBody(t) {
  if (t.tip_status === 'matched')      return t.tip_summary || 'Radio match found.';
  if (t.tip_status === 'no_data')      return 'Monitored 2 hours — nothing detected on radio.';
  if (t.tip_status === 'investigating') return 'Checking radio traffic' + (t.tip_location ? (' near ' + t.tip_location) : '') + '...';
  return '';
}
async function refresh() {
  const [callsR, activeR, allR, tipsR] = await Promise.all([
    fetch('/api/calls'), fetch('/api/incidents/active'), fetch('/api/incidents'), fetch('/api/reddit_tips')]);
  const calls = await callsR.json();
  const active = await activeR.json();
  const all = await allR.json();
  let tips = []; try { tips = await tipsR.json(); } catch(e) { tips = []; }

  // Community Tips
  const tipsEl = document.getElementById('tips-section');
  if (!tips.length) {
    tipsEl.innerHTML = '<p style="color:#475569;font-size:0.8rem">No community tips in the last 48 hours.</p>';
  } else {
    tipsEl.innerHTML = tips.map(t => {
      const safeTitle = (t.title||'').replace(/</g,'&lt;');
      return `<div class="tip-card ${t.tip_status||''}">
        <div class="tip-title"><a href="${t.url||'#'}" target="_blank" rel="noopener">${safeTitle}</a>${tipBadge(t.tip_status)}</div>
        <div class="tip-meta">r/${t.subreddit||'Austin'} · ${timeAgo(t.ts)}${t.tip_location?(' · '+t.tip_location):''}</div>
        <div class="tip-summary">${tipBody(t)}</div>
      </div>`;
    }).join('');
  }

  // Breaking bar — never show test incidents
  const realActive = active.filter(i => !i.is_test);
  const bar = document.getElementById('breaking');
  if (realActive.length) { bar.textContent='⚠ BREAKING: '+realActive.map(i=>i.itype+(i.location?' @ '+i.location:'')).join(' · '); bar.classList.add('show'); }
  else bar.classList.remove('show');

  // Incidents — never show test incidents
  const realAll = all.filter(i => !i.is_test);
  const inc = document.getElementById('incidents-section');
  if (!realAll.length) { inc.innerHTML='<p style="color:#475569;font-size:0.8rem">No incidents in the last 48 hours.</p>'; }
  else inc.innerHTML = realAll.map(i => {
    let ag=''; try{ag=JSON.parse(i.agencies||'[]').join(', ');}catch(e){}
    return `<div class="incident-card ${i.status}">
      <div class="itype">${i.itype}${i.location?' <span style="font-weight:400;color:#94a3b8;font-size:0.85rem">@ ${i.location}</span>':''}</div>
      <div class="meta">${new Date(i.ts_start*1000).toLocaleString()} · ${timeAgo(i.ts_start)} · ${i.status.toUpperCase()} · ${ag}</div>
      <div class="desc">${i.description||''}</div>
    </div>`;
  }).join('');

  // Feed
  const feed = document.getElementById('feed-section');
  feed.innerHTML = calls.slice(0,60).map(c => {
    const color = CAT_COLORS[c.category]||'#475569';
    return `<div class="call-row">
      <div class="time">${timeStr(c.ts)}</div>
      <div class="tag" style="color:${color}">${c.tag||'TGID '+c.tgid}<span class="cat-badge" style="background:${color}22;color:${color}">${c.category||'?'}</span></div>
      <div class="body">
        <div class="transcript">${c.transcript||'<em style="color:#334155">transcribing...</em>'}</div>
        ${c.location?`<div class="loc">&#9654; ${c.location}</div>`:''}
      </div>
    </div>`;
  }).join('');
}

refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>
"""

PUBLIC_ABOUT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>About — Battle Buddy Austin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Battle Buddy monitors Austin public safety radio 24/7 — AI transcription, incident detection, homicide mapping, and air asset tracking. Built for journalists and community members.">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0a0f1e;color:#e2e8f0;line-height:1.6}
#topbar{background:#0f1729;border-bottom:1px solid #1e3a5f;padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
#topbar .logo{font-size:1.1rem;font-weight:700;color:#3b82f6;letter-spacing:3px}
#topbar .tagline{font-size:0.72rem;color:#64748b}
#topbar .nav{margin-left:auto;display:flex;gap:16px}
#topbar .nav a{color:#94a3b8;text-decoration:none;font-size:0.8rem}
#topbar .nav a:hover,#topbar .nav a.active{color:#3b82f6}
#stats-strip{background:#060c18;border-bottom:1px solid #1e3a5f;padding:12px 24px;display:flex;justify-content:center;gap:48px;flex-wrap:wrap}
.sstat{text-align:center}
.sstat-num{font-size:1.5rem;font-weight:800;color:#3b82f6}
.sstat-label{font-size:0.65rem;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-top:2px}
.live-pip{display:inline-flex;align-items:center;gap:4px;font-size:0.6rem;color:#fca5a5;margin-left:6px}
.live-dot{width:5px;height:5px;background:#ef4444;border-radius:50%;animation:blink 1s infinite;display:inline-block}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.2}}
#hero-wrap{position:relative;overflow:hidden}
#hero-bg{position:absolute;inset:0;background-image:url('/static/bgbattlebuddy.png');background-size:cover;background-position:center top;filter:brightness(0.28) saturate(0.7);z-index:0}
#hero-overlay{position:absolute;inset:0;background:linear-gradient(to bottom,rgba(10,15,30,0.2) 0%,rgba(10,15,30,0.75) 70%,rgba(10,15,30,1) 100%);z-index:1}
#hero{position:relative;z-index:2;padding:90px 24px 72px;text-align:center;max-width:760px;margin:0 auto}
#atak-showcase{padding:72px 24px;background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f}
.atak-inner{max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center}
@media(max-width:700px){.atak-inner{grid-template-columns:1fr}}
.atak-screen{border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)}
.atak-screen img{width:100%;display:block}
.atak-copy .eyebrow{font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:12px}
.atak-copy h2{font-size:1.6rem;color:#f8fafc;font-weight:800;line-height:1.25;margin-bottom:16px}
.atak-copy p{font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:14px}
.atak-bullets{list-style:none;margin:20px 0 0}
.atak-bullets li{font-size:0.82rem;color:#94a3b8;padding:6px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px}
.atak-bullets li:last-child{border-bottom:none}
.atak-bullets li::before{content:"";width:6px;height:6px;background:#3b82f6;border-radius:50%;flex-shrink:0}
.hero-label{font-size:0.7rem;letter-spacing:4px;color:#3b82f6;text-transform:uppercase;margin-bottom:20px}
#hero h1{font-size:clamp(1.9rem,4vw,2.9rem);font-weight:800;color:#f8fafc;line-height:1.2;margin-bottom:22px}
#hero h1 em{color:#3b82f6;font-style:normal}
#hero .lead{font-size:1rem;color:#94a3b8;line-height:1.75;max-width:600px;margin:0 auto 32px}
.btn-primary{display:inline-block;background:#3b82f6;color:white;padding:13px 34px;border-radius:8px;text-decoration:none;font-weight:700;font-size:0.95rem}
.btn-primary:hover{background:#2563eb}
#pillars{background:#0f1729;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f;padding:52px 24px}
.pillar-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0;max-width:900px;margin:0 auto}
.pillar{text-align:center;padding:28px 24px;border-right:1px solid #1e3a5f}
.pillar:last-child{border-right:none}
@media(max-width:640px){.pillar{border-right:none;border-bottom:1px solid #1e3a5f}}
.pillar .pnum{font-size:2.4rem;font-weight:900;color:#1e3a5f;line-height:1;margin-bottom:10px}
.pillar h3{font-size:1rem;color:#f8fafc;margin-bottom:8px;font-weight:700}
.pillar p{font-size:0.82rem;color:#64748b;line-height:1.6}
.section-wrap{padding:64px 24px}
.section-header{text-align:center;margin-bottom:44px}
.section-header .eyebrow{font-size:0.68rem;letter-spacing:3px;color:#3b82f6;text-transform:uppercase;margin-bottom:8px}
.section-header h2{font-size:1.6rem;color:#f8fafc;margin-bottom:10px}
.section-header p{font-size:0.88rem;color:#64748b;max-width:520px;margin:0 auto}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;max-width:1000px;margin:0 auto}
.feature{background:#0f1729;border:1px solid #1e3a5f;border-radius:10px;padding:20px}
.feature .icon{font-size:1.5rem;margin-bottom:10px}
.feature h3{font-size:0.88rem;color:#f8fafc;margin-bottom:5px;font-weight:600}
.feature p{font-size:0.78rem;color:#64748b;line-height:1.55}
.badge{font-size:0.58rem;background:#1e3a5f;color:#60a5fa;padding:2px 6px;border-radius:3px;vertical-align:middle;margin-left:4px;white-space:nowrap}
#methodology{background:#0f1729;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f;padding:64px 24px}
.method-content{max-width:720px;margin:0 auto}
.method-step{display:flex;gap:20px;margin-bottom:30px;align-items:flex-start}
.step-num{flex-shrink:0;width:34px;height:34px;border:1px solid #1e3a5f;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:0.72rem;color:#3b82f6;font-weight:700}
.method-step h4{font-size:0.88rem;color:#f8fafc;margin-bottom:4px;font-weight:600}
.method-step p{font-size:0.8rem;color:#64748b;line-height:1.6}
.audience-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;max-width:900px;margin:0 auto}
.audience-card{background:#0f1729;border:1px solid #1e3a5f;border-radius:10px;padding:20px}
.audience-card .icon{font-size:1.4rem;margin-bottom:10px}
.audience-card h4{font-size:0.88rem;color:#f8fafc;margin-bottom:6px;font-weight:600}
.audience-card p{font-size:0.78rem;color:#64748b;line-height:1.5}
#cta{background:linear-gradient(135deg,#0f1729,#1a2d4a);border-top:1px solid #1e3a5f;padding:72px 24px;text-align:center}
#cta h2{font-size:1.7rem;color:#f8fafc;margin-bottom:12px}
#cta p{color:#64748b;font-size:0.9rem;max-width:460px;margin:0 auto 28px}
footer{background:#0a0f1e;border-top:1px solid #0f1729;padding:20px 24px;text-align:center;font-size:0.7rem;color:#334155}
footer a{color:#3b82f6;text-decoration:none}
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about" class="active">About</a>
    <a href="https://kevinwatkins.grafana.net/public-dashboards/235baceac1774dfe8bd12c242acbd014" target="_blank">📊 Stats</a>
  </nav>
</div>

<div id="stats-strip">
  <div class="sstat">
    <div class="sstat-num" id="ss-calls">—</div>
    <div class="sstat-label">Calls / 24h <span class="live-pip"><span class="live-dot"></span>live</span></div>
  </div>
  <div class="sstat">
    <div class="sstat-num" id="ss-incidents">—</div>
    <div class="sstat-label">Incidents Detected / 24h</div>
  </div>
  <div class="sstat">
    <div class="sstat-num" id="ss-homicides">—</div>
    <div class="sstat-label">Homicides Mapped — 2026</div>
  </div>
  <div class="sstat">
    <div class="sstat-num" id="ss-agencies">—</div>
    <div class="sstat-label">Agencies Monitored</div>
  </div>
</div>

<div id="hero-wrap">
<div id="hero-bg"></div>
<div id="hero-overlay"></div>
<div id="hero">
  <div class="hero-label">&#9652; Battle Buddy</div>
  <h1>Before the tweet.<br>Before the article.<br><em>Before anyone else knows.</em></h1>
  <p class="lead">Battle Buddy monitors every Austin public safety radio channel around the clock — transcribing, classifying, geocoding, and mapping incidents the moment they happen. No scanner. No waiting. No missed calls.</p>
  <a href="/public" class="btn-primary">Open Live Map</a>
</div>
</div><!-- /hero-wrap -->

<div id="pillars">
  <div class="pillar-grid">
    <div class="pillar">
      <div class="pnum">01</div>
      <h3>Speed</h3>
      <p>Incidents are detected and mapped within seconds of the first radio transmission — before any news desk, tweet, or dispatch alert reaches the public.</p>
    </div>
    <div class="pillar">
      <div class="pnum">02</div>
      <h3>Completeness</h3>
      <p>Every APD, AFD, DPS, Travis County EMS, UT Police, and ABIA transmission captured simultaneously. Battle Buddy never tunes out, never takes a break.</p>
    </div>
    <div class="pillar">
      <div class="pnum">03</div>
      <h3>Verification</h3>
      <p>Every homicide marker links directly to the official APD press release. Scanner intelligence cross-referenced with confirmed public records — not speculation.</p>
    </div>
  </div>
</div>

<div class="section-wrap">
  <div class="section-header">
    <div class="eyebrow">Platform Features</div>
    <h2>Eleven Intelligence Layers. One Feed.</h2>
    <p>Running simultaneously, 24 hours a day, across Austin and Travis County.</p>
  </div>
  <div class="feature-grid">
    <div class="feature">
      <div class="icon">📡</div>
      <h3>P25 Radio Monitoring</h3>
      <p>Software-defined radio captures the Austin GATRRS P25 trunked radio system (WPQY813) across all public safety talkgroups simultaneously, 24/7. Every transmission logged.</p>
    </div>
    <div class="feature">
      <div class="icon">🤖</div>
      <h3>AI Transcription</h3>
      <p>OpenAI Whisper converts every transmission to searchable text in near-real-time. Every call timestamped, tagged by talkgroup, and stored for the complete incident window.</p>
    </div>
    <div class="feature">
      <div class="icon">🔍</div>
      <h3>Incident Detection &amp; Classification</h3>
      <p>AI classifies incidents automatically — shootings, structure fires, SWAT activations, officer-down calls, pursuits, hazmat, mass casualties, and more.</p>
    </div>
    <div class="feature">
      <div class="icon">📈</div>
      <h3>Escalation Tracking</h3>
      <p>From welfare check to K-9 standoff, Battle Buddy tracks the full chain as incidents escalate across dispatch, field, and tactical channels — following the radio traffic as it moves.</p>
    </div>
    <div class="feature">
      <div class="icon">🗺️</div>
      <h3>Live Incident Map</h3>
      <p>Every incident plotted in real time with address, agency, incident type, and transcript excerpt. Geographic patterns visible at a glance across the entire metro.</p>
    </div>
    <div class="feature">
      <div class="icon">⚡</div>
      <h3>Instant Subscriber Alerts</h3>
      <p>Critical incidents trigger direct alerts the moment they are detected — before any public notification, press release, or news broadcast exists.</p>
    </div>
    <div class="feature">
      <div class="icon">🚁</div>
      <h3>Air Asset Tracking</h3>
      <p>ADS-B transponder data monitored continuously. When APD Air1, STAR Flight, or any low-altitude helicopter enters Austin airspace, Battle Buddy maps it and alerts subscribers — intelligence that persists even when radio goes encrypted.</p>
    </div>
    <div class="feature">
      <div class="icon">📰</div>
      <h3>APD Press Release Monitor</h3>
      <p>Austin Police Department press releases are automatically retrieved within 5 minutes of publication. Homicides and major incidents are geocoded, mapped, and cross-referenced with scanner intelligence.</p>
    </div>
    <div class="feature">
      <div class="icon">🔴</div>
      <h3>Austin Homicide Map</h3>
      <p>Every confirmed 2026 Austin homicide sourced directly from official APD press releases — geocoded, mapped, and linked to the source document. Heat map and incident markers. Self-updating. <a href="/public/homicides" style="color:#3b82f6">View it live.</a></p>
    </div>
    <div class="feature">
      <div class="icon">🛸</div>
      <h3>FAA Remote ID Drone Detection <span class="badge">COMING SOON</span></h3>
      <p>FAA Remote ID broadcasts from licensed drones captured via software-defined radio and plotted on the live map in real time — adding aerial dimension to ground-level situational awareness.</p>
    </div>
    <div class="feature">
      <div class="icon">📊</div>
      <h3>Public Intelligence Dashboard</h3>
      <p>Live Grafana dashboard showing shooting intel tiers (confirmed vs signal vs official press release), homicides YTD, structure fires, EMS callouts, radio volume by agency, and incident trends — with a full confidence model so you know exactly how to read each number.</p>
    </div>
    <div class="feature">
      <div class="icon">🗞️</div>
      <h3>Intel News Feed</h3>
      <p>Every confirmed incident and APD press release delivered as a live RSS feed directly in Nextcloud News — auto-subscribed at signup. One scrollable feed covering radio detections, press releases, and homicide updates across Austin.</p>
    </div>
    <div class="feature">
      <div class="icon">💬</div>
      <h3>Talk Bot Database Queries</h3>
      <p>Ask the Battle Buddy bot anything directly in Nextcloud Talk. Natural language queries against the live incident and transcript database — how many shootings this week, what radio said during the last homicide call, SWAT callouts in the last 30 days. Type !help to see all commands.</p>
    </div>
  </div>
</div>

<div class="section-wrap" style="background:#060c18;border-top:1px solid #1e3a5f;border-bottom:1px solid #1e3a5f;padding:72px 24px">
  <div style="max-width:960px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:56px;align-items:center">
    <div style="border-radius:12px;overflow:hidden;border:1px solid #1e3a5f;box-shadow:0 0 40px rgba(59,130,246,0.15)">
      <img src="/static/nextcloud_ecosystem.png" alt="Battle Buddy connected ecosystem across laptop, phone, and tablet" loading="lazy" style="width:100%;display:block"/>
    </div>
    <div>
      <div class="eyebrow">Connected Platform</div>
      <h2 style="margin-bottom:16px">Every Device.<br>One Intelligence Feed.</h2>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:12px">Battle Buddy is built on a private Nextcloud platform — not a generic SaaS stack. Subscribers get access to a full ecosystem of connected apps alongside the real-time intelligence feed, all hosted on the same hardened infrastructure.</p>
      <p style="font-size:0.88rem;color:#64748b;line-height:1.7;margin-bottom:20px">Every app runs on the same server as the scanner pipeline. No third-party data exposure. Accessible from any device — phone, tablet, laptop — in the field or at a desk.</p>
      <ul style="list-style:none;margin:0;padding:0">
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Talk</strong>&nbsp;— encrypted team messaging; where incident alerts are delivered</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Files</strong>&nbsp;— shared briefings, incident archives, and ATAK data packages</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Calendar</strong>&nbsp;— event coordination and assignment scheduling</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Maps</strong>&nbsp;— offline maps for field deployment without cell connectivity</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;border-bottom:1px solid #0f1729;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">Notes</strong>&nbsp;— field intel and incident notes synced across all devices</li>
        <li style="font-size:0.82rem;color:#94a3b8;padding:7px 0;display:flex;align-items:center;gap:10px"><span style="width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0"></span><strong style="color:#cbd5e1">News</strong>&nbsp;— live incident and press release feed, auto-subscribed at signup</li>
      </ul>
    </div>
  </div>
</div>

<section id="methodology">
  <div class="section-header">
    <div class="eyebrow">How It Works</div>
    <h2>Radio Wave to Mapped Incident in Seconds</h2>
    <p>A fully automated pipeline with no human in the loop.</p>
  </div>
  <div class="method-content">
    <div class="method-step">
      <div class="step-num">1</div>
      <div>
        <h4>P25 Radio Capture</h4>
        <p>A software-defined radio receiver continuously monitors the GATRRS P25 trunked radio system — the shared radio backbone for AFD, DPS, Travis County EMS, UT Police, ABIA, Austin Energy, and others. Every unencrypted talkgroup is captured simultaneously, 24 hours a day. APD traffic is P25 encrypted and does not produce transcripts.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">2</div>
      <div>
        <h4>Whisper Transcription</h4>
        <p>Each recorded transmission is passed to OpenAI Whisper running locally. Audio is transcribed to text within seconds, tagged with talkgroup ID, timestamp, and call duration, then logged to the incident database.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">3</div>
      <div>
        <h4>AI Incident Detection</h4>
        <p>Transcripts are analyzed by a large language model that classifies the call type, extracts location information, and determines whether an active incident should be opened, updated, or escalated. Address strings are geocoded in real time against Austin and Travis County data.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">4</div>
      <div>
        <h4>Press Release Cross-Reference</h4>
        <p>APD public press releases are polled every 5 minutes. Confirmed homicides, fatal shootings, and major incidents are automatically pulled, geocoded, and added to the homicide map — linked directly to the official source document so every data point is verifiable.</p>
      </div>
    </div>
    <div class="method-step">
      <div class="step-num">5</div>
      <div>
        <h4>Live Map, Archive &amp; Alerts</h4>
        <p>Active incidents are plotted on the live map and pushed to subscribers. Incidents are tracked until cleared, then archived with full radio traffic transcripts, geocoded address, agency attribution, and escalation chain for the complete incident window.</p>
      </div>
    </div>
  </div>
</section>

<section id="atak-showcase">
  <div class="atak-inner">
    <div class="atak-screen">
      <img src="/static/atak_screenshot.png" alt="Battle Buddy incidents displayed as CoT markers on ATAK — Austin aerial view" loading="lazy"/>
    </div>
    <div class="atak-copy">
      <div class="eyebrow">TAK Integration</div>
      <h2>Live Incidents.<br>On Your Tactical Map.</h2>
      <p>Every incident Battle Buddy detects is automatically pushed to FreeTAKServer as a CoT marker — appearing in real time on WinTAK, ATAK, and iTAK displays across your team.</p>
      <p>No manual entry. No copy-paste. The moment a shooting or structure fire is confirmed, a red marker hits the map at the geocoded address with incident type, timestamp, and description.</p>
      <ul class="atak-bullets">
        <li>CoT markers auto-post on incident detection</li>
        <li>Markers auto-clear when incident closes</li>
        <li>Works with WinTAK, ATAK Phone, iTAK</li>
        <li>Connects via FreeTAKServer over SSL — tak.example.local</li>
        <li>Incident type drives marker color and stale time</li>
      </ul>
    </div>
  </div>
</section>

<div class="section-wrap">
  <div class="section-header">
    <div class="eyebrow">Who Uses Battle Buddy</div>
    <h2>Built for Anyone Who Needs to Know</h2>
  </div>
  <div class="audience-grid">
    <div class="audience-card">
      <div class="icon">📰</div>
      <h4>Journalists &amp; News Desks</h4>
      <p>Beat reporters and assignment desks covering Austin crime, fire, and public safety. Know about breaking incidents before any official statement exists.</p>
    </div>
    <div class="audience-card">
      <div class="icon">🏘️</div>
      <h4>Community Members</h4>
      <p>Residents who want to understand what is actually happening in their neighborhoods — verified and mapped rather than rumor-driven social media posts.</p>
    </div>
    <div class="audience-card">
      <div class="icon">🔬</div>
      <h4>Researchers &amp; Analysts</h4>
      <p>Academics, policy analysts, and public safety researchers who need incident-level data with timestamps, locations, and agency attribution.</p>
    </div>
    <div class="audience-card">
      <div class="icon">⚖️</div>
      <h4>Legal &amp; Insurance Professionals</h4>
      <p>Attorneys, investigators, and adjusters who need timestamped incident records cross-referenced with official press releases and radio traffic logs.</p>
    </div>
  </div>
</div>

<section id="cta">
  <h2>Austin Does Not Slow Down.<br>Neither Do We.</h2>
  <p>Subscriber access includes real-time alerts, full incident history, and the complete intelligence feed — built for people who need to know right now.</p>
  <a href="mailto:admin@libertas.mobi" class="btn-primary">Request Subscriber Access</a>
</section>

<footer>
  &copy; 2026 Battle Buddy &nbsp;&middot;&nbsp; Austin Metro Public Safety Intelligence &nbsp;&middot;&nbsp;
  <a href="/public">Live Map</a> &nbsp;&middot;&nbsp;
  <a href="/public/homicides">Homicide Map</a> &nbsp;&middot;&nbsp;
  <a href="/public/feed">Live Feed</a> &nbsp;&middot;&nbsp;
  <a href="/public/about">About</a>
</footer>

<script>
async function loadStats() {
  try {
    const r = await fetch("/api/stats");
    const d = await r.json();
    document.getElementById("ss-calls").textContent = d.calls_24h.toLocaleString();
    document.getElementById("ss-incidents").textContent = d.incidents_24h.toLocaleString();
    document.getElementById("ss-agencies").textContent = d.agencies_24h.toLocaleString();
  } catch(e) {}
  try {
    const r2 = await fetch("/api/homicides");
    const d2 = await r2.json();
    const all = (d2.homicides || []).concat(d2.live || []);
    let total = 0;
    all.forEach(function(h){ total += (h.count || 1); });
    document.getElementById("ss-homicides").textContent = total;
  } catch(e) {}
}
loadStats();
setInterval(loadStats, 60000);
</script>
</body>
</html>
"""


@public_bp.route("/splash")
def public_splash():
    return PUBLIC_SPLASH_HTML

@public_bp.route("/public")
def public_map():
    return PUBLIC_MAP_HTML


@public_bp.route("/api/homicides")
def api_homicides():
    """Return 2026 homicide data for the heat map — static seed + live DB incidents."""
    import os
    seed_path = "/opt/battlebuddy/homicides_2026.json"
    seed = []
    if os.path.exists(seed_path):
        try:
            with open(seed_path) as f:
                seed = json.load(f)
        except Exception:
            pass

    # Only pull confirmed homicides from DB (APD press release sourced)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id, ts_start, itype, description, location, lat, lon
           FROM incidents
           WHERE itype = 'HOMICIDE'
             AND lat IS NOT NULL AND lon IS NOT NULL
             AND ts_start > strftime('%s','2026-01-01')
             AND is_test = 0"""
    ).fetchall()
    conn.close()

    live = []
    for r in rows:
        live.append({
            "source": "scanner",
            "date": __import__('datetime').datetime.fromtimestamp(r[1]).strftime('%Y-%m-%d'),
            "itype": r[2],
            "summary": r[3][:120] if r[3] else "",
            "address": r[4] or "",
            "lat": r[5],
            "lon": r[6],
            "url": ""
        })

    return jsonify({"homicides": seed, "live": live})

@public_bp.route("/public/feed")
def public_feed():
    return PUBLIC_FEED_HTML



@public_bp.route("/public/feed.rss")
def public_feed_rss():
    """RSS 2.0 feed of confirmed Battle Buddy incidents (last 200, 30 days)."""
    cutoff = time.time() - 86400 * 30
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, ts_start, itype, location, description, article_url FROM incidents "
        "WHERE ts_start > ? AND is_test = 0 "
        "ORDER BY ts_start DESC LIMIT 200",
        (cutoff,)
    ).fetchall()
    conn.close()

    def _esc(s):
        if not s:
            return ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _clean_desc(s):
        if not s:
            return ""
        # JS/JSON noise from Google News article scraping starts at patterns like
        # ","key":  or  ",true,  or  ",[  — cut everything from that point.
        noise = re.search(r'\\["\']', s)
        if noise:
            s = s[:noise.start()].rstrip('., \t')
        s = re.sub(r'\s+', ' ', s).strip()
        if len(s) > 350:
            s = s[:350].rsplit(' ', 1)[0] + "..."
        return s

    items = []
    for inc_id, ts, itype, location, description, article_url in rows:
        dt = datetime.utcfromtimestamp(ts)
        pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        title_loc = f" — {location}" if location else ""
        # For press releases, pull title from description for a cleaner feed title
        if description and description.startswith("[APD Press Release]"):
            pr_title = description[len("[APD Press Release] "):].split(".")[0].strip()
            title = _esc(f"[{itype}] {pr_title[:80]}" if pr_title else f"[{itype}]{title_loc}")
        else:
            title = _esc(f"[{itype}]{title_loc}")
        desc = _esc(_clean_desc(description or itype))
        guid = f"https://battlebuddy.news/public/incident/{inc_id}"
        items.append(
            f"    <item>\n"
            f"      <title>{title}</title>\n"
            f"      <description>{desc}</description>\n"
            f"      <pubDate>{pub_date}</pubDate>\n"
            f"      <guid isPermaLink=\"false\">{guid}</guid>\n"
            f"      <link>{_esc(article_url) if article_url else 'https://battlebuddy.news/public'}</link>\n"
            f"    </item>"
        )

    body = "\n".join(items)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        '  <channel>\n'
        '    <title>Battle Buddy — Austin Public Safety Intelligence</title>\n'
        '    <link>https://battlebuddy.news/public</link>\n'
        '    <description>Real-time confirmed incidents from Austin, TX public safety radio and press releases.</description>\n'
        '    <language>en-us</language>\n'
        f'{body}\n'
        '  </channel>\n'
        '</rss>'
    )
    from flask import Response
    return Response(xml, mimetype="application/rss+xml")


HOMICIDE_MAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Austin Homicide Map 2026 — Battle Buddy</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.heat/dist/leaflet-heat.js"></script>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0a0f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif}
    #topbar{display:flex;align-items:center;gap:16px;padding:10px 20px;background:#0f0f1a;border-bottom:1px solid #1e293b;position:sticky;top:0;z-index:1000}
    .logo{font-weight:800;font-size:1.1rem;color:#f1f5f9;letter-spacing:1px}
    .tagline{font-size:.75rem;color:#64748b;flex:1}
    nav a{color:#94a3b8;text-decoration:none;font-size:.85rem;padding:4px 10px;border-radius:4px;transition:all .2s}
    nav a:hover,nav a.active{color:#f1f5f9;background:#1e293b}
    #header{padding:20px 24px 12px;border-bottom:1px solid #1e293b}
    #header h1{font-size:1.4rem;color:#f8fafc;margin-bottom:4px}
    #header p{font-size:.85rem;color:#64748b}
    #stats-bar{display:flex;gap:24px;padding:12px 24px;background:#0f0f1a;border-bottom:1px solid #1e293b;font-size:.8rem}
    .stat{color:#94a3b8}.stat span{color:#ef4444;font-weight:700;font-size:1rem}
    #controls{display:flex;gap:12px;padding:10px 24px;background:#0f0f1a;border-bottom:1px solid #1e293b;align-items:center;flex-wrap:wrap}
    .ctrl-btn{background:#1e293b;border:1px solid #334155;color:#94a3b8;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:.8rem;transition:all .2s}
    .ctrl-btn.active,.ctrl-btn:hover{background:#1d4ed8;border-color:#3b82f6;color:#fff}
    #map{height:calc(100vh - 220px);width:100%;position:relative}
    #hmap-legend{position:absolute;bottom:40px;right:12px;z-index:1000;background:rgba(10,10,15,.92);border:1px solid #1e293b;border-radius:8px;padding:14px 18px;font-size:.75rem;min-width:190px;backdrop-filter:blur(4px)}
    #hmap-legend h4{color:#94a3b8;font-size:.68rem;letter-spacing:1px;text-transform:uppercase;margin-bottom:10px}
    .hleg-row{display:flex;align-items:center;gap:8px;margin:5px 0;color:#cbd5e1}
    .hleg-dot{width:11px;height:11px;border-radius:50%;flex-shrink:0;border:1.5px solid rgba(255,255,255,.25)}
    .hleg-heat{width:52px;height:8px;border-radius:4px;flex-shrink:0;background:linear-gradient(to right,#1d4ed8,#7c3aed,#dc2626,#ea580c,#fbbf24)}
    .hleg-sq{width:11px;height:11px;border-radius:2px;flex-shrink:0}
    .hleg-divider{border:none;border-top:1px solid #1e293b;margin:8px 0}
    .incident-popup h3{font-size:.9rem;color:#ef4444;margin-bottom:4px}
    .incident-popup p{font-size:.78rem;color:#94a3b8;margin:2px 0}
    .incident-popup a{color:#3b82f6;font-size:.78rem}
    .legend{background:#0f0f1a;border:1px solid #1e293b;padding:10px 14px;border-radius:8px;font-size:.75rem;color:#94a3b8}
    .legend-row{display:flex;align-items:center;gap:8px;margin:3px 0}
    .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
    footer{text-align:center;padding:12px;font-size:.75rem;color:#475569;border-top:1px solid #1e293b}
    #methodology{background:#0a0a0f;border-top:1px solid #1e293b}
    #sources-bar{display:flex;gap:0;flex-wrap:wrap;border-bottom:1px solid #1e293b}
    .source-block{flex:1;min-width:240px;padding:14px 20px;border-right:1px solid #1e293b}
    .source-block:last-child{border-right:none}
    .source-badge{font-size:.7rem;font-weight:700;padding:2px 7px;border-radius:3px;margin-right:6px}
    .source-badge.verified{background:#14532d;color:#4ade80}
    .source-badge.scanner{background:#1e3a5f;color:#60a5fa}
    .source-badge.live{background:#3b1f00;color:#fb923c}
    .source-label{font-size:.82rem;font-weight:600;color:#e2e8f0}
    .source-desc{display:block;font-size:.75rem;color:#64748b;margin-top:4px}
    #nerd-box{border-top:1px solid #1e293b}
    #nerd-box summary{padding:12px 24px;cursor:pointer;font-size:.85rem;color:#64748b;user-select:none;list-style:none}
    #nerd-box summary:hover{color:#94a3b8;background:#0f0f1a}
    #nerd-box summary::marker{display:none}
    .nerd-content{padding:20px 28px 24px;max-width:860px;font-size:.82rem;color:#94a3b8;line-height:1.7}
    .nerd-content h3{color:#e2e8f0;font-size:.9rem;margin:16px 0 6px;letter-spacing:.05em;text-transform:uppercase}
    .nerd-content p{margin-bottom:10px}
    .nerd-content a{color:#3b82f6}
    .nerd-content ul{margin:6px 0 10px 18px}
    .nerd-content li{margin-bottom:4px}
  </style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Real-Time Public Safety Intelligence</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/homicides" class="active">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
    <a href="/tip">Submit Tip</a>
  </nav>
</div>
<div id="header">
  <h1>&#128308; Austin Homicide Map — 2026</h1>
  <p>All confirmed homicides in Austin from January 1, 2026 to present. Click any marker for details and press release links.</p>
</div>
<div id="stats-bar">
  <div class="stat">Total homicides: <span id="total">—</span> <span style="font-size:0.75rem;color:#94a3b8">(<a href="#methodology" style="color:#94a3b8">includes 3 victims from Mar 1 mass shooting — 1 marker</a>)</span></div>
  <div class="stat">Most recent: <span id="latest" style="color:#f59e0b">—</span></div>
  <div class="stat">Hot zone: <span id="hotzone" style="color:#f97316">—</span></div>
  <div class="stat" style="margin-left:auto;color:#475569">Source: APD press releases + Battle Buddy scanner</div>
</div>
<div id="controls">
  <span style="font-size:.8rem;color:#64748b">View:</span>
  <button class="ctrl-btn active" onclick="setMode('heat')" id="btn-heat">Heat Map</button>
  <button class="ctrl-btn" onclick="setMode('markers')" id="btn-markers">Markers</button>
  <button class="ctrl-btn" onclick="setMode('both')" id="btn-both">Both</button>
</div>
<div id="map">
  <div id="hmap-legend">
    <h4>&#9650; Legend</h4>
    <div class="hleg-row"><div class="hleg-heat"></div><span>Incident density</span></div>
    <hr class="hleg-divider"/>
    <div class="hleg-row"><div class="hleg-dot" style="background:#ef4444"></div><span>APD Press Release (verified)</span></div>
    <div class="hleg-row"><div class="hleg-dot" style="background:#f59e0b"></div><span>Scanner detection</span></div>
    <hr class="hleg-divider"/>
    <div class="hleg-row"><div class="hleg-sq" style="background:#7f1d1d;border:1px solid #ef4444"></div><span>Shooting / Homicide</span></div>
    <div class="hleg-row"><div class="hleg-sq" style="background:#1e1b4b;border:1px solid #818cf8"></div><span>Stabbing</span></div>
    <div class="hleg-row"><div class="hleg-sq" style="background:#1c1917;border:1px solid #a8a29e"></div><span>Other violent crime</span></div>
    <hr class="hleg-divider"/>
    <div style="font-size:.68rem;color:#475569;margin-top:2px">Click any marker for details<br/>and press release links.</div>
  </div>
</div>

<div id="methodology">
  <div id="sources-bar">
    <div class="source-block">
      <span class="source-badge verified">&#10003; VERIFIED</span>
      <span class="source-label">APD Press Releases</span>
      <span class="source-desc">Official homicide announcements published at austintexas.gov/news. Each incident links directly to the source document.</span>
    </div>
    <div class="source-block">
      <span class="source-badge scanner">&#9632; SCANNER</span>
      <span class="source-label">Battle Buddy Scanner Detection</span>
      <span class="source-desc">Incidents detected via P25 radio monitoring and AI transcription. Not yet confirmed by press release.</span>
    </div>
    <div class="source-block">
      <span class="source-badge live">&#9654; LIVE</span>
      <span class="source-label">Self-Updating</span>
      <span class="source-desc">New APD press releases are detected automatically within 5 minutes of publication and added to this map.</span>
    </div>
  </div>

  <details id="nerd-box">
    <summary>&#128300; Methodology (for nerds)</summary>
    <div class="nerd-content">
      <h3>Data Sources</h3>
      <p><strong>Primary source:</strong> APD homicide press releases published at
      <a href="https://www.austintexas.gov/news?field_news_type_tid=75" target="_blank">austintexas.gov/news</a>.
      Battle Buddy polls this page every 5 minutes. New articles matching homicide/shooting/death keywords trigger
      automatic article retrieval, address extraction, and geocoding.</p>

      <p><strong>Secondary source:</strong> Battle Buddy&rsquo;s P25 radio scanner pipeline.
      The system monitors Austin&rsquo;s GATRRS trunked radio system (WPQY813, 851 MHz, P25 Phase II),
      transcribes audio using faster-whisper large-v3-turbo (INT8 quantized), and classifies incidents using
      Groq&rsquo;s llama-3.3-70b-versatile LLM. Scanner detections are flagged separately from press-release-verified incidents.</p>

      <h3>Geocoding</h3>
      <p>Street addresses are extracted from press release body text using regex pattern matching and geocoded
      via Nominatim (OpenStreetMap) with Austin, TX and Travis County, TX fallbacks for rural addresses.
      Incidents without a resolvable address are excluded from the map but still appear in Talk alerts.</p>

      <h3>Seed Dataset</h3>
      <p>The 2026 dataset was bootstrapped on April 6, 2026 by manually compiling all APD homicide press releases
      from January 1&ndash;April 5, 2026 (16 confirmed incidents, covering Austin&rsquo;s 1st through 18th homicide of the year).
      All seed records were individually verified against official press releases and geocoded.</p>

      <h3>Limitations</h3>
      <ul>
        <li>Homicides where APD has not yet published a press release will not appear in the verified dataset.</li>
        <li>Scanner detections depend on radio traffic being unencrypted. APD patrol channels (TGIDs 960&ndash;987)
        went to AES-256 encryption in March 2026, significantly reducing real-time APD intelligence.</li>
        <li>Geocoding accuracy is address-level (not GPS-precise). Block-range addresses are plotted at the midpoint.</li>
        <li>The March 1, 2026 mass shooting at 700 W 6th Street is counted as a single map point but represents 3 homicide victims (Austin&rsquo;s 12th&ndash;14th of the year).</li>
      </ul>

      <h3>Technology Stack</h3>
      <p>Battle Buddy runs on a Contabo VPS (Ubuntu 24.04, 24 GB RAM). Radio capture via RTL-SDR on a Raspberry Pi 5.
      P25 trunked decoding via OP25 (GNU Radio). Web stack: Python/Flask, SQLite, Nginx.
      Map: Leaflet.js + leaflet.heat. Geocoding: geopy/Nominatim.</p>
    </div>
  </details>
</div>

<footer>&copy; 2026 Battle Buddy &nbsp;&middot;&nbsp; Austin Metro Public Safety Intelligence</footer>

<script>
const map = L.map('map', {center: [30.307, -97.735], zoom: 11});
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19
}).addTo(map);

let heatLayer = null, markerGroup = L.layerGroup(), mode = 'heat';
let allPoints = [];

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
      const icon = L.divIcon({
        className: '',
        html: '<div style="background:' + ((p.source==='scanner'?'#f59e0b':(p.itype==='STABBING'?'#818cf8':(p.itype==='WEAPONS'?'#a8a29e':'#ef4444')))) +
              ';width:12px;height:12px;border-radius:50%;border:2px solid rgba(255,255,255,.4)"></div>',
        iconSize: [12, 12], iconAnchor: [6, 6]
      });
      const popup = '<div class="incident-popup">' +
        '<h3>#' + (p.n||'') + ' ' + (p.itype||'HOMICIDE') + '</h3>' +
        '<p><b>Date:</b> ' + p.date + '</p>' +
        (p.victim ? '<p><b>Victim:</b> ' + p.victim + '</p>' : '') +
        '<p><b>Location:</b> ' + (p.address||'Unknown') + '</p>' +
        '<p>' + (p.summary||'') + '</p>' +
        (p.url ? '<a href="' + p.url + '" target="_blank">APD Press Release &#8599;</a>' : '') +
        '</div>';
      L.marker([p.lat, p.lon], {icon}).addTo(markerGroup).bindPopup(popup);
    });
    markerGroup.addTo(map);
  }
}

async function load() {
  const r = await fetch('/api/homicides');
  const d = await r.json();
  const seed = (d.homicides||[]).filter(h => h.lat && h.lon);
  const live = (d.live||[]).filter(h => h.lat && h.lon);
  allPoints = [
    ...seed,
    ...live.map(l => ({...l, n: null, victim: null}))
  ];

  document.getElementById('total').textContent = seed.reduce((s,h)=>s+(h.count||1),0) + live.reduce((s,h)=>s+(h.count||1),0);
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
</script>
</body>
</html>"""


@public_bp.route("/public/homicides")
def public_homicides():
    return HOMICIDE_MAP_HTML

@public_bp.route("/public/about")
def public_about():
    return PUBLIC_ABOUT_HTML


PUBLIC_AIRCRAFT_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Austin Aircraft Tracker</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Live low-altitude aircraft tracking over Austin, TX. Helicopters, police air assets, and EMS flight tracking.">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; display: flex; flex-direction: column; height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; flex-wrap: wrap; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover { color: #3b82f6; }
#topbar .nav a.active { color: #3b82f6; }
#map { flex: 1; }
#legend {
  position: absolute; bottom: 30px; left: 10px; z-index: 1000;
  background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f;
  border-radius: 8px; padding: 12px 16px; font-size: 0.72rem;
}
#legend h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.leg-item { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
#status-bar {
  position: absolute; bottom: 30px; right: 10px; z-index: 1000;
  background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f;
  border-radius: 8px; padding: 12px 16px; font-size: 0.72rem; min-width: 180px;
}
#status-bar h4 { color: #94a3b8; margin-bottom: 8px; font-size: 0.7rem; letter-spacing: 1px; text-transform: uppercase; }
.stat-row { display: flex; justify-content: space-between; gap: 16px; margin-bottom: 3px; color: #cbd5e1; }
.stat-val { color: #3b82f6; font-weight: 600; }
#no-aircraft {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
  z-index: 1000; background: rgba(10,15,30,0.92); border: 1px solid #1e3a5f;
  border-radius: 8px; padding: 24px 32px; text-align: center; display: none;
}
#no-aircraft h3 { color: #64748b; margin-bottom: 8px; }
#no-aircraft p { color: #475569; font-size: 0.8rem; }
</style>
</head>
<body>
<div id="topbar">
  <span class="logo">&#9652; BATTLE BUDDY</span>
  <span class="tagline">Austin Metro — Live Aircraft Tracker</span>
  <nav class="nav">
    <a href="/public">Live Map</a>
    <a href="/public/aircraft" class="active">Aircraft</a>
    <a href="/public/homicides">Homicide Map</a>
    <a href="/public/feed">Live Feed</a>
    <a href="/public/about">About</a>
  </nav>
</div>
<div id="map"></div>
<div id="legend">
  <h4>Aircraft</h4>
  <div class="leg-item"><span style="font-size:18px;line-height:1;color:#f59e0b">🚁</span><span style="color:#f59e0b">LEO / EMS (APD, STAR Flight)</span></div>
  <div class="leg-item"><span style="font-size:18px;line-height:1;color:#a855f7">🚁</span><span style="color:#a855f7">Unknown helicopter &lt;5,000ft</span></div>
  <div class="leg-item">
    <svg width="32" height="6" style="flex-shrink:0">
      <line x1="0" y1="3" x2="32" y2="3" stroke="#f59e0b" stroke-width="2" stroke-dasharray="4 4"/>
    </svg>
    <span>30-min flight trail</span>
  </div>
</div>
<div id="status-bar">
  <h4>Status</h4>
  <div class="stat-row"><span>Aircraft tracked</span><span class="stat-val" id="s-count">—</span></div>
  <div class="stat-row"><span>LEO airborne</span><span class="stat-val" id="s-leo">—</span></div>
  <div class="stat-row"><span>Last update</span><span class="stat-val" id="s-time">—</span></div>
</div>
<div id="no-aircraft">
  <h3>No aircraft in range</h3>
  <p>No helicopters below 5,000ft detected within 60 miles of Austin.<br>Checking every 30 seconds.</p>
</div>
<script>
const map = L.map('map').setView([30.2672, -97.7431], 10);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap &amp; CartoDB', maxZoom: 18
}).addTo(map);

const acMarkers = {};
const acTrails  = {};

function makeHeloIcon(isLeo) {
  const color = isLeo ? '#f59e0b' : '#a855f7';
  return L.divIcon({
    html: `<div style="font-size:22px;line-height:1;filter:drop-shadow(0 0 5px ${color});color:${color}">🚁</div>`,
    iconSize: [26,26], iconAnchor: [13,13], className: ''
  });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function flightAwareIdentifier(ac) {
  const fields = [ac.callsign, ac.label, ac.icao24];
  for (const field of fields) {
    const match = String(field || '').toUpperCase().match(/\bN[0-9][A-Z0-9]{1,5}\b/);
    if (match) return match[0];
  }
  const callsign = String(ac.callsign || '').trim().toUpperCase();
  return /^[A-Z0-9]{2,8}$/.test(callsign) ? callsign : '';
}

function flightAwareLink(ac) {
  const ident = flightAwareIdentifier(ac);
  if (!ident) return '';
  const url = `https://www.flightaware.com/live/flight/${encodeURIComponent(ident)}`;
  return `<div style="margin-top:8px"><a href="${url}" target="_blank" rel="noopener noreferrer" style="color:#3b82f6;text-decoration:none;font-weight:600">FlightAware details</a></div>`;
}

function popupHtml(ac) {
  const ago  = Math.round((Date.now()/1000 - ac.ts) / 60);
  const leo  = ac.is_leo ? '<div style="color:#f59e0b;font-weight:700;margin:4px 0">🔴 LAW ENFORCEMENT / EMS</div>' : '';
  const cs   = ac.callsign ? `<div>Flight: <b>${escapeHtml(ac.callsign)}</b></div>` : '';
  const hdg  = ac.heading  ? `${Math.round(ac.heading)}&deg;` : '?';
  const spd  = ac.speed_kts ? `${Math.round(ac.speed_kts)} kts` : '?';
  const label = escapeHtml(ac.label || ac.icao24);
  const icao = escapeHtml(ac.icao24);
  return `
    <div style="font-family:-apple-system,sans-serif;min-width:180px">
      <div style="font-size:15px;font-weight:700;margin-bottom:4px">${label}</div>
      ${leo}${cs}
      <div style="color:#64748b;font-size:12px">
        ICAO: ${icao}<br>
        Alt: <b>${ac.alt_ft ? ac.alt_ft.toLocaleString() + ' ft' : '?'}</b> &nbsp;
        Hdg: ${hdg} &nbsp; Spd: ${spd}<br>
        Updated ${ago}m ago
      </div>
      ${flightAwareLink(ac)}
    </div>`;
}

async function poll() {
  try {
    const resp = await fetch('/api/adsb');
    const aircraft = await resp.json();
    const seen = new Set();

    for (const ac of aircraft) {
      const key = ac.icao24;
      seen.add(key);

      const trailPts   = ac.trail.map(p => [p[0], p[1]]);
      const trailColor = ac.is_leo ? '#f59e0b' : '#a855f7';

      if (acTrails[key]) {
        acTrails[key].setLatLngs(trailPts);
      } else {
        acTrails[key] = L.polyline(trailPts, {
          color: trailColor, weight: 2, opacity: 0.55, dashArray: '5 5'
        }).addTo(map);
      }

      if (acMarkers[key]) {
        acMarkers[key].setLatLng([ac.lat, ac.lon]);
        acMarkers[key].setPopupContent(popupHtml(ac));
      } else {
        acMarkers[key] = L.marker([ac.lat, ac.lon], {icon: makeHeloIcon(ac.is_leo)})
          .bindPopup(popupHtml(ac))
          .addTo(map);
      }
    }

    // Remove aircraft that are gone
    for (const key of Object.keys(acMarkers)) {
      if (!seen.has(key)) {
        acMarkers[key].remove(); delete acMarkers[key];
        if (acTrails[key]) { acTrails[key].remove(); delete acTrails[key]; }
      }
    }

    const total = aircraft.length;
    const leo   = aircraft.filter(a => a.is_leo).length;
    document.getElementById('s-count').textContent = total;
    document.getElementById('s-leo').textContent   = leo;
    document.getElementById('s-time').textContent  = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    document.getElementById('no-aircraft').style.display = total === 0 ? 'block' : 'none';
  } catch(e) {
    document.getElementById('s-time').textContent = 'error';
  }
}

poll();
setInterval(poll, 30000);
</script>
</body>
</html>
"""


@public_bp.route("/public/aircraft")
def public_aircraft():
    return PUBLIC_AIRCRAFT_HTML
