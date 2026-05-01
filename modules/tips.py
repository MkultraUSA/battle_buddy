"""Battle Buddy tip submission and review system.

Extracted from audio_receiver.py to keep the main file focused on the audio/incident
pipeline. Mounted as a Flask Blueprint named tips_bp and registered in
audio_receiver.py.
"""

import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from modules.config import DB_PATH, TIPS_UPLOAD_DIR
from modules.geocoding import _geocode_address
from modules.talk import _bot_reply, _get_or_create_dm_room

tips_bp = Blueprint("tips", __name__)

os.makedirs(TIPS_UPLOAD_DIR, exist_ok=True)

_ALLOWED_TIP_EXT = {"jpg", "jpeg", "png", "gif", "webp"}


def _save_tip_photo(file) -> str | None:
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _ALLOWED_TIP_EXT:
        return None
    filename = uuid.uuid4().hex + "." + ext
    file.save(os.path.join(TIPS_UPLOAD_DIR, filename))
    return filename


def _notify_new_tip(tip_id: int, location_text: str, description: str,
                    photo_path: str | None, lat, lon, ts: float):
    """DM kevin when a new tip arrives. Runs in a thread."""
    token = _get_or_create_dm_room("kevin")
    if not token:
        print("[tip] could not get DM room for kevin", flush=True)
        return

    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %I:%M %p")
    coords_str = f"{lat:.5f}, {lon:.5f}" if (lat and lon) else "not geocoded"

    lines = [
        f"\U0001F4CD NEW TIP #{tip_id} — review needed",
        "",
        f"Location: {location_text}",
        f"Coords: {coords_str}",
        f"Time: {time_str}",
    ]
    if description:
        lines += ["", "What they saw:", description]
    if photo_path:
        lines += ["", f"\U0001F4F7 https://battlebuddy.news/static/tips/{photo_path}"]
    lines += [
        "",
        "Review: https://battlebuddy.news/admin/tips",
        f"To investigate: ask me to look into tip #{tip_id}",
    ]

    _bot_reply(token, chr(10).join(lines))
    print(f"[tip] DM sent to kevin for tip #{tip_id}", flush=True)


TIP_FORM_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Submit a Tip</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; min-height: 100vh; }
#topbar { background: #0f1729; border-bottom: 1px solid #1e3a5f; padding: 10px 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
#topbar .logo { font-size: 1.1rem; font-weight: 700; color: #3b82f6; letter-spacing: 3px; }
#topbar .tagline { font-size: 0.75rem; color: #64748b; }
#topbar .nav { margin-left: auto; display: flex; gap: 16px; }
#topbar .nav a { color: #94a3b8; text-decoration: none; font-size: 0.8rem; }
#topbar .nav a:hover { color: #3b82f6; }
#topbar .nav a.active { color: #3b82f6; }
.container { max-width: 600px; margin: 40px auto; padding: 0 20px 60px; }
h1 { font-size: 1.4rem; font-weight: 700; color: #e2e8f0; margin-bottom: 6px; }
.subtitle { color: #64748b; font-size: 0.85rem; margin-bottom: 32px; line-height: 1.5; }
.field { margin-bottom: 22px; }
label { display: block; font-size: 0.8rem; font-weight: 600; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
label .req { color: #ef4444; margin-left: 3px; }
input[type=text], input[type=datetime-local], textarea {
  width: 100%; background: #1e293b; border: 1px solid #1e3a5f; border-radius: 6px;
  color: #e2e8f0; padding: 10px 14px; font-size: 0.9rem; outline: none;
  font-family: inherit; transition: border-color 0.2s;
}
input[type=text]:focus, textarea:focus, input[type=datetime-local]:focus { border-color: #3b82f6; }
textarea { min-height: 120px; resize: vertical; line-height: 1.5; }
.file-label {
  display: flex; align-items: center; gap: 10px; background: #1e293b;
  border: 1px dashed #1e3a5f; border-radius: 6px; padding: 14px;
  cursor: pointer; transition: border-color 0.2s; color: #64748b; font-size: 0.85rem;
}
.file-label:hover { border-color: #3b82f6; color: #94a3b8; }
input[type=file] { display: none; }
#file-name { font-size: 0.8rem; color: #3b82f6; }
.hint { font-size: 0.75rem; color: #475569; margin-top: 6px; }
.anon-note { background: #0f1729; border: 1px solid #1e3a5f; border-radius: 8px; padding: 14px 16px; margin-bottom: 28px; font-size: 0.8rem; color: #64748b; line-height: 1.6; }
.anon-note strong { color: #94a3b8; }
button[type=submit] {
  width: 100%; background: #1d4ed8; color: #fff; border: none; border-radius: 8px;
  padding: 13px; font-size: 0.95rem; font-weight: 600; cursor: pointer;
  letter-spacing: 0.5px; transition: background 0.2s;
}
button[type=submit]:hover { background: #2563eb; }
.hp { display: none; }
#confirm { display: none; text-align: center; padding: 40px 20px; }
#confirm .check { font-size: 3rem; margin-bottom: 16px; }
#confirm h2 { color: #22c55e; margin-bottom: 10px; }
#confirm p { color: #64748b; font-size: 0.9rem; line-height: 1.6; }
#confirm a { color: #3b82f6; }
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
    <a href="/public/about">About</a>
    <a href="/tip" class="active">Submit Tip</a>
  </nav>
</div>
<div class="container">
  <div id="form-wrap">
    <h1>Submit a Tip</h1>
    <p class="subtitle">Saw something in Austin worth knowing about? City operations, unusual activity, police presence, encampments, infrastructure — anything that adds to the picture. All tips are reviewed before appearing anywhere.</p>
    <div class="anon-note"><strong>Anonymous by default.</strong> We do not log your IP address or require an account. What you submit is all we receive.</div>
    <form id="tip-form" enctype="multipart/form-data">
      <div class="field">
        <label>Location <span class="req">*</span></label>
        <input type="text" name="location_text" id="location_text" placeholder="e.g. South Congress and Wasson Road" required autocomplete="off">
        <div class="hint">Street intersection, address, or landmark. Be as specific as you can.</div>
      </div>
      <div class="field">
        <label>What did you see?</label>
        <textarea name="description" placeholder="Describe what you observed — who, what, how many, any vehicles or equipment..."></textarea>
      </div>
      <div class="field">
        <label>When</label>
        <input type="datetime-local" name="observed_at" id="observed_at">
        <div class="hint">Leave blank to use current time.</div>
      </div>
      <div class="field">
        <label>Photo <span style="color:#475569;font-weight:400;text-transform:none">(optional)</span></label>
        <label class="file-label" for="photo">
          <span>&#128247; Attach a photo</span>
          <span id="file-name"></span>
        </label>
        <input type="file" name="photo" id="photo" accept="image/*">
        <div class="hint">JPG, PNG, GIF, or WebP. Max 10 MB.</div>
      </div>
      <div class="hp"><input type="text" name="website" tabindex="-1" autocomplete="off"></div>
      <button type="submit">Submit Tip</button>
    </form>
  </div>
  <div id="confirm">
    <div class="check">&#10003;</div>
    <h2>Tip received</h2>
    <p>Thank you. Your tip has been logged and will be reviewed shortly.<br><br><a href="/public">Return to the live map</a> &nbsp;·&nbsp; <a href="/tip">Submit another</a></p>
  </div>
</div>
<script>
document.getElementById('observed_at').value = new Date().toISOString().slice(0,16);
document.getElementById('photo').addEventListener('change', function() {
  document.getElementById('file-name').textContent = this.files[0] ? this.files[0].name : '';
});
document.getElementById('tip-form').addEventListener('submit', async function(e) {
  e.preventDefault();
  const btn = this.querySelector('button[type=submit]');
  btn.disabled = true; btn.textContent = 'Submitting...';
  const fd = new FormData(this);
  try {
    const r = await fetch('/tip', {method:'POST', body: fd});
    if (r.ok) {
      document.getElementById('form-wrap').style.display = 'none';
      document.getElementById('confirm').style.display = 'block';
    } else {
      btn.disabled = false; btn.textContent = 'Submit Tip';
      alert('Submission failed. Please try again.');
    }
  } catch(err) {
    btn.disabled = false; btn.textContent = 'Submit Tip';
    alert('Network error. Please try again.');
  }
});
</script>
</body>
</html>"""

TIPS_ADMIN_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Battle Buddy — Tip Review</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0f1e; color: #e2e8f0; padding: 30px; }
h1 { font-size: 1.3rem; font-weight: 700; color: #3b82f6; letter-spacing: 2px; margin-bottom: 6px; }
.counts { color: #64748b; font-size: 0.8rem; margin-bottom: 28px; }
.tip-card { background: #0f1729; border: 1px solid #1e3a5f; border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }
.tip-card.approved { border-color: #166534; }
.tip-card.rejected { border-color: #374151; opacity: 0.55; }
.tip-meta { font-size: 0.72rem; color: #64748b; margin-bottom: 8px; }
.tip-location { font-size: 1rem; font-weight: 600; color: #e2e8f0; margin-bottom: 6px; }
.tip-desc { font-size: 0.85rem; color: #94a3b8; line-height: 1.5; margin-bottom: 10px; white-space: pre-wrap; }
.tip-coords { font-size: 0.72rem; color: #475569; margin-bottom: 10px; }
.tip-photo img { max-width: 280px; max-height: 200px; border-radius: 6px; border: 1px solid #1e3a5f; margin-bottom: 10px; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.btn-approve { background: #166534; color: #86efac; border: 1px solid #166534; border-radius: 6px; padding: 6px 16px; font-size: 0.8rem; cursor: pointer; font-weight: 600; }
.btn-approve:hover { background: #15803d; }
.btn-reject { background: transparent; color: #94a3b8; border: 1px solid #374151; border-radius: 6px; padding: 6px 16px; font-size: 0.8rem; cursor: pointer; }
.btn-reject:hover { border-color: #ef4444; color: #ef4444; }
.badge { font-size: 0.7rem; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.badge-pending { background: #1e3a5f; color: #60a5fa; }
.badge-approved { background: #14532d; color: #86efac; }
.badge-rejected { background: #1f2937; color: #6b7280; }
.section-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: #475569; margin: 28px 0 12px; }
.note-input { background: #1e293b; border: 1px solid #1e3a5f; border-radius: 6px; color: #e2e8f0; padding: 6px 10px; font-size: 0.8rem; width: 220px; }
</style>
</head>
<body>
<h1>&#9652; TIP REVIEW</h1>
<div class="counts" id="counts">Loading...</div>
<div class="section-label">Pending Review</div>
<div id="pending-list"></div>
<div class="section-label">Reviewed</div>
<div id="reviewed-list"></div>
<script>
async function loadTips() {
  const r = await fetch('/api/tips');
  const tips = await r.json();
  const pending = tips.filter(t => t.status === 'pending');
  const reviewed = tips.filter(t => t.status !== 'pending');
  document.getElementById('counts').textContent =
    pending.length + ' pending · ' + reviewed.length + ' reviewed · ' + tips.length + ' total';
  document.getElementById('pending-list').innerHTML = pending.map(tipCard).join('') || '<p style="color:#475569;font-size:0.85rem">No pending tips.</p>';
  document.getElementById('reviewed-list').innerHTML = reviewed.map(tipCard).join('') || '<p style="color:#475569;font-size:0.85rem">None yet.</p>';
}
function tipCard(t) {
  const dt = new Date(t.ts * 1000).toLocaleString();
  const badge = `<span class="badge badge-${t.status}">${t.status.toUpperCase()}</span>`;
  const photo = t.photo_path ? `<div class="tip-photo"><img src="/static/tips/${t.photo_path}"></div>` : '';
  const coords = (t.lat && t.lon) ? `<div class="tip-coords">&#128205; ${t.lat.toFixed(5)}, ${t.lon.toFixed(5)}</div>` : '<div class="tip-coords">Location not geocoded</div>';
  const actions = t.status === 'pending' ? `
    <div class="actions">
      <input class="note-input" id="note-${t.id}" placeholder="Reviewer note (optional)">
      <button class="btn-approve" onclick="act(${t.id},'approve')">&#10003; Approve</button>
      <button class="btn-reject" onclick="act(${t.id},'reject')">&#215; Reject</button>
    </div>` : (t.reviewer_note ? `<div style="font-size:0.75rem;color:#475569">Note: ${t.reviewer_note}</div>` : '');
  return `<div class="tip-card ${t.status}" id="card-${t.id}">
    <div class="tip-meta">#${t.id} &nbsp;·&nbsp; ${dt} &nbsp;·&nbsp; ${badge}</div>
    <div class="tip-location">${t.location_text || '(no location)'}</div>
    ${coords}
    <div class="tip-desc">${t.description || '<em style="color:#475569">No description provided.</em>'}</div>
    ${photo}
    ${actions}
  </div>`;
}
async function act(id, action) {
  const note = document.getElementById('note-' + id)?.value || '';
  const r = await fetch('/api/tips/' + id + '/' + action, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({reviewer_note: note})
  });
  if (r.ok) loadTips();
  else alert('Action failed');
}
loadTips();
</script>
</body>
</html>"""


@tips_bp.route("/tip", methods=["GET"])
def tip_form():
    return TIP_FORM_HTML


@tips_bp.route("/tip", methods=["POST"])
def tip_submit():
    # Honeypot — bots fill the hidden website field
    if request.form.get("website"):
        return jsonify({"status": "ok"}), 200  # silent drop

    location_text = (request.form.get("location_text") or "").strip()
    if not location_text:
        return jsonify({"error": "location required"}), 400

    description = (request.form.get("description") or "").strip()
    observed_at  = request.form.get("observed_at") or ""

    # Parse observed time or use now
    try:
        ts = datetime.fromisoformat(observed_at).timestamp() if observed_at else time.time()
    except ValueError:
        ts = time.time()

    # Geocode
    lat, lon = None, None
    coords = _geocode_address(location_text)
    if coords:
        lat, lon = coords

    # Photo
    photo_path = None
    if "photo" in request.files:
        photo_path = _save_tip_photo(request.files["photo"])

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO tips (ts, location_text, lat, lon, description, photo_path, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', 'web')",
        (ts, location_text, lat, lon, description, photo_path)
    )
    tip_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"[tip] new submission: {location_text} | geocoded={lat},{lon} | photo={'yes' if photo_path else 'no'}", flush=True)
    threading.Thread(
        target=_notify_new_tip,
        args=(tip_id, location_text, description, photo_path, lat, lon, ts),
        daemon=True
    ).start()
    return jsonify({"status": "ok"}), 200


@tips_bp.route("/api/reddit_tips")
def api_reddit_tips():
    cutoff = time.time() - 48 * 3600
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT r.ts, r.post_id, r.subreddit, r.title, r.url, r.author,
               r.keywords, r.tip_status, r.tip_location, r.tip_lat, r.tip_lon,
               r.tip_summary, r.tip_ts_start, r.tip_ts_cleared,
               r.incident_id, i.itype, i.location as inc_location
        FROM reddit_intel r
        LEFT JOIN incidents i ON r.incident_id = i.id
        WHERE r.ts > ? AND r.tip_status IS NOT NULL AND r.tip_status != 'new'
        ORDER BY r.ts DESC LIMIT 50
    """, (cutoff,)).fetchall()
    conn.close()
    keys = ["ts","post_id","subreddit","title","url","author","keywords",
            "tip_status","tip_location","tip_lat","tip_lon","tip_summary",
            "tip_ts_start","tip_ts_cleared","incident_id","incident_type","incident_location"]
    return jsonify([dict(zip(keys, row)) for row in rows])


@tips_bp.route("/admin/tips")
def tips_admin():
    return TIPS_ADMIN_HTML


@tips_bp.route("/api/tips")
def api_tips():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tips ORDER BY ts DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@tips_bp.route("/api/tips/<int:tip_id>/approve", methods=["POST"])
def api_tip_approve(tip_id):
    data = request.get_json(silent=True) or {}
    note = (data.get("reviewer_note") or "").strip()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tips SET status='approved', reviewer_note=? WHERE id=?", (note, tip_id))
    conn.commit()
    conn.close()
    print(f"[tip] approved #{tip_id}", flush=True)
    return jsonify({"status": "approved", "id": tip_id})


@tips_bp.route("/api/tips/<int:tip_id>/reject", methods=["POST"])
def api_tip_reject(tip_id):
    data = request.get_json(silent=True) or {}
    note = (data.get("reviewer_note") or "").strip()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tips SET status='rejected', reviewer_note=? WHERE id=?", (note, tip_id))
    conn.commit()
    conn.close()
    print(f"[tip] rejected #{tip_id}", flush=True)
    return jsonify({"status": "rejected", "id": tip_id})
