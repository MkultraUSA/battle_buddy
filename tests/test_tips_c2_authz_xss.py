"""Regression tests for slate item C2: tip admin authz + stored XSS.

All admin reviewer surfaces (/api/tips, /admin/tips, approve, reject) must
require an authorized admin; approve/reject must validate the tip id and
leave an audit record; tipCard() must neutralise HTML/JS payloads; the admin
page must carry a restrictive CSP; public POST /tip must stay anonymous.

Tests run offline: Nextcloud credential validation is stubbed, the database
is a temporary SQLite file, and geocoding/notification side effects are
stubbed. Tests must not read .env.
"""

import base64
import re
import shutil
import sqlite3
import subprocess
import sys
import types

import pytest
from flask import Flask

# stripe is not installed in this environment; stub it before importing
# modules.premium (same pattern as tests/test_premium_login_case.py).
sys.modules.setdefault("stripe", types.SimpleNamespace(api_key=None))

# Collection-order hygiene (assertions below untouched): tests/test_pi_watchdog.py
# installs a minimal modules.config stub into sys.modules at its own import time.
# When the whole suite is collected in one pytest process, that stub (which has no
# __file__) shadows the real config for modules imported afterwards, so importing
# modules.premium here would fail with ImportError. Evict a file-less stub so the
# real modules.config is imported. Standalone runs already have the real config
# (has __file__) and take no action.
_config_mod = sys.modules.get("modules.config")
if _config_mod is not None and getattr(_config_mod, "__file__", None) is None:
    del sys.modules["modules.config"]

from modules import premium as premium_mod  # noqa: E402
from modules import tips as tips_mod  # noqa: E402

XSS_PAYLOAD = (
    '"><img src=x onerror=alert("XSS")>'
    "<script>alert('XSS')</script>"
    "' onfocus='alert(1)"
)


def _basic(username, password="secret"):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest.fixture()
def tip_db(tmp_path, monkeypatch):
    db = tmp_path / "tips_test.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tips ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "ts REAL NOT NULL,"
        "location_text TEXT,"
        "lat REAL,"
        "lon REAL,"
        "description TEXT,"
        "photo_path TEXT,"
        "status TEXT DEFAULT 'pending',"
        "source TEXT DEFAULT 'web',"
        "incident_id INTEGER,"
        "reviewer_note TEXT)"
    )
    conn.execute(
        "CREATE TABLE sessions (token TEXT PRIMARY KEY, username TEXT, "
        "created_ts REAL, expires_ts REAL, is_admin INTEGER, is_premium INTEGER)"
    )
    conn.execute(
        "INSERT INTO tips (ts, location_text, lat, lon, description, "
        "photo_path, status, source, incident_id) "
        "VALUES (1700000000, 'Congress and 6th', 30.267, -97.743, "
        "'suspicious van', NULL, 'pending', 'web', NULL)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(tips_mod, "DB_PATH", str(db))
    monkeypatch.setattr(premium_mod, "DB_PATH", str(db))
    # Offline: stub Nextcloud credential validation. Return True so the
    # Basic-auth path reaches the real _is_admin check (kevin/mrrob admin,
    # everyone else refused with 403). Never weakens _is_admin itself.
    monkeypatch.setattr(premium_mod, "_nc_validate_user", lambda u, p: True)
    # Offline: no geocoding network, no Talk DM thread side effects.
    monkeypatch.setattr(tips_mod, "_geocode_address", lambda address: None)
    monkeypatch.setattr(tips_mod, "_notify_new_tip", lambda *a: None)
    return db


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(tips_mod.tips_bp)
    app.testing = True
    return app.test_client()


def _tip_status(db, tip_id=1):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT status, reviewer_note FROM tips WHERE id=?", (tip_id,)).fetchone()
    conn.close()
    return row


# --- A. authorize the read -------------------------------------------------

def test_anonymous_get_api_tips_denied(client, tip_db):
    r = client.get("/api/tips")
    assert r.status_code in (401, 403)
    assert "suspicious van" not in r.get_data(as_text=True)
    assert "Congress and 6th" not in r.get_data(as_text=True)


def test_non_admin_get_api_tips_refused(client, tip_db):
    r = client.get("/api/tips", headers=_basic("randomuser"))
    assert r.status_code == 403
    assert "suspicious van" not in r.get_data(as_text=True)


def test_admin_get_api_tips_field_filtered(client, tip_db):
    r = client.get("/api/tips", headers=_basic("kevin"))
    assert r.status_code == 200
    rows = r.get_json()
    assert isinstance(rows, list) and len(rows) == 1
    allowed = {"id", "ts", "location_text", "lat", "lon", "description",
               "photo_path", "status", "reviewer_note"}
    for row in rows:
        assert set(row.keys()) <= allowed, f"over-exposed fields: {set(row) - allowed}"
        assert "source" not in row
        assert "incident_id" not in row


# --- B. authorize the reviewer UI ------------------------------------------

def test_anonymous_get_admin_tips_denied(client, tip_db):
    r = client.get("/admin/tips")
    assert r.status_code in (401, 403)


def test_non_admin_get_admin_tips_refused(client, tip_db):
    r = client.get("/admin/tips", headers=_basic("randomuser"))
    assert r.status_code == 403


# --- C. authorize approve / reject + audit ---------------------------------

def test_anonymous_approve_changes_nothing(client, tip_db):
    r = client.post("/api/tips/1/approve", json={"reviewer_note": "pwned"})
    assert r.status_code in (401, 403)
    assert _tip_status(tip_db) == ("pending", None)


def test_anonymous_reject_changes_nothing(client, tip_db):
    r = client.post("/api/tips/1/reject", json={"reviewer_note": "pwned"})
    assert r.status_code in (401, 403)
    assert _tip_status(tip_db) == ("pending", None)


def test_non_admin_approve_refused(client, tip_db):
    r = client.post("/api/tips/1/approve", json={"reviewer_note": "x"},
                    headers=_basic("randomuser"))
    assert r.status_code == 403
    assert _tip_status(tip_db) == ("pending", None)


def test_approve_missing_tip_is_not_success(client, tip_db):
    r = client.post("/api/tips/9999/approve", json={"reviewer_note": "x"},
                    headers=_basic("kevin"))
    assert r.status_code == 404


def test_reject_missing_tip_is_not_success(client, tip_db):
    r = client.post("/api/tips/9999/reject", json={"reviewer_note": "x"},
                    headers=_basic("kevin"))
    assert r.status_code == 404


def test_approve_leaves_audit_record(client, tip_db):
    r = client.post("/api/tips/1/approve", json={"reviewer_note": "looks real"},
                    headers=_basic("kevin"))
    assert r.status_code == 200
    assert _tip_status(tip_db) == ("approved", "looks real")
    conn = sqlite3.connect(tip_db)
    rows = conn.execute(
        "SELECT admin_username, tip_id, action, ts FROM tip_audit ORDER BY id DESC LIMIT 1"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    admin_username, tip_id, action, ts = rows[0]
    assert admin_username == "kevin"
    assert tip_id == 1
    assert action == "approve"
    assert ts and float(ts) > 0


def test_reject_leaves_audit_record(client, tip_db):
    r = client.post("/api/tips/1/reject", json={"reviewer_note": "not credible"},
                    headers=_basic("mrrob"))
    assert r.status_code == 200
    assert _tip_status(tip_db) == ("rejected", "not credible")
    conn = sqlite3.connect(tip_db)
    rows = conn.execute(
        "SELECT admin_username, tip_id, action, ts FROM tip_audit ORDER BY id DESC LIMIT 1"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    admin_username, tip_id, action, ts = rows[0]
    assert admin_username == "mrrob"
    assert tip_id == 1
    assert action == "reject"
    assert ts and float(ts) > 0


# --- D. stored XSS neutralised ----------------------------------------------

def _admin_page_js():
    """Return (page_html, js_source) for the reviewer surface under test.

    The admin HTML references the external review script; fall back to any
    inline script block so this test fails loudly on the vulnerable layout.
    """
    html = tips_mod.TIPS_ADMIN_HTML
    m = re.search(r'<script\s+src="([^"]+)"', html)
    if m:
        src = m.group(1)
        name = src.rsplit("/", 1)[-1]
        candidates = [
            tips_mod_path_parent() / "static" / "js" / name,
        ]
        for path in candidates:
            if path.exists():
                return html, path.read_text()
        raise AssertionError(f"admin page references {src} but file is missing")
    inlines = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert inlines, "admin page has no script at all"
    return html, "\n".join(inlines)


def tips_mod_path_parent():
    import pathlib
    return pathlib.Path(tips_mod.__file__).parent.parent


def _run_escaper(js_source, payload):
    """Execute the page's esc() against payload; return the escaped string."""
    assert re.search(r"function\s+esc\s*\(", js_source), "no esc() function defined"
    harness = js_source + "\n;globalThis.__out = esc(globalThis.__payload);"
    node = shutil.which("node")
    if node:
        proc = subprocess.run(
            [node, "-e",
             f"globalThis.__payload = {payload!r};\n" + harness + "\n;process.stdout.write(String(globalThis.__out));"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, f"node failed: {proc.stderr}"
        return proc.stdout
    # Fallback without node: emulate the literal replace-chain in esc().
    pairs = re.findall(r'\.replace\(\s*/(.*?)/g\s*,\s*"([^"]*)"\s*\)', js_source)
    assert pairs, "esc() has no recognisable replace chain"
    out = payload
    for pattern, repl in pairs:
        out = re.sub(pattern, repl, out)
    return out


def test_no_raw_user_field_interpolation(client, tip_db):
    html, js = _admin_page_js()
    for field in ("t.location_text", "t.description", "t.reviewer_note"):
        assert field not in js or "esc(" in js, f"{field} interpolated without escaping"
    assert "${t.location_text" not in js
    assert "${t.description" not in js
    assert "${t.reviewer_note}" not in js
    # innerHTML must never receive a raw user-controlled field.
    for m in re.finditer(r"\.innerHTML\s*=\s*(.*?);", js, re.S):
        chunk = m.group(1)
        assert "location_text" not in chunk
        assert "reviewer_note" not in chunk
        assert "description" not in chunk


def test_escaper_neutralises_xss_payload():
    _, js = _admin_page_js()
    # The escaper must map every HTML-significant character to a real entity,
    # never to itself (a previous esc() shipped identity mappings).
    for entity in ("&amp;", "&lt;", "&gt;", "&quot;"):
        assert entity in js, f"escaper missing entity {entity}"
    assert "&#x27;" in js or "&#39;" in js
    escaped = _run_escaper(js, XSS_PAYLOAD)
    assert XSS_PAYLOAD not in escaped, "raw payload survives escaping"
    assert "<img" not in escaped
    assert "<script" not in escaped
    assert "onerror=" not in escaped
    assert "onfocus=" not in escaped
    assert "&lt;" in escaped and "&gt;" in escaped and "&quot;" in escaped


# --- E. restrictive CSP on the admin surface --------------------------------

def test_admin_page_has_restrictive_csp(client, tip_db):
    r = client.get("/admin/tips", headers=_basic("kevin"))
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy", "")
    assert csp, "admin page sets no Content-Security-Policy"
    assert "default-src 'self'" in csp
    script_src = re.search(r"script-src([^;]*)", csp)
    assert script_src, "CSP has no script-src directive"
    assert "'unsafe-inline'" not in script_src.group(1), \
        "CSP script-src allows inline script"
    assert "object-src 'none'" in csp


# --- Scope: public submission stays open ------------------------------------

def test_public_tip_submit_still_works_anonymously(client, tip_db):
    r = client.post("/tip", data={"location_text": "Rainey St",
                                  "description": "loud party"})
    assert r.status_code == 200
    conn = sqlite3.connect(tip_db)
    row = conn.execute(
        "SELECT location_text, description, status FROM tips ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row == ("Rainey St", "loud party", "pending")


def test_tip_honeypot_still_drops_silently(client, tip_db):
    conn = sqlite3.connect(tip_db)
    before = conn.execute("SELECT COUNT(*) FROM tips").fetchone()[0]
    conn.close()
    r = client.post("/tip", data={"location_text": "x", "website": "bot"})
    assert r.status_code == 200
    assert r.get_json() == {"status": "ok"}
    conn = sqlite3.connect(tip_db)
    after = conn.execute("SELECT COUNT(*) FROM tips").fetchone()[0]
    conn.close()
    assert after == before
