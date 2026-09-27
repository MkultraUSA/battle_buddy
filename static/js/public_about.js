/* Battle Buddy about page stats. Text-only updates (textContent), no HTML
 * interpolation, no inline handlers — CSP compatible (no inline script).
 */
'use strict';

async function loadStats() {
  try {
    var r = await fetch('/api/stats');
    var d = await r.json();
    document.getElementById('ss-calls').textContent = d.calls_24h.toLocaleString();
    document.getElementById('ss-incidents').textContent = d.incidents_24h.toLocaleString();
    document.getElementById('ss-agencies').textContent = d.agencies_24h.toLocaleString();
  } catch (e) {}
  try {
    var r2 = await fetch('/api/homicides');
    if (!r2.ok) {
      // Seed unavailable (503) — never render a fabricated zero.
      document.getElementById('ss-homicides').textContent = 'unavailable';
    } else {
      var d2 = await r2.json();
      var total = d2.total_area_homicides || 0;
      document.getElementById('ss-homicides').textContent = total;
    }
  } catch (e2) {}
}

if (typeof document !== 'undefined') {
  loadStats();
  setInterval(loadStats, 60000);
}
