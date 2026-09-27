/* Battle Buddy splash page stats. Text-only updates (textContent), no HTML
 * interpolation, no inline handlers — CSP compatible (no inline script).
 */
'use strict';

async function loadStats() {
  try {
    var r = await fetch('/api/stats');
    var d = await r.json();
    document.getElementById('s-calls').textContent = d.calls_24h.toLocaleString();
    document.getElementById('s-incidents').textContent = d.incidents_24h.toLocaleString();
    // fetch homicide count separately
    try {
      var rh = await fetch('/api/homicides');
      if (!rh.ok) {
        // Seed unavailable (503) — never render a fabricated zero.
        document.getElementById('s-homicides').textContent = 'unavailable';
      } else {
        var dh = await rh.json();
        var total = dh.total_area_homicides || 0;
        document.getElementById('s-homicides').textContent = total;
      }
    } catch (eh) {}
    document.getElementById('s-agencies').textContent = d.agencies_24h.toLocaleString();
    document.getElementById('s-updated').textContent = 'Updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  } catch (e) {}
}

if (typeof document !== 'undefined') {
  loadStats();
  setInterval(loadStats, 60000);
}
