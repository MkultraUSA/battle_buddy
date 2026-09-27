"""Regression tests for slate item C1: Stripe anonymous-checkout account takeover.

Live path under test is modules/stripe_billing.py (checkout + webhook) together
with modules/premium.py _provision_premium_user / _nc_create_user, which the
webhook actually calls. The dead premium_bp checkout duplicates are untouched.

Offline: temporary SQLite file, mocked Stripe SDK and mocked Nextcloud. Never
reads .env and never touches the network.
"""

import json
import sqlite3
import sys
import time
import types

import pytest
from flask import Flask

# --- stripe stub (full surface the live code touches) -----------------------
# `stripe` is NOT installed here. Install a stub before importing the modules
# under test. Overwrite (not setdefault): other test files install a bare
# SimpleNamespace(api_key=None) which lacks checkout/Webhook/error.
_stripe_stub = types.ModuleType("stripe")
_stripe_stub.api_key = None


class _SignatureVerificationError(Exception):
    pass


_stripe_stub.error = types.SimpleNamespace(
    SignatureVerificationError=_SignatureVerificationError
)
_stripe_stub.checkout = types.SimpleNamespace(
    Session=types.SimpleNamespace(create=None)
)
_stripe_stub.Webhook = types.SimpleNamespace(construct_event=None)
sys.modules["stripe"] = _stripe_stub

# Collection-order hygiene (same as tests/test_tips_c2_authz_xss.py):
# tests/test_pi_watchdog.py may install a file-less modules.config stub.
_config_mod = sys.modules.get("modules.config")
if _config_mod is not None and getattr(_config_mod, "__file__", None) is None:
    del sys.modules["modules.config"]

from modules import premium as premium_mod  # noqa: E402
from modules import stripe_billing as billing_mod  # noqa: E402

INTENT_TTL = 24 * 3600


def _create_tables(db):
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS premium_users ("
        "username TEXT PRIMARY KEY, email TEXT, stripe_customer_id TEXT, "
        "stripe_subscription_id TEXT, status TEXT DEFAULT 'active', "
        "created_ts REAL NOT NULL, intel_queries_used INTEGER DEFAULT 0, "
        "intel_quota INTEGER DEFAULT 5, setup_token TEXT, "
        "setup_token_expires INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS subscriptions ("
        "username TEXT NOT NULL, beat TEXT NOT NULL DEFAULT 'all', "
        "PRIMARY KEY (username, beat))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS premium_checkout_intents ("
        "intent_id TEXT PRIMARY KEY, username TEXT NOT NULL, "
        "display_name TEXT, nc_password TEXT NOT NULL, tier TEXT, plan TEXT, "
        "created_ts REAL NOT NULL, expires_ts REAL NOT NULL, "
        "consumed_ts REAL, stripe_session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stripe_processed_events ("
        "event_id TEXT PRIMARY KEY, ts REAL NOT NULL, type TEXT)"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def c1_db(tmp_path, monkeypatch):
    db = str(tmp_path / "c1_test.db")
    _create_tables(db)
    monkeypatch.setattr(premium_mod, "DB_PATH", db)
    monkeypatch.setattr(billing_mod, "DB_PATH", db)
    monkeypatch.setattr(billing_mod, "STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(premium_mod, "STRIPE_SECRET_KEY", "sk_test_dummy")
    # Offline: no Talk/News/guide side effects.
    monkeypatch.setattr(premium_mod, "_add_to_talk_rooms", lambda *a: None)
    monkeypatch.setattr(premium_mod, "_plant_user_guide", lambda *a: None)
    monkeypatch.setattr(premium_mod, "_subscribe_news_feed", lambda *a: None)
    return db


@pytest.fixture()
def billing_client():
    app = Flask(__name__)
    app.register_blueprint(billing_mod.stripe_bp)
    app.testing = True
    return app.test_client()


def _insert_victim(db, username="kevin"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO premium_users (username, email, stripe_customer_id, "
        "stripe_subscription_id, status, created_ts, setup_token, "
        "setup_token_expires) VALUES (?,?,?,?,?,?,?,?)",
        (username, "victim@example.com", "cus_victim", "sub_victim",
         "active", 1700000000.0, "ORIGINAL_TOKEN", 1800000000),
    )
    conn.commit()
    conn.close()


def _get_premium_row(db, username):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT username, email, stripe_customer_id, stripe_subscription_id, "
        "status, created_ts, setup_token, setup_token_expires "
        "FROM premium_users WHERE username=?", (username,),
    ).fetchone()
    conn.close()
    return row


def _insert_intent(db, intent_id, username, password="NC_SECRET_PW",
                   tier="premium", expired=False, consumed=False):
    now = time.time()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO premium_checkout_intents (intent_id, username, "
        "display_name, nc_password, tier, plan, created_ts, expires_ts, "
        "consumed_ts, stripe_session_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (intent_id, username, username, password, tier, "premium_monthly",
         now, now - 10 if expired else now + INTENT_TTL,
         now if consumed else None, "cs_test_123"),
    )
    conn.commit()
    conn.close()


def _session_for(intent_id, username, email="attacker@example.com"):
    return {
        "customer_details": {"email": email},
        "customer": "cus_attacker",
        "subscription": "sub_attacker",
        "id": "cs_test_123",
        "metadata": {"intent_id": intent_id, "username": username},
    }


def _mock_stripe_create(monkeypatch):
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(url="https://checkout.stripe.com/pay/cs_test")

    monkeypatch.setattr(_stripe_stub.checkout.Session, "create", fake_create)
    return calls


def _mock_nc_success(monkeypatch):
    monkeypatch.setattr(premium_mod, "_nc_create_user",
                        lambda *a, **k: None)


def _mock_email(monkeypatch):
    sent = []
    monkeypatch.setattr(premium_mod, "_send_welcome_email",
                        lambda e, u, t, tier="premium": sent.append((e, u, t)))
    return sent


# --- 1. checkout for an existing username is rejected ------------------------


def test_checkout_existing_username_rejected(c1_db, billing_client, monkeypatch):
    _insert_victim(c1_db, "kevin")
    calls = _mock_stripe_create(monkeypatch)
    r = billing_client.post("/api/stripe/create_checkout",
                            json={"username": "kevin", "plan": "premium_monthly"})
    assert r.status_code != 200, "checkout for an existing account must be rejected"
    assert calls == [], "no Stripe session may be created for an existing username"


def test_checkout_existing_username_case_insensitive(c1_db, billing_client, monkeypatch):
    _insert_victim(c1_db, "kevin")
    calls = _mock_stripe_create(monkeypatch)
    r = billing_client.post("/api/stripe/create_checkout",
                            json={"username": "KEVIN", "plan": "premium_monthly"})
    assert r.status_code != 200
    assert calls == []


def test_checkout_duplicate_live_intent_rejected(c1_db, billing_client, monkeypatch):
    _mock_stripe_create(monkeypatch)
    r1 = billing_client.post("/api/stripe/create_checkout",
                             json={"username": "newbie", "plan": "premium_monthly"})
    assert r1.status_code == 200
    calls2 = _mock_stripe_create(monkeypatch)
    r2 = billing_client.post("/api/stripe/create_checkout",
                             json={"username": "newbie", "plan": "premium_monthly"})
    assert r2.status_code != 200, "a second live intent for the same username must be rejected"
    assert calls2 == []


# --- 2. checkout carries only an opaque intent id ----------------------------


def test_checkout_metadata_has_intent_only(c1_db, billing_client, monkeypatch):
    calls = _mock_stripe_create(monkeypatch)
    r = billing_client.post("/api/stripe/create_checkout",
                            json={"username": "freshuser", "plan": "premium_monthly"})
    assert r.status_code == 200
    assert len(calls) == 1
    metadata = calls[0].get("metadata") or {}
    assert "intent_id" in metadata, "Stripe metadata must carry the opaque intent id"
    blob = json.dumps(metadata)
    assert "freshuser" not in blob, "raw username must not transit Stripe metadata"
    for value in metadata.values():
        assert "freshuser" not in str(value)
    conn = sqlite3.connect(c1_db)
    row = conn.execute(
        "SELECT username, nc_password FROM premium_checkout_intents WHERE intent_id=?",
        (metadata["intent_id"],),
    ).fetchone()
    conn.close()
    assert row is not None, "intent row must exist server-side"
    assert row[0] == "freshuser"
    nc_password = row[1]
    assert nc_password
    for value in metadata.values():
        assert nc_password not in str(value), \
            "generated Nextcloud password must not transit Stripe"


# --- 3. provisioning an existing account changes nothing ---------------------


def test_provision_existing_leaves_row_unchanged(c1_db, monkeypatch):
    _insert_victim(c1_db, "kevin")
    before = _get_premium_row(c1_db, "kevin")
    _insert_intent(c1_db, "intent_takeover", "kevin")
    _mock_nc_success(monkeypatch)
    sent = _mock_email(monkeypatch)
    session = _session_for("intent_takeover", "kevin")
    try:
        premium_mod._provision_premium_user(session)
    except Exception:
        pass
    after = _get_premium_row(c1_db, "kevin")
    assert after == before, "existing premium_users row must be byte-for-byte unchanged"
    assert after[6] == "ORIGINAL_TOKEN", "setup_token must not rotate"
    assert sent == [], "no welcome email for an existing account"


# --- 4. Nextcloud failure raises; no row, no token, no email -----------------


def test_nc_create_failure_raises():
    import urllib.request
    real_urlopen = urllib.request.urlopen

    def boom(*a, **k):
        raise ConnectionError("nc down")

    urllib.request.urlopen = boom
    try:
        with pytest.raises(Exception):
            premium_mod._nc_create_user("someuser", "pw", "e@x.com", "Some")
    finally:
        urllib.request.urlopen = real_urlopen


def test_provision_nc_failure_creates_nothing(c1_db, monkeypatch):
    import urllib.request
    _insert_intent(c1_db, "intent_ncfail", "ncnewbie")
    sent = _mock_email(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("nc down")))
    session = _session_for("intent_ncfail", "ncnewbie")
    with pytest.raises(Exception):
        premium_mod._provision_premium_user(session)
    assert _get_premium_row(c1_db, "ncnewbie") is None
    assert sent == []


# --- 5. replaying the same Stripe event id is a no-op ------------------------


def _wait_for(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def test_replay_same_event_id_is_noop(c1_db, billing_client, monkeypatch):
    _insert_intent(c1_db, "intent_replay", "replayuser")
    _mock_nc_success(monkeypatch)
    sent = []
    orig_send = premium_mod._send_welcome_email
    monkeypatch.setattr(premium_mod, "_send_welcome_email",
                        lambda e, u, t, tier="premium": sent.append((e, u, t)))
    assert orig_send is not None
    session = _session_for("intent_replay", "replayuser")
    payload = json.dumps({"id": "evt_replay_1",
                          "type": "checkout.session.completed",
                          "data": {"object": session}}).encode()
    monkeypatch.setattr(_stripe_stub.Webhook, "construct_event",
                        lambda p, s, w: {"id": "evt_replay_1",
                                         "type": "checkout.session.completed"})
    headers = {"Stripe-Signature": "t=1,v1=abc"}
    r1 = billing_client.post("/stripe/webhook", data=payload, headers=headers,
                             content_type="application/json")
    assert r1.status_code == 200
    assert _wait_for(
        lambda: _get_premium_row(c1_db, "replayuser") is not None), \
        "first delivery must provision the account"
    first = _get_premium_row(c1_db, "replayuser")
    assert _wait_for(lambda: len(sent) == 1), "first delivery must send one email"
    r2 = billing_client.post("/stripe/webhook", data=payload, headers=headers,
                             content_type="application/json")
    assert r2.status_code == 200
    time.sleep(0.5)
    second = _get_premium_row(c1_db, "replayuser")
    assert second == first, "replay must not rotate setup_token or touch the row"
    assert len(sent) == 1, "replay must not send a second email"
    conn = sqlite3.connect(c1_db)
    n = conn.execute("SELECT COUNT(*) FROM premium_users WHERE username=?",
                     ("replayuser",)).fetchone()[0]
    conn.close()
    assert n == 1


# --- 6. swapped Stripe metadata never takes over (most important) ------------


def test_swapped_metadata_provisions_intent_username(c1_db, monkeypatch):
    _insert_victim(c1_db, "kevin")
    victim_before = _get_premium_row(c1_db, "kevin")
    _insert_intent(c1_db, "intent_honest", "honestuser")
    _mock_nc_success(monkeypatch)
    sent = _mock_email(monkeypatch)
    # Attacker swaps the victim username into Stripe metadata while keeping a
    # valid intent id for their own checkout.
    session = _session_for("intent_honest", "kevin")
    try:
        premium_mod._provision_premium_user(session)
    except Exception:
        pass
    assert _get_premium_row(c1_db, "kevin") == victim_before, \
        "victim row must be untouched by swapped metadata"
    honest = _get_premium_row(c1_db, "honestuser")
    assert honest is not None, "the INTENT username must be the one provisioned"
    assert _get_premium_row(c1_db, "KEVIN") is None or \
        _get_premium_row(c1_db, "kevin") == victim_before
    for _email, user, _token in sent:
        assert user != "kevin", "no setup token may be issued for the victim"


# --- 7. new customer with a free username can still check out ----------------


def test_new_customer_free_username_can_checkout(c1_db, billing_client, monkeypatch):
    calls = _mock_stripe_create(monkeypatch)
    r = billing_client.post("/api/stripe/create_checkout",
                            json={"username": "brandnew",
                                  "display_name": "Brand New",
                                  "plan": "premium_monthly"})
    assert r.status_code == 200
    assert "checkout_url" in r.get_json()
    assert len(calls) == 1


# --- E. missing / expired / consumed intents fail closed ---------------------


def test_provision_missing_intent_fails_closed(c1_db, monkeypatch):
    _mock_nc_success(monkeypatch)
    sent = _mock_email(monkeypatch)
    session = _session_for("intent_nope_missing", "ghost")
    with pytest.raises(Exception):
        premium_mod._provision_premium_user(session)
    assert _get_premium_row(c1_db, "ghost") is None
    assert sent == []


def test_provision_expired_intent_fails_closed(c1_db, monkeypatch):
    _insert_intent(c1_db, "intent_old", "olduser", expired=True)
    _mock_nc_success(monkeypatch)
    sent = _mock_email(monkeypatch)
    with pytest.raises(Exception):
        premium_mod._provision_premium_user(
            _session_for("intent_old", "olduser"))
    assert _get_premium_row(c1_db, "olduser") is None
    assert sent == []


def test_provision_consumed_intent_fails_closed(c1_db, monkeypatch):
    _insert_intent(c1_db, "intent_used", "useduser", consumed=True)
    _mock_nc_success(monkeypatch)
    sent = _mock_email(monkeypatch)
    with pytest.raises(Exception):
        premium_mod._provision_premium_user(
            _session_for("intent_used", "useduser"))
    assert _get_premium_row(c1_db, "useduser") is None
    assert sent == []
