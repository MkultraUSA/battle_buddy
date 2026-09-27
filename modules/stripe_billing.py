"""Battle Buddy Stripe billing — checkout, webhook, plan definitions.

Extracted from audio_receiver.py. Mounts as a Flask Blueprint named stripe_bp.
"""

import json
import os
import secrets as _secrets
import sqlite3
import time

import stripe as _stripe
from flask import Blueprint, jsonify, request

from modules.config import DB_PATH

stripe_bp = Blueprint("stripe_billing", __name__)

# ---------------------------------------------------------------------------
# Stripe configuration
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")  # legacy

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
# Checkout endpoint
# ---------------------------------------------------------------------------

# One-time checkout intents live 24h; the intent row is the only authority
# for the username a checkout may provision (slate C1).
CHECKOUT_INTENT_TTL_S = 24 * 3600


def _ensure_billing_tables(conn):
    """Create the C1 billing tables if missing (idempotent, same DDL as
    schema.sql / modules/database.py init_db())."""
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


# ---------------------------------------------------------------------------
# Checkout endpoint
# ---------------------------------------------------------------------------

@stripe_bp.route("/api/stripe/create_checkout", methods=["POST"])
def api_stripe_create_checkout():
    """Create a Stripe Checkout Session. Client sends username, display_name, plan.

    Slate C1: the username is bound server-side in a one-time
    premium_checkout_intents row. Stripe metadata carries ONLY the opaque
    intent_id — never the raw username nor the generated Nextcloud password.
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "payments not configured"}), 503
    data = request.get_json(silent=True) or {}
    username     = (data.get("username") or "").strip().lower()
    display_name = (data.get("display_name") or username).strip()
    plan         = (data.get("plan") or "premium_monthly").strip()
    if not username:
        return jsonify({"error": "username required"}), 400
    if plan not in STRIPE_PLANS:
        return jsonify({"error": "invalid plan"}), 400

    plan_info   = STRIPE_PLANS[plan]
    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_billing_tables(conn)
        existing = conn.execute(
            "SELECT 1 FROM premium_users WHERE lower(username)=lower(?)",
            (username,),
        ).fetchone()
        if existing:
            print(f"[stripe] checkout refused — account '{username}' already exists", flush=True)
            return jsonify({"error": "username taken"}), 409
        live = conn.execute(
            "SELECT 1 FROM premium_checkout_intents "
            "WHERE lower(username)=lower(?) AND consumed_ts IS NULL AND expires_ts > ?",
            (username, now),
        ).fetchone()
        if live:
            print(f"[stripe] checkout refused — pending intent for '{username}'", flush=True)
            return jsonify({"error": "checkout already pending for this username"}), 409
        intent_id   = _secrets.token_urlsafe(32)
        nc_password = _secrets.token_urlsafe(12)
        conn.execute(
            "INSERT INTO premium_checkout_intents "
            "(intent_id, username, display_name, nc_password, tier, plan, "
            "created_ts, expires_ts, consumed_ts, stripe_session_id) "
            "VALUES (?,?,?,?,?,?,?, ?,NULL,NULL)",
            (intent_id, username, display_name, nc_password,
             plan_info["tier"], plan, now, now + CHECKOUT_INTENT_TTL_S),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        session = _stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan_info["price_id"], "quantity": 1}],
            subscription_data={"trial_period_days": 7},
            success_url="https://battlebuddy.news/premium/welcome?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://battlebuddy.news/premium/",
            metadata={
                "intent_id": intent_id,
            },
        )
    except Exception as e:
        print(f"[stripe] create_checkout error: {e}", flush=True)
        # Best-effort cleanup so the customer can retry without tripping the
        # duplicate-intent guard on a checkout that never reached Stripe.
        try:
            cleanup = sqlite3.connect(DB_PATH)
            cleanup.execute(
                "DELETE FROM premium_checkout_intents WHERE intent_id=? AND consumed_ts IS NULL",
                (intent_id,),
            )
            cleanup.commit()
            cleanup.close()
        except Exception:
            pass
        return jsonify({"error": str(e)}), 500
    try:
        link = sqlite3.connect(DB_PATH)
        link.execute(
            "UPDATE premium_checkout_intents SET stripe_session_id=? WHERE intent_id=?",
            (getattr(session, "id", "") or "", intent_id),
        )
        link.commit()
        link.close()
    except Exception:
        pass
    return jsonify({"checkout_url": session.url})


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@stripe_bp.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe sends signed events here. Verify signature, then provision."""
    from modules.premium import _provision_premium_user  # deferred to avoid startup circularity

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = _stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except _stripe.error.SignatureVerificationError as e:
        print(f"[stripe] webhook signature invalid: {e}", flush=True)
        return jsonify({"error": "invalid signature"}), 400
    except Exception as e:
        print(f"[stripe] webhook parse error: {e}", flush=True)
        return jsonify({"error": "bad payload"}), 400

    event_type = event["type"]
    event_id = event.get("id", "")
    print(f"[stripe] webhook received: {event_type}", flush=True)

    if event_type == "checkout.session.completed":
        # Parse session from raw payload — avoids SDK v15 StripeObject attribute issues
        session_data = json.loads(payload)["data"]["object"]
        # Durable idempotency pre-check: a replay of an already-processed
        # event is a complete no-op (the authoritative guard is the committed
        # stripe_processed_events write inside provisioning).
        if event_id:
            conn = sqlite3.connect(DB_PATH)
            try:
                _ensure_billing_tables(conn)
                if conn.execute(
                    "SELECT 1 FROM stripe_processed_events WHERE event_id=?",
                    (event_id,),
                ).fetchone():
                    conn.close()
                    print(f"[stripe] replay ignored for event {event_id}", flush=True)
                    return jsonify({"status": "ok"})
            finally:
                conn.close()
        # Process synchronously and report the real outcome: success only
        # after provisioning is durably committed; failure (non-2xx) so
        # Stripe retries rather than silently dropping a paying customer.
        try:
            _provision_premium_user(session_data, event_id or None)
        except ValueError as e:
            print(f"[stripe] provision rejected: {e}", flush=True)
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            print(f"[stripe] provision failed: {e}", flush=True)
            return jsonify({"error": "provisioning failed"}), 500

    elif event_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer", "")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE premium_users SET status='cancelled' WHERE stripe_customer_id=?",
            (customer_id,)
        )
        conn.commit()
        conn.close()
        print(f"[stripe] subscription cancelled for customer {customer_id}", flush=True)

    return jsonify({"status": "ok"})
