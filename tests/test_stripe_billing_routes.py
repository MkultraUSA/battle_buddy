from unittest.mock import patch

from flask import Flask

from modules import stripe_billing


def _make_app():
    app = Flask(__name__)
    app.register_blueprint(stripe_billing.stripe_bp)
    return app


def test_checkout_rejects_invalid_plan():
    app = _make_app()
    with app.test_client() as client:
        with patch.object(stripe_billing, "STRIPE_SECRET_KEY", "sk_test_mock"):
            resp = client.post(
                "/api/stripe/create_checkout",
                json={"username": "demo", "display_name": "Demo", "plan": "bad_plan"},
            )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid plan"


def test_checkout_returns_url_when_stripe_succeeds():
    app = _make_app()
    with app.test_client() as client:
        with patch.object(stripe_billing, "STRIPE_SECRET_KEY", "sk_test_mock"):
            with patch.object(
                stripe_billing._stripe.checkout.Session,
                "create",
                return_value=type("S", (), {"url": "https://checkout.stripe.test/session"})(),
            ):
                resp = client.post(
                    "/api/stripe/create_checkout",
                    json={"username": "demo", "display_name": "Demo", "plan": "premium_monthly"},
                )
    assert resp.status_code == 200
    assert resp.get_json()["checkout_url"] == "https://checkout.stripe.test/session"


def test_webhook_is_exposed_at_legacy_and_current_paths():
    app = _make_app()
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/stripe/webhook" in routes
    assert "/api/stripe/webhook" in routes

