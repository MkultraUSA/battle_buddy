"""Regression guard for the removal of the dead premium_bp route handlers.

Background: modules/premium.py declared a Flask blueprint `premium_bp` that was
NEVER registered in audio_receiver.py or app.py, so every route on it was
unreachable dead code. Worse, it contained pre-C1 copies of the Stripe checkout
handler (reading `username` from the unauthenticated request body into Stripe
metadata) plus a duplicate declaration and an unrouted webhook. Registering
that blueprint — or editing those copies believing they were live — would
bypass the C1 account-takeover fix in modules/stripe_billing.py.

These tests pin the safe end-state:
  1. every name other modules import from modules.premium still resolves;
  2. modules/premium.py declares NO routes (in particular no
     /api/stripe/create_checkout or /api/stripe/webhook copies);
  3. the LIVE stripe_billing.py path is untouched (exactly one checkout route,
     one webhook route, still on the one-time-intent flow).

Offline: stubs the `stripe` package (NOT installed here), never reads .env,
never touches the network.

Suite hygiene: this module NEVER overwrites sys.modules["stripe"] — it reuses
the stub left by earlier-imported test modules (or installs a full-surface one
only if none exists), so the C1 tests' monkeypatch targets keep working. It
also never imports audio_receiver or stripe_billing at collection time.
"""

import ast
import pathlib
import subprocess
import sys
import types

_REPO_ROOT = pathlib.Path(__file__).parent.parent

# --- stripe stub (full surface the live code touches) -----------------------
# `stripe` is NOT installed here. Reuse whatever stub is already installed so
# we never rebind another test module's monkeypatch target; only install our
# own if nothing is there yet (modules.premium does `import stripe` at top).
_existing_stripe = sys.modules.get("stripe")
if _existing_stripe is None:
    _existing_stripe = types.ModuleType("stripe")
    _existing_stripe.api_key = None

    class _SignatureVerificationError(Exception):
        pass

    _existing_stripe.error = types.SimpleNamespace(
        SignatureVerificationError=_SignatureVerificationError
    )
    _existing_stripe.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(create=None)
    )
    _existing_stripe.Webhook = types.SimpleNamespace(construct_event=None)
    sys.modules["stripe"] = _existing_stripe

# Collection-order hygiene (same as tests/test_stripe_c1_account_takeover.py):
# tests/test_pi_watchdog.py may install a file-less modules.config stub.
_config_mod = sys.modules.get("modules.config")
if _config_mod is not None and getattr(_config_mod, "__file__", None) is None:
    del sys.modules["modules.config"]

from modules import premium as premium_mod  # noqa: E402

# Live import surface, captured BEFORE the dead-route removal (base 07f6bf4):
#  - audio_receiver.py `from modules.premium import (...)` block:
#      _get_session, _get_session_by_token, _is_admin, _issue_session,
#      _nc_validate_user, _require_premium
#  - modules/stripe_billing.py deferred import inside stripe_webhook():
#      _provision_premium_user
#  - modules/tips.py `from modules import premium as premium_mod` uses:
#      premium_mod._nc_validate_user, premium_mod._is_admin
# Plus the remaining live helpers those callers (and the live webhook) need.
STRIPE_BILLING_IMPORTS = ("_provision_premium_user",)
OTHER_LIVE_HELPERS = (
    "_ensure_intent_tables",
    "_nc_create_user",
    "_is_premium",
    "_require_admin",
    "_send_welcome_email",
    "_add_to_talk_rooms",
    "_enroll_subscriptions",
    "_plant_user_guide",
    "_subscribe_news_feed",
)


def _premium_source():
    return pathlib.Path(premium_mod.__file__).read_text()


def _names_imported_from_premium(path):
    """Exact names a source file imports from modules.premium (AST)."""
    tree = ast.parse(pathlib.Path(path).read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "modules.premium":
            names.extend(a.asname or a.name for a in node.names)
    return names


def _billing_mod():
    from modules import stripe_billing as billing_mod  # noqa: E402

    return billing_mod


def test_live_premium_imports_still_resolve():
    """Every name other modules import from modules.premium must exist."""
    audio_names = _names_imported_from_premium(_REPO_ROOT / "audio_receiver.py")
    assert len(audio_names) == 6, (
        f"expected 6 names in audio_receiver's premium import block, got "
        f"{audio_names}"
    )
    for name in (
        tuple(audio_names) + STRIPE_BILLING_IMPORTS + OTHER_LIVE_HELPERS
    ):
        assert hasattr(premium_mod, name), f"modules.premium.{name} is missing"
        assert callable(getattr(premium_mod, name)), (
            f"modules.premium.{name} is not callable"
        )


def test_audio_receiver_still_imports():
    """audio_receiver (the live importer) must still import cleanly.

    In-process import is impractical in this suite: tests/test_pi_watchdog.py
    installs a file-less modules.config stub and several test modules install
    competing `stripe` stubs, so an in-process import is order-dependent (the
    existing audio_receiver tests all import it in subprocess children for the
    same reason). Proved instead in a clean subprocess interpreter.
    """
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from unittest import mock; "
            "sys.modules['stripe'] = mock.MagicMock(); "
            "import audio_receiver; print('audio_receiver import OK')",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
    )
    assert r.returncode == 0, f"audio_receiver import failed: {r.stderr[-2000:]}"
    assert "audio_receiver import OK" in r.stdout


def test_no_dead_stripe_routes_in_premium():
    """The unrouted pre-C1 checkout/webhook copies must never come back."""
    src = _premium_source()
    assert "/api/stripe/create_checkout" not in src, (
        "dead /api/stripe/create_checkout declaration still present in "
        "modules/premium.py"
    )
    assert "/api/stripe/webhook" not in src, (
        "dead /api/stripe/webhook declaration still present in "
        "modules/premium.py"
    )


def test_no_premium_bp_routes_at_all():
    """The unregistered premium blueprint must be gone entirely."""
    src = _premium_source()
    assert "premium_bp" not in src, (
        "premium_bp still referenced in modules/premium.py"
    )
    assert "@premium_bp.route" not in src


def test_live_stripe_billing_routes_untouched():
    """The registered live path must still declare exactly its two routes."""
    from flask import Flask

    billing_mod = _billing_mod()
    src = pathlib.Path(billing_mod.__file__).read_text()
    assert src.count('@stripe_bp.route("/api/stripe/create_checkout"') == 1
    assert src.count('@stripe_bp.route("/stripe/webhook"') == 1

    app = Flask(__name__)
    app.register_blueprint(billing_mod.stripe_bp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/stripe/create_checkout" in rules
    assert "/stripe/webhook" in rules


def test_live_stripe_billing_still_on_intent_flow():
    """The live checkout must still route through one-time checkout intents."""
    src = pathlib.Path(_billing_mod().__file__).read_text()
    assert "premium_checkout_intents" in src
    assert '"intent_id": intent_id' in src, (
        "live checkout metadata must carry only the opaque intent_id"
    )
    # The dangerous pre-C1 pattern (raw username into Stripe metadata) must
    # not exist on the live path.
    assert '"username": username' not in src
    assert "_provision_premium_user" in src
