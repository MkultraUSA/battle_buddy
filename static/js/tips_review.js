/* Battle Buddy tip review surface.
 *
 * All user-controlled fields (location_text, description, reviewer_note from
 * the unauthenticated POST /tip and the approve/reject routes; photo_path is
 * server-generated uuid hex) reach the DOM through textContent / setAttribute
 * only — never through innerHTML or HTML-string interpolation — so a stored
 * payload cannot become executable markup. esc() below is a tested,
 * defense-in-depth HTML escaper for any future HTML-string context.
 */
'use strict';

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;")
    .replace(/`/g, "&#x60;")
    .replace(/=/g, "&#x3D;");
}

function safeStatus(s) {
  return s === 'approved' || s === 'rejected' ? s : 'pending';
}

function safeId(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function safePhotoPath(p) {
  // _save_tip_photo() writes uuid4 hex + allowlisted extension only; refuse
  // anything else so a crafted value can never become an attribute payload.
  const s = String(p || '');
  return /^[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp)$/i.test(s) ? s : '';
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function tipCard(t) {
  const id = safeId(t.id);
  const status = safeStatus(t.status);
  const card = el('div', 'tip-card ' + status);
  card.id = 'card-' + id;

  const dt = new Date(Number(t.ts) * 1000).toLocaleString();
  const meta = el('div', 'tip-meta');
  const badge = el('span', 'badge badge-' + status, status.toUpperCase());
  meta.appendChild(document.createTextNode('#' + id + '  ·  ' + dt + '  ·  '));
  meta.appendChild(badge);
  card.appendChild(meta);

  card.appendChild(el('div', 'tip-location', t.location_text || '(no location)'));

  const lat = Number(t.lat);
  const lon = Number(t.lon);
  const coords = el('div', 'tip-coords');
  if (Number.isFinite(lat) && Number.isFinite(lon) && (lat || lon)) {
    coords.textContent = '\uD83D\uDCCC ' + lat.toFixed(5) + ', ' + lon.toFixed(5);
  } else {
    coords.textContent = 'Location not geocoded';
  }
  card.appendChild(coords);

  const desc = el('div', 'tip-desc');
  if (t.description) {
    desc.textContent = t.description;
  } else {
    const em = document.createElement('em');
    em.setAttribute('style', 'color:#475569');
    em.textContent = 'No description provided.';
    desc.appendChild(em);
  }
  card.appendChild(desc);

  const photo = safePhotoPath(t.photo_path);
  if (photo) {
    const wrap = el('div', 'tip-photo');
    const img = document.createElement('img');
    img.setAttribute('src', '/static/tips/' + photo);
    img.setAttribute('alt', 'tip photo');
    wrap.appendChild(img);
    card.appendChild(wrap);
  }

  if (status === 'pending') {
    const actions = el('div', 'actions');
    const input = document.createElement('input');
    input.className = 'note-input';
    input.id = 'note-' + id;
    input.setAttribute('placeholder', 'Reviewer note (optional)');
    input.setAttribute('maxlength', '500');
    const approve = el('button', 'btn-approve', '\u2713 Approve');
    approve.setAttribute('type', 'button');
    approve.addEventListener('click', function () { act(id, 'approve'); });
    const reject = el('button', 'btn-reject', '\u00D7 Reject');
    reject.setAttribute('type', 'button');
    reject.addEventListener('click', function () { act(id, 'reject'); });
    actions.appendChild(input);
    actions.appendChild(approve);
    actions.appendChild(reject);
    card.appendChild(actions);
  } else if (t.reviewer_note) {
    const note = el('div', undefined, 'Note: ' + t.reviewer_note);
    note.setAttribute('style', 'font-size:0.75rem;color:#475569');
    card.appendChild(note);
  }
  return card;
}

function emptyMsg(text) {
  const p = document.createElement('p');
  p.setAttribute('style', 'color:#475569;font-size:0.85rem');
  p.textContent = text;
  return p;
}

async function loadTips() {
  const pendingList = document.getElementById('pending-list');
  const reviewedList = document.getElementById('reviewed-list');
  const counts = document.getElementById('counts');
  let tips;
  try {
    const r = await fetch('/api/tips');
    if (!r.ok) {
      counts.textContent = r.status === 401 || r.status === 403
        ? 'Reviewer sign-in required.'
        : 'Failed to load tips.';
      return;
    }
    tips = await r.json();
  } catch (e) {
    counts.textContent = 'Failed to load tips.';
    return;
  }
  const pending = tips.filter(function (t) { return safeStatus(t.status) === 'pending'; });
  const reviewed = tips.filter(function (t) { return safeStatus(t.status) !== 'pending'; });
  counts.textContent = pending.length + ' pending · ' + reviewed.length + ' reviewed · ' + tips.length + ' total';
  pendingList.replaceChildren();
  if (!pending.length) {
    pendingList.appendChild(emptyMsg('No pending tips.'));
  } else {
    pending.forEach(function (t) { pendingList.appendChild(tipCard(t)); });
  }
  reviewedList.replaceChildren();
  if (!reviewed.length) {
    reviewedList.appendChild(emptyMsg('None yet.'));
  } else {
    reviewed.forEach(function (t) { reviewedList.appendChild(tipCard(t)); });
  }
}

async function act(id, action) {
  id = safeId(id);
  if (!id || (action !== 'approve' && action !== 'reject')) return;
  const noteEl = document.getElementById('note-' + id);
  const note = noteEl ? noteEl.value : '';
  const r = await fetch('/api/tips/' + id + '/' + action, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({reviewer_note: note})
  });
  if (r.ok) loadTips();
  else alert('Action failed');
}

// Only auto-run in a browser (document defined). Under node-based esc() unit
// tests this module is loaded purely for its esc function.
if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  loadTips();
}
