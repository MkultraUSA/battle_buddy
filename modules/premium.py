
"""Battle Buddy premium member and subscription management.

Extracted from audio_receiver.py to keep the main file focused on the audio/incident
pipeline. This module provides auth/provisioning helpers only; it declares no
routes. (A previous unregistered blueprint with dead route copies was
removed; the live Stripe routes live in modules/stripe_billing.py as stripe_bp.)
"""

import base64
import json
import os
import pwd
import shutil
import sqlite3

# Bypass SSL cert verification (Nextcloud snap cert not in system store)
import ssl as _ssl_mod
import subprocess
import time
import urllib.parse
import urllib.request

from flask import jsonify, request

from modules.config import (
    DB_PATH,
    MAILGUN_API_KEY,
    MAILGUN_DOMAIN,
    MAILGUN_FROM,
    NC_PASS,
    NC_USER,
)

_ssl_ctx = _ssl_mod._create_unverified_context()


# STRIPE + AUTH — Premium membership integration
# ===========================================================================
import secrets as _secrets  # noqa: E402

import stripe as _stripe  # noqa: E402

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")  # legacy
STRIPE_PLANS = {
    "premium_monthly": {"price_id": "price_1TGmOYIkODTTsH8IeoQPtVXf", "tier": "premium"},
    "premium_annual":  {"price_id": "price_1TGmPjIkODTTsH8IKHU4a5xK", "tier": "premium"},
    "basic_monthly":   {"price_id": "price_1TGmMzIkODTTsH8IipK3zPVr", "tier": "basic"},
    "basic_annual":    {"price_id": "price_1TGmNrIkODTTsH8IpSy0yHNi", "tier": "basic"},
}
STRIPE_PRICE_TO_TIER = {v["price_id"]: v["tier"] for v in STRIPE_PLANS.values()}

if STRIPE_SECRET_KEY:
    _stripe.api_key = STRIPE_SECRET_KEY

NEXTCLOUD_WEB_BASE = os.environ.get("NEXTCLOUD_WEB_BASE", "https://nextcloud.example.com")

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _nc_validate_user(username, password):
    """Validate Nextcloud credentials via OCS API. Returns True/False."""
    import urllib.parse
    url = os.environ.get("NEXTCLOUD_OCS_USER_URL", "https://nextcloud.example.com/ocs/v2.php/cloud/user")
    auth_b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=6) as r:
            data = json.loads(r.read())
            return data.get("ocs", {}).get("meta", {}).get("status") == "ok"
    except Exception:
        return False


def _is_premium(username):
    """Check if username has an active premium record."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT status FROM premium_users WHERE lower(username)=lower(?)", (username,)
    ).fetchone()
    conn.close()
    return row is not None and row[0] == "active"


def _is_admin(username):
    """Simple admin check — kevin is always admin."""
    return username.lower() in ("kevin", "mrrob")


def _issue_session(username):
    """Create a session token, store in DB, return token string."""
    token = _secrets.token_hex(32)
    now = time.time()
    expires = now + 86400 * 30  # 30 days
    conn = sqlite3.connect(DB_PATH)
    canonical = conn.execute(
        "SELECT username FROM premium_users WHERE lower(username)=lower(?) LIMIT 1",
        (username,),
    ).fetchone()
    session_username = canonical[0] if canonical else username
    premium = _is_premium(username)
    admin = _is_admin(username)
    conn.execute(
        "INSERT OR REPLACE INTO sessions (token, username, created_ts, expires_ts, is_admin, is_premium) "
        "VALUES (?,?,?,?,?,?)",
        (token, session_username, now, expires, int(admin), int(premium))
    )
    conn.commit()
    conn.close()
    return token


def _get_session(request_obj):
    """Extract and validate session token from cookie or Authorization header.
    Returns dict {username, is_admin, is_premium} or None."""
    token = request_obj.cookies.get("bb_session") or \
            request_obj.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, expires_ts, is_admin, is_premium FROM sessions WHERE token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    username, expires_ts, is_admin, is_premium = row
    if time.time() > expires_ts:
        return None
    return {"username": username, "is_admin": bool(is_admin), "is_premium": bool(is_premium)}


def _require_premium(f):
    """Decorator: require valid session with is_premium=True."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        sess = _get_session(request)
        if not sess:
            return jsonify({"error": "login required"}), 401
        if not sess["is_premium"] and not sess["is_admin"]:
            return jsonify({"error": "premium required"}), 403
        return f(*args, **kwargs)
    return decorated


def _require_admin(f):
    """Decorator: require valid session with is_admin=True."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        sess = _get_session(request)
        if not sess or not sess["is_admin"]:
            return jsonify({"error": "admin required"}), 403
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Provisioning helpers
# ---------------------------------------------------------------------------

TALK_ROOMS = {
    "incidents": "89q5fnh5",
    "apd":       "m38srso2",
    "fire-ems":  "ee6si4vj",
    "general":   "iyidr3xy",
}


def _ensure_intent_tables(conn):
    """Create the C1 billing tables if they are missing (idempotent).

    Production databases created before this packet lack the tables, so every
    entry point (checkout, webhook, provisioning) ensures them before reading.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS premium_checkout_intents (
            intent_id         TEXT PRIMARY KEY,
            username          TEXT NOT NULL,
            display_name      TEXT,
            nc_password       TEXT NOT NULL,
            tier              TEXT,
            plan              TEXT,
            created_ts        REAL NOT NULL,
            expires_ts        REAL NOT NULL,
            consumed_ts       REAL,
            stripe_session_id TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_checkout_intents_username
            ON premium_checkout_intents(username)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stripe_processed_events (
            event_id TEXT PRIMARY KEY,
            ts       REAL NOT NULL,
            type     TEXT
        )
    """)


def _nc_create_user(username, password, email, display_name, tier="premium"):
    """Create Nextcloud user and add to the appropriate membership group.

    Fail closed (slate C1): any Nextcloud refusal — including the user
    already existing — or any network/auth error raises. Callers must not
    continue past a failed creation.
    """
    nc_group = "Premium Members" if tier == "premium" else "Basic Members"
    nc_base = os.environ.get("NEXTCLOUD_OCS_BASE", "https://nextcloud.example.com/ocs/v2.php/cloud")
    auth_b64 = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    def _post(url, body):
        data = urllib.parse.urlencode(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
                raw = r.read()
        except Exception as e:
            print(f"[stripe] NC POST {url} error: {e}", flush=True)
            raise RuntimeError(f"Nextcloud request failed for '{username}': {e}") from e
        try:
            return json.loads(raw)
        except Exception as e:
            print(f"[stripe] NC POST {url} bad response: {e}", flush=True)
            raise RuntimeError(f"Nextcloud bad response for '{username}': {e}") from e

    # Create user — an already-existing user is a hard stop, not a success.
    created = _post(f"{nc_base}/users", {
        "userid": username, "password": password,
        "email": email, "displayName": display_name,
    })
    created_meta = ((created or {}).get("ocs") or {}).get("meta") or {}
    if created_meta.get("status") != "ok":
        print(f"[stripe] Nextcloud user creation refused for '{username}': "
              f"{created_meta.get('message') or created_meta}", flush=True)
        raise RuntimeError(
            f"Nextcloud user creation failed for '{username}': "
            f"{created_meta.get('message') or created_meta}"
        )
    # Add to group — also fail closed so a half-provisioned user is retried
    # through Stripe rather than silently dropped.
    grouped = _post(f"{nc_base}/users/{username}/groups", {"groupid": nc_group})
    grouped_meta = ((grouped or {}).get("ocs") or {}).get("meta") or {}
    if grouped_meta.get("status") != "ok":
        print(f"[stripe] Nextcloud group add refused for '{username}': "
              f"{grouped_meta.get('message') or grouped_meta}", flush=True)
        raise RuntimeError(
            f"Nextcloud group add failed for '{username}': "
            f"{grouped_meta.get('message') or grouped_meta}"
        )
    print(f"[stripe] Nextcloud user '{username}' created and added to {nc_group}", flush=True)
    _subscribe_news_feed(username)


def _subscribe_news_feed(username):
    """Subscribe a Nextcloud user to the Battle Buddy RSS feed in Nextcloud News."""
    try:
        result = subprocess.run(
            ["sudo", "-u", "www-data", "php", "/var/www/nextcloud/occ",
             "news:feed:add", username, "https://battlebuddy.news/public/feed.rss",
             "--title", "Battle Buddy Intel Feed"],
            capture_output=True, text=True, timeout=30
        )
        print(f"[provision] News feed subscribed for '{username}': "
              f"{result.stdout.strip() or result.stderr.strip()}", flush=True)
    except Exception as e:
        print(f"[provision] News feed subscribe error for '{username}': {e}", flush=True)



def _plant_user_guide(username):
    """Copy Battle Buddy User Guide.md into a new user's NC files and scan it into the DB."""
    NC_DATA   = "/var/www/nextcloud-data"
    GUIDE_SRC = "/var/www/nextcloud/core/skeleton/Battle Buddy User Guide.md"
    dest_dir  = os.path.join(NC_DATA, username, "files")
    dest_file = os.path.join(dest_dir, "Battle Buddy User Guide.md")
    try:
        if not os.path.isdir(dest_dir):
            print(f"[provision] guide: user dir not ready yet for '{username}', skipping filesystem copy", flush=True)
        else:
            shutil.copy2(GUIDE_SRC, dest_file)
            os.chown(dest_file, pwd.getpwnam("www-data").pw_uid, pwd.getpwnam("www-data").pw_gid)
            result = subprocess.run(
                ["sudo", "-u", "www-data", "php", "/var/www/nextcloud/occ",
                 "files:scan", "--path=/" + username + "/files/Battle Buddy User Guide.md"],
                capture_output=True, text=True, timeout=30
            )
            print(f"[provision] guide planted for '{username}': {result.stdout.strip() or 'ok'}", flush=True)
    except Exception as e:
        print(f"[provision] guide plant error for '{username}': {e}", flush=True)

def _add_to_talk_rooms(username):
    """Add Nextcloud user to all 4 Battle Buddy Talk rooms."""
    auth_b64 = base64.b64encode(f"{NC_USER}:{NC_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "OCS-APIREQUEST": "true",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    for room_name, token in TALK_ROOMS.items():
        url = f"{os.environ.get('NEXTCLOUD_SPREED_API_BASE', 'https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v4')}/room/{token}/participants"
        data = urllib.parse.urlencode({"newParticipant": username}).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=8) as r:
                resp = json.loads(r.read())
                print(f"[stripe] Added {username} to Talk room '{room_name}': {resp.get('ocs',{}).get('meta',{}).get('status')}", flush=True)
        except Exception as e:
            print(f"[stripe] Talk room '{room_name}' add error: {e}", flush=True)


def _enroll_subscriptions(username):
    """Add to subscriptions table for DM incident alerts."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (username, beat) VALUES (?, 'all')",
        (username,)
    )
    conn.commit()
    conn.close()
    print(f"[stripe] {username} enrolled in subscriptions (beat=all)", flush=True)


def _send_welcome_email(email, username, setup_token, tier="premium"):
    """Send Mailgun welcome email with onboarding instructions, tier-aware."""
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        return

    atak_step = (
        "=== STEP 7 — ATAK FIELD PACKAGE ===\n"
        "Battle Buddy pushes live incident markers directly to WinTAK, ATAK, and iTAK.\n"
        "Reply to this email to request your ATAK data package and connection instructions.\n\n"
    ) if tier == "premium" else ""

    plain = (
        f"Welcome to Battle Buddy Premium, {username}!\n\n"
        f"=== SET YOUR PASSWORD ===\n"
        f"Use the link below to set your password and access your dashboard.\n"
        f"This link expires in 72 hours.\n"
        f"  https://battlebuddy.news/premium/setup?token={setup_token}\n\n"
        f"Your username is: {username}\n\n"
        f"=== STEP 1 — DOWNLOAD THE APPS ===\n"
        f"Install the Nextcloud app on your phone or tablet to access everything on the go.\n"
        f"  Android: https://play.google.com/store/apps/details?id=com.nextcloud.client\n"
        f"  iPhone/iPad: https://apps.apple.com/us/app/nextcloud/id1125420102\n"
        f"  Desktop (Windows/Mac/Linux): https://nextcloud.com/install/#desktop-clients\n\n"
        f"For Talk (incident alert notifications on your phone):\n"
        f"  Android: https://play.google.com/store/apps/details?id=com.nextcloud.talk2\n"
        f"  iPhone/iPad: https://apps.apple.com/us/app/nextcloud-talk/id1296825574\n\n"
        f"=== STEP 2 — INCIDENT ALERTS (TALK) ===\n"
        f"You have been added to all Battle Buddy alert rooms in Nextcloud Talk.\n"
        f"  Web: {NEXTCLOUD_WEB_BASE}/apps/talk\n"
        f"  Rooms you are in:\n"
        f"    - Incidents  (all detected incidents)\n"
        f"    - APD        (Austin Police press releases and scanner intel)\n"
        f"    - Fire & EMS (AFD, Travis County EMS, STAR Flight)\n"
        f"    - General    (Battle Buddy updates and announcements)\n\n"
        f"Enable push notifications in the Talk app so alerts reach you immediately.\n\n"
        f"=== STEP 3 — INTEL NEWS FEED ===\n"
        f"You are auto-subscribed to the Battle Buddy Intel Feed in Nextcloud News.\n"
        f"  Web: {NEXTCLOUD_WEB_BASE}/apps/news\n"
        f"The feed updates continuously with every confirmed incident, APD press release,\n"
        f"and homicide update — one scrollable stream of verified Austin intelligence.\n\n"
        f"You can also subscribe any RSS reader to the feed directly:\n"
        f"  Feed URL: https://battlebuddy.news/public/feed.rss\n\n"
        f"=== STEP 4 — INTEL QUERY ===\n"
        f"Ask questions about Austin radio traffic in plain English. Battle Buddy searches\n"
        f"the full scanner transcript database and returns an AI-synthesized summary.\n"
        f"  URL: https://battlebuddy.news/premium/\n"
        f"  Quota: 5 queries per month (resets the 1st of each month)\n"
        f"  Example: 'How many shootings near North Lamar this week?'\n\n"
        f"=== STEP 5 — COMMUTE MONITOR ===\n"
        f"Save your commute route and get a Talk alert whenever an incident is detected\n"
        f"near your path — with live travel time vs your normal commute.\n"
        f"  Dashboard:   https://battlebuddy.news/premium/\n"
        f"  Commute Map: https://battlebuddy.news/premium/commute\n\n"
        f"=== STEP 6 — LIVE INTELLIGENCE ===\n"
        f"  Live incident map:  https://battlebuddy.news/public\n"
        f"  Live incident feed: https://battlebuddy.news/public/feed\n"
        f"  2026 Homicide map:  https://battlebuddy.news/public/homicides\n\n"
        + atak_step
        + "=== SUPPORT ===\n"
        + "Reply to this email with any questions. We will get back to you promptly.\n\n"
        + "— Battle Buddy Operations\n"
        + "  https://battlebuddy.news"
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0f;color:#e2e8f0;margin:0;padding:0}}
  .wrap{{max-width:600px;margin:0 auto;padding:32px 20px}}
  .header{{background:#0f1729;border:1px solid #1e3a5f;border-radius:10px;padding:28px 24px;margin-bottom:24px;text-align:center}}
  .header h1{{color:#f8fafc;font-size:1.5rem;margin:0 0 6px}}
  .header p{{color:#64748b;font-size:0.88rem;margin:0}}
  .creds{{background:#0f1729;border:1px solid #f59e0b;border-radius:8px;padding:20px 24px;margin-bottom:24px}}
  .creds h2{{color:#f59e0b;font-size:0.72rem;letter-spacing:2px;text-transform:uppercase;margin:0 0 14px}}
  .cred-row{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #1e293b;font-size:0.88rem}}
  .cred-row:last-child{{border-bottom:none}}
  .cred-label{{color:#64748b}}
  .cred-val{{color:#f8fafc;font-weight:600;font-family:monospace}}
  .section{{background:#0f1729;border:1px solid #1e3a5f;border-radius:8px;padding:20px 24px;margin-bottom:16px}}
  .section-num{{display:inline-block;background:#1e3a5f;color:#60a5fa;font-size:0.7rem;font-weight:700;padding:2px 8px;border-radius:3px;letter-spacing:1px;margin-bottom:10px}}
  .section h2{{color:#f8fafc;font-size:1rem;margin:0 0 10px}}
  .section p{{color:#94a3b8;font-size:0.84rem;line-height:1.6;margin:0 0 10px}}
  .section p:last-child{{margin-bottom:0}}
  .app-links{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}}
  .app-link{{display:inline-block;background:#1e3a5f;color:#60a5fa;text-decoration:none;font-size:0.78rem;padding:6px 14px;border-radius:5px;border:1px solid #2d4a7a}}
  .rooms{{list-style:none;margin:10px 0 0;padding:0}}
  .rooms li{{font-size:0.83rem;color:#94a3b8;padding:5px 0;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:8px}}
  .rooms li:last-child{{border-bottom:none}}
  .dot{{width:6px;height:6px;background:#3b82f6;border-radius:50%;display:inline-block;flex-shrink:0}}
  .feed-url{{background:#060c18;border:1px solid #1e3a5f;border-radius:5px;padding:10px 14px;font-family:monospace;font-size:0.8rem;color:#60a5fa;word-break:break-all;margin-top:10px}}
  .cta{{display:inline-block;background:#f59e0b;color:#0a0a0f;font-weight:700;text-decoration:none;padding:11px 24px;border-radius:6px;font-size:0.9rem;margin-top:12px}}
  .links{{display:flex;flex-direction:column;gap:7px;margin-top:10px}}
  .links a{{color:#60a5fa;font-size:0.84rem;text-decoration:none}}
  .footer{{text-align:center;color:#334155;font-size:0.75rem;margin-top:24px;line-height:1.6}}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <h1>Welcome to Battle Buddy Premium</h1>
    <p>Austin's real-time public safety intelligence platform</p>
  </div>

  <div class="creds">
    <h2>Activate Your Account</h2>
    <p style="color:#94a3b8;font-size:0.9rem;margin:0 0 16px">Click below to set your password and access your dashboard. This link expires in 72 hours.</p>
    <a class="cta" href="https://battlebuddy.news/premium/setup?token={setup_token}">Set Your Password →</a>
    <p style="color:#64748b;font-size:0.78rem;margin:14px 0 0">Your username is: <strong style="color:#cbd5e1">{username}</strong></p>
  </div>

  <div class="section">
    <span class="section-num">STEP 1</span>
    <h2>Download the Apps</h2>
    <p>Install Nextcloud on your phone or tablet for access to Talk alerts, the News feed, files, and calendar — all in one place.</p>
    <p><strong style="color:#cbd5e1">Nextcloud (main app)</strong></p>
    <div class="app-links">
      <a class="app-link" href="https://play.google.com/store/apps/details?id=com.nextcloud.client">Android</a>
      <a class="app-link" href="https://apps.apple.com/us/app/nextcloud/id1125420102">iPhone / iPad</a>
      <a class="app-link" href="https://nextcloud.com/install/#desktop-clients">Desktop (Win/Mac/Linux)</a>
    </div>
    <p style="margin-top:14px"><strong style="color:#cbd5e1">Nextcloud Talk (for push alert notifications)</strong></p>
    <div class="app-links">
      <a class="app-link" href="https://play.google.com/store/apps/details?id=com.nextcloud.talk2">Android</a>
      <a class="app-link" href="https://apps.apple.com/us/app/nextcloud-talk/id1296825574">iPhone / iPad</a>
    </div>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">After installing, tap <em>Add account</em> and enter the server URL above with your username and password.</p>
  </div>

  <div class="section">
    <span class="section-num">STEP 2</span>
    <h2>Incident Alerts — Talk</h2>
    <p>You are enrolled in all four Battle Buddy alert rooms. Incidents are posted the moment they are detected — before any news broadcast exists.</p>
    <ul class="rooms">
      <li><span class="dot"></span><strong style="color:#cbd5e1">Incidents</strong> &nbsp;— all detected incidents across agencies</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">APD</strong> &nbsp;— Austin Police press releases and scanner intel</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">Fire &amp; EMS</strong> &nbsp;— AFD, Travis County EMS, STAR Flight</li>
      <li><span class="dot"></span><strong style="color:#cbd5e1">General</strong> &nbsp;— Battle Buddy updates and announcements</li>
    </ul>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">Enable push notifications in the Talk app so critical incident alerts wake your phone immediately.</p>
    <div class="app-links" style="margin-top:10px">
      <a class="app-link" href="{NEXTCLOUD_WEB_BASE}/apps/talk">Open Talk on Web</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 3</span>
    <h2>Intel News Feed</h2>
    <p>You are auto-subscribed to the <strong style="color:#cbd5e1">Battle Buddy Intel Feed</strong> in Nextcloud News. Open it from the News app on the web or inside the Nextcloud mobile app — it updates continuously with every confirmed incident, APD press release, and homicide update.</p>
    <div class="app-links">
      <a class="app-link" href="{NEXTCLOUD_WEB_BASE}/apps/news">Open News on Web</a>
    </div>
    <p style="margin-top:12px;font-size:0.8rem;color:#64748b">You can also subscribe any RSS reader (Feedly, Reeder, etc.) to the feed directly:</p>
    <div class="feed-url">https://battlebuddy.news/public/feed.rss</div>
  </div>

  <div class="section">
    <span class="section-num">STEP 4</span>
    <h2>Intel Query — AI Search</h2>
    <p>Ask any question about Austin radio traffic in plain English. Battle Buddy searches the full scanner transcript and press release database and returns an AI-written intelligence summary.</p>
    <p style="font-size:0.8rem;color:#64748b"><em>Examples: "How many shootings near North Lamar this week?" &nbsp;·&nbsp; "Any AFD structure fires in East Austin this month?"</em></p>
    <p style="font-size:0.8rem;color:#64748b">Quota: <strong style="color:#cbd5e1">5 queries per month</strong>, resets on the 1st.</p>
    <div class="app-links">
      <a class="app-link" href="https://battlebuddy.news/premium/">Open Intel Query</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 5</span>
    <h2>🚗 Commute Monitor</h2>
    <p>Save your daily commute route and Battle Buddy will alert you via Talk whenever an active incident is detected near your path — with live travel time vs your normal commute.</p>
    <p style="font-size:0.8rem;color:#64748b">Open your premium dashboard, find the Commute Monitor card, and enter your origin and destination (include city and state — e.g. <em>Slaughter Ln &amp; I-35, Austin TX</em>).</p>
    <div class="app-links">
      <a class="app-link" href="https://battlebuddy.news/premium/commute">Open Commute Map</a>
      <a class="app-link" href="https://battlebuddy.news/premium/">Premium Dashboard</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 6</span>
    <h2>Live Intelligence — Web</h2>
    <p>The public intelligence portal is available to you at any time from any browser — no login required.</p>
    <div class="links">
      <a href="https://battlebuddy.news/public">battlebuddy.news/public &nbsp;— Live incident map</a>
      <a href="https://battlebuddy.news/public/feed">battlebuddy.news/public/feed &nbsp;— Live incident feed</a>
      <a href="https://battlebuddy.news/public/homicides">battlebuddy.news/public/homicides &nbsp;— 2026 Austin Homicide Map</a>
    </div>
  </div>

  <div class="section">
    <span class="section-num">STEP 7</span>
    <h2>ATAK Field Package</h2>
    <p>Battle Buddy pushes live incident markers directly to WinTAK, ATAK (Android), and iTAK as Cursor-on-Target (CoT) markers — appearing on your tactical map the moment an incident is confirmed.</p>
    <p>Reply to this email to request your ATAK data package (.zip with certs and connection profile) and setup instructions for your device.</p>
  </div>

  <div class="footer">
    <p>Questions? Reply to this email — we respond promptly.</p>
    <p style="margin-top:6px"><a href="https://battlebuddy.news" style="color:#3b82f6">battlebuddy.news</a> &nbsp;·&nbsp; Battle Buddy Operations</p>
  </div>

</div>
</body>
</html>"""

    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    auth_b64 = base64.b64encode(f"api:{MAILGUN_API_KEY}".encode()).decode()
    data = urllib.parse.urlencode({
        "from": MAILGUN_FROM,
        "to": email,
        "subject": "Welcome to Battle Buddy Premium — Getting Started",
        "text": plain,
        "html": html,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Basic {auth_b64}",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[stripe] Welcome email sent to {email}: {r.status}", flush=True)
    except Exception as e:
        print(f"[stripe] Welcome email error: {e}", flush=True)


def _provision_premium_user(session_obj, event_id=None):
    """Full provisioning flow after successful Stripe checkout.

    Slate C1: the server-side ``premium_checkout_intents`` row keyed by the
    opaque ``intent_id`` in Stripe metadata is the ONLY authority for the
    username — any username arriving in Stripe metadata is ignored. Never
    replaces an existing account, never issues a token for one, and consumes
    the intent in the SAME transaction as the ``premium_users`` insert (which
    also records the Stripe event id) so a crash or replay cannot double
    provision. Raises on any failure so the webhook fails the request and
    Stripe retries instead of silently dropping a paying customer. This is the
    code the live webhook in modules/stripe_billing.py actually calls.
    """
    customer_email = (session_obj.get("customer_details") or {}).get("email") or                      session_obj.get("customer_email") or ""
    customer_id    = session_obj.get("customer", "")
    sub_id         = session_obj.get("subscription", "")
    metadata       = session_obj.get("metadata") or {}

    intent_id = (metadata.get("intent_id") or "").strip()
    if not intent_id:
        print("[stripe] provision stopped — missing checkout intent", flush=True)
        raise ValueError("missing checkout intent")

    # 0. Resolve the intent; fail closed on missing/expired/consumed, and on
    #    replays of an already-processed Stripe event. Never fall back to a
    #    username from Stripe metadata.
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_intent_tables(conn)
        if event_id:
            already = conn.execute(
                "SELECT 1 FROM stripe_processed_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if already:
                print(f"[stripe] replay ignored for event {event_id}", flush=True)
                return
        row = conn.execute(
            "SELECT username, display_name, nc_password, tier, plan, "
            "created_ts, expires_ts, consumed_ts "
            "FROM premium_checkout_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            print("[stripe] provision stopped — unknown checkout intent", flush=True)
            raise ValueError("unknown checkout intent")
        intent_username, display_name, nc_password, tier, _plan, _cts, expires_ts, consumed_ts = row
        now = time.time()
        if consumed_ts is not None:
            print("[stripe] provision stopped — checkout intent already consumed", flush=True)
            raise ValueError("checkout intent already consumed")
        if not expires_ts or expires_ts < now:
            print("[stripe] provision stopped — checkout intent expired", flush=True)
            raise ValueError("checkout intent expired")
        username = (intent_username or "").strip().lower()
        display_name = display_name or username
        tier = tier or "premium"
        if not username or not customer_email:
            print("[stripe] provision stopped — missing username or email in session", flush=True)
            raise ValueError("missing username or email")
        existing = conn.execute(
            "SELECT 1 FROM premium_users WHERE lower(username)=lower(?)",
            (username,),
        ).fetchone()
        if existing:
            print(f"[stripe] provision stopped — account '{username}' already exists", flush=True)
            raise ValueError("account already exists")
    finally:
        conn.close()

    # 1. Nextcloud user — raises on any failure; nothing below runs then.
    _nc_create_user(username, nc_password, customer_email, display_name, tier)

    # 2. Talk rooms
    _add_to_talk_rooms(username)

    # 3. Subscriptions
    _enroll_subscriptions(username)

    # 3b. Plant user guide in NC files
    _plant_user_guide(username)

    # 4. premium_users table + intent consumption + idempotency record, in ONE
    #    transaction. Plain INSERT (never OR REPLACE) so an existing row can
    #    never be overwritten; a uniqueness violation aborts with no token.
    setup_token   = _secrets.token_urlsafe(32)
    setup_expires = int(time.time()) + 72 * 3600  # 72-hour window
    session_key   = session_obj.get("id", "")
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_intent_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        if event_id:
            already = conn.execute(
                "SELECT 1 FROM stripe_processed_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if already:
                conn.rollback()
                print(f"[stripe] replay ignored for event {event_id}", flush=True)
                return
        live = conn.execute(
            "SELECT consumed_ts, expires_ts FROM premium_checkout_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if live is None or live[0] is not None or not live[1] or live[1] < time.time():
            conn.rollback()
            print("[stripe] provision stopped — checkout intent no longer live", flush=True)
            raise ValueError("checkout intent no longer live")
        if conn.execute(
            "SELECT 1 FROM premium_users WHERE lower(username)=lower(?)",
            (username,),
        ).fetchone():
            conn.rollback()
            print(f"[stripe] provision stopped — account '{username}' already exists", flush=True)
            raise ValueError("account already exists")
        try:
            conn.execute(
                "INSERT INTO premium_users "
                "(username, email, stripe_customer_id, stripe_subscription_id, status, created_ts, setup_token, setup_token_expires) "
                "VALUES (?,?,?,?,'active',?,?,?)",
                (username, customer_email, customer_id, sub_id, time.time(), setup_token, setup_expires)
            )
        except sqlite3.IntegrityError as e:
            conn.rollback()
            print(f"[stripe] provision stopped — account '{username}' already exists", flush=True)
            raise ValueError("account already exists") from e
        cur = conn.execute(
            "UPDATE premium_checkout_intents SET consumed_ts=?, stripe_session_id=? "
            "WHERE intent_id=? AND consumed_ts IS NULL",
            (time.time(), session_key, intent_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            print("[stripe] provision stopped — checkout intent no longer live", flush=True)
            raise ValueError("checkout intent no longer live")
        if event_id:
            conn.execute(
                "INSERT INTO stripe_processed_events (event_id, ts, type) VALUES (?,?,?)",
                (event_id, time.time(), "checkout.session.completed"),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    print(f"[stripe] premium_users record created for '{username}'", flush=True)

    # 5. Welcome email — only after the account row is durably committed.
    _send_welcome_email(customer_email, username, setup_token, tier)


def _get_session_by_token(token):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT username, expires_ts, is_admin, is_premium FROM sessions WHERE token=?",
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    username, expires_ts, is_admin, is_premium = row
    return {"username": username, "is_admin": bool(is_admin), "is_premium": bool(is_premium)}
