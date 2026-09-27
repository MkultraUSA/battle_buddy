/* Battle Buddy public live feed — safe rendering.
 *
 * Every attacker-influenced value (incident fields, call tags/transcripts,
 * talkgroup names, agencies, locations, tip fields, anything from /receive)
 * reaches the DOM through textContent / setAttribute only — never through
 * innerHTML or HTML-string interpolation — so a stored payload stays inert
 * text. esc() is a tested, defense-in-depth HTML escaper for any future
 * HTML-string context; safeUrl() gates every href so no javascript:/data:/
 * vbscript: URL is ever emitted. Matches the style already used in
 * static/js/tips_review.js, the in-repo precedent.
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

// Only http/https absolute URLs or same-origin relative paths may be emitted
// as a link target. Anything else returns '' so the caller renders no usable
// href instead of a live javascript:/data:/vbscript: sink.
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

var CAT_COLORS = {"APD":"#3b82f6","TCSO":"#3b82f6","UTPD":"#3b82f6","DPS":"#a855f7","AFD":"#f97316","TCFD":"#f97316","TCEMS":"#22c55e","ABIA":"#eab308","Unknown":"#475569"};

function safeColor(cat) {
  return Object.prototype.hasOwnProperty.call(CAT_COLORS, cat) ? CAT_COLORS[cat] : '#475569';
}

function safeTipStatus(s) {
  return (s === 'investigating' || s === 'matched' || s === 'no_data') ? s : '';
}

function safeCardStatus(s) {
  return (s === 'active' || s === 'cleared') ? s : '';
}

function timeStr(ts) { return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); }
function timeAgo(ts) { var m=Math.round((Date.now()/1000-ts)/60); return m<60 ? m+'m ago' : Math.round(m/60)+'h ago'; }

function tipBadge(status) {
  // status is allowlisted by safeTipStatus before it reaches this switch, so
  // every branch below is a static string — no attacker data.
  if (status === 'investigating') return '<span class="tip-badge investigating"><span class="pulse"></span>Investigating</span>';
  if (status === 'matched')      return '<span class="tip-badge matched">Radio Match Found</span>';
  if (status === 'no_data')      return '<span class="tip-badge no_data">Nothing on Radio</span>';
  return '';
}

function tipBodyText(t) {
  if (t.tip_status === 'matched')      return t.tip_summary || 'Radio match found.';
  if (t.tip_status === 'no_data')      return 'Monitored 2 hours — nothing detected on radio.';
  if (t.tip_status === 'investigating') return 'Checking radio traffic' + (t.tip_location ? (' near ' + t.tip_location) : '') + '...';
  return '';
}

function renderTips(tipsEl, tips) {
  if (!tips.length) {
    tipsEl.innerHTML = '<p style="color:#475569;font-size:0.8rem">No community tips in the last 48 hours.</p>';
    return;
  }
  // Every tip field is Reddit-sourced and untrusted: each interpolation is
  // esc()'d, and the href goes through safeUrl() so no javascript:/data:/
  // vbscript: URL is ever emitted (Finding 1). A hostile URL yields an
  // anchor with no href at all — inert text, never a live sink.
  tipsEl.innerHTML = tips.map(function (t) {
    var status = safeTipStatus(t.tip_status);
    var href = safeUrl(t.url);
    var open = href
      ? '<a href="' + esc(href) + '" target="_blank" rel="noopener">'
      : '<a>';
    return '<div class="tip-card ' + status + '">' +
      '<div class="tip-title">' + open + esc(t.title || '') + '</a>' + tipBadge(status) + '</div>' +
      '<div class="tip-meta">r/' + esc(t.subreddit || 'Austin') + ' · ' + esc(timeAgo(t.ts)) +
        (t.tip_location ? (' · ' + esc(t.tip_location)) : '') + '</div>' +
      '<div class="tip-summary">' + esc(tipBodyText(t)) + '</div>' +
      '</div>';
  }).join('');
}

function renderIncidents(incEl, all) {
  var realAll = all.filter(function (i) { return !i.is_test; });
  if (!realAll.length) {
    incEl.innerHTML = '<p style="color:#475569;font-size:0.8rem">No incidents in the last 48 hours.</p>';
    return;
  }
  incEl.innerHTML = realAll.map(function (i) {
    var ag = '';
    try { ag = JSON.parse(i.agencies || '[]').join(', '); } catch (e) { ag = ''; }
    // Finding 2: the real location is interpolated (escaped) here. The old
    // code nested ${i.location} inside a single-quoted string, rendering the
    // literal text "@ ${i.location}" to every visitor instead of the place.
    return '<div class="incident-card ' + safeCardStatus(i.status) + '">' +
      '<div class="itype">' + esc(i.itype) +
        (i.location ? ' <span style="font-weight:400;color:#94a3b8;font-size:0.85rem">@ ' + esc(i.location) + '</span>' : '') + '</div>' +
      '<div class="meta">' + esc(new Date(i.ts_start * 1000).toLocaleString()) + ' · ' +
        esc(timeAgo(i.ts_start)) + ' · ' + esc(String(i.status || '').toUpperCase()) + ' · ' + esc(ag) + '</div>' +
      '<div class="desc">' + esc(i.description || '') + '</div>' +
      '</div>';
  }).join('');
}

function renderCalls(feedEl, calls) {
  // tag / category / transcript / location all originate from /receive ingest
  // or Whisper output: esc() every one. color comes only from the allowlisted
  // CAT_COLORS map (safeColor), never from the raw category string, so it
  // cannot smuggle a style payload.
  feedEl.innerHTML = calls.slice(0, 60).map(function (c) {
    var color = safeColor(c.category);
    return '<div class="call-row">' +
      '<div class="time">' + esc(timeStr(c.ts)) + '</div>' +
      '<div class="tag" style="color:' + color + '">' + esc(c.tag || ('TGID ' + c.tgid)) +
        '<span class="cat-badge" style="background:' + color + '22;color:' + color + '">' +
        esc(c.category || '?') + '</span></div>' +
      '<div class="body">' +
        '<div class="transcript">' +
          (c.transcript ? esc(c.transcript) : '<em style="color:#334155">transcribing...</em>') + '</div>' +
        (c.location ? '<div class="loc">▶ ' + esc(c.location) + '</div>' : '') +
      '</div>' +
      '</div>';
  }).join('');
}

async function refresh() {
  var callsR = await fetch('/api/calls');
  var activeR = await fetch('/api/incidents/active');
  var allR = await fetch('/api/incidents');
  var tipsR = await fetch('/api/reddit_tips');
  var calls = await callsR.json();
  var active = await activeR.json();
  var all = await allR.json();
  var tips = [];
  try { tips = await tipsR.json(); } catch (e) { tips = []; }

  renderTips(document.getElementById('tips-section'), tips);

  // Breaking bar — never show test incidents
  var realActive = active.filter(function (i) { return !i.is_test; });
  var bar = document.getElementById('breaking');
  if (realActive.length) {
    bar.textContent = '⚠ BREAKING: ' + realActive.map(function (i) {
      return i.itype + (i.location ? ' @ ' + i.location : '');
    }).join('  ·  ');
    bar.classList.add('show');
  } else {
    bar.classList.remove('show');
  }

  renderIncidents(document.getElementById('incidents-section'), all);
  renderCalls(document.getElementById('feed-section'), calls);
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  refresh();
  setInterval(refresh, 8000);
}
