"""Battle Buddy Stripe billing — checkout, webhook, plan definitions.

Extracted from audio_receiver.py. Mounts as a Flask Blueprint named stripe_bp.
"""

import json
import os
import secrets as _secrets
import sqlite3
import threading

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

@stripe_bp.route("/api/stripe/create_checkout", methods=["POST"])
def api_stripe_create_checkout():
    """Create a Stripe Checkout Session. Client sends username, display_name, plan."""
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
    nc_password = _secrets.token_urlsafe(12)

    try:
        session = _stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": plan_info["price_id"], "quantity": 1}],
            subscription_data={"trial_period_days": 7},
            success_url="https://battlebuddy.news/premium/welcome?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://battlebuddy.news/premium/",
            metadata={
                "username": username,
                "display_name": display_name,
                "nc_password": nc_password,
                "tier": plan_info["tier"],
            },
        )
        return jsonify({"checkout_url": session.url})
    except Exception as e:
        print(f"[stripe] create_checkout error: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


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
    print(f"[stripe] webhook received: {event_type}", flush=True)

    if event_type == "checkout.session.completed":
        # Parse session from raw payload — avoids SDK v15 StripeObject attribute issues
        session_data = json.loads(payload)["data"]["object"]
        threading.Thread(
            target=_provision_premium_user,
            args=(session_data,),
            daemon=True
        ).start()

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
