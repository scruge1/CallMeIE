"""Gap A smoke tests — D5 build pipeline (PDR-CLIENT-FULFILLMENT.md §6).

Three things we MUST verify before declaring this safe to deploy:

  1. The d5_build_queue table is created idempotently on a fresh SQLite DB.
  2. The webhook detects a D5 checkout.session.completed (one of the 3
     PRICING-SSOT.md §6a price IDs) and inserts a queue row + returns 200.
  3. A non-D5 webhook (e.g. a care plan) is a no-op for the queue —
     no false positives.
  4. Replaying the same event is idempotent (UNIQUE on stripe_event_id).
  5. `_extract_stripe_price_id` finds the price across all three event
     shapes Stripe sends (line_items, items.data, metadata.price_id).

This test imports server.py with DB_PATH pointed at a tmp SQLite file
and OWL_STRIPE_WEBHOOK_SECRET set BEFORE import, so the runtime DDL
runs against the isolated DB. server.py is heavy — we accept the
~1-2s import cost for these critical correctness checks.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


D5_LAUNCH = "price_1TYuVGCEqG2AuI1zqvAc1w2B"
D5_BUSINESS = "price_1TYuVHCEqG2AuI1zDn31aEM1"
D5_PREMIUM = "price_1TYuVHCEqG2AuI1zSmCBhi0f"

NON_D5_PRICE = "price_1TVxwgCEqG2AuI1zPZzlP7q3"  # Receptionist Professional
WEBHOOK_SECRET = "whsec_test_d5_secret"


def _sign(payload: bytes, secret: str = WEBHOOK_SECRET, t: int | None = None) -> str:
    """Stripe-format signature: t=...,v1=hex(hmac_sha256(secret, t.payload))."""
    if t is None:
        t = int(time.time())
    signed = f"{t}.{payload.decode('utf-8')}"
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def _checkout_session_completed_body(
    *,
    event_id: str = "evt_d5_test_001",
    price_id: str = D5_LAUNCH,
    email: str = "owner@testbiz.ie",
    name: str = "Test Biz",
    phone: str = "+353851234567",
) -> bytes:
    """Build a minimal but realistic Stripe checkout.session.completed payload.
    The handler reads price via _extract_stripe_price_id → tries
    metadata.price_id first (set by some Payment Links), then items.data,
    then line_items. We populate metadata so the test exercises the
    shortest path."""
    body = {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_d5_001",
                "customer": "cus_test_d5_owner",
                "amount_total": 6900,  # €69 launch
                "currency": "eur",
                "status": "complete",
                "customer_details": {
                    "email": email,
                    "name": name,
                    "phone": phone,
                },
                "metadata": {"price_id": price_id},
            }
        },
    }
    return json.dumps(body).encode("utf-8")


@pytest.fixture(scope="module")
def server_module(tmp_path_factory):
    """Import server.py with DB_PATH redirected to a tmp SQLite file.
    Module-scoped because server.py is expensive to import (google + fastapi
    + billing routers all wire on first import). All tests in this module
    share the same isolated DB — they pick unique stripe_event_ids to
    avoid cross-test pollution."""
    tmp_db = tmp_path_factory.mktemp("d5") / "callmeie.db"
    os.environ["DB_PATH"] = str(tmp_db)
    # Critical: ensure SQLite path (not Postgres) — handler only writes
    # to the same DB the runtime DDL ran against.
    os.environ.pop("DATABASE_URL", None)
    os.environ["OWL_STRIPE_WEBHOOK_SECRET"] = WEBHOOK_SECRET
    # Disable Twilio so the test doesn't actually try to send SMS
    os.environ["TWILIO_ACCOUNT_SID"] = ""
    os.environ["TWILIO_AUTH_TOKEN"] = ""
    os.environ["OWNER_NOTIFICATION_NUMBER"] = ""

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    # Drop any previously imported copy so the new env takes effect.
    if "server" in sys.modules:
        del sys.modules["server"]
    server = importlib.import_module("server")
    return server


@pytest.fixture
def client(server_module):
    return TestClient(server_module.app)


class TestSchema:
    def test_d5_build_queue_table_exists(self, server_module):
        """Runtime DDL must have created the table at module import."""
        with server_module.get_db() as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='d5_build_queue'"
            )
            row = cur.fetchone()
        assert row is not None, "d5_build_queue table not created at import"

    def test_d5_init_idempotent(self, server_module):
        """Re-running the init must not raise (IF NOT EXISTS)."""
        server_module._d5_init_queue_table()
        server_module._d5_init_queue_table()


class TestExtractStripePriceId:
    def test_from_metadata(self, server_module):
        obj = {"metadata": {"price_id": D5_LAUNCH}}
        assert server_module._extract_stripe_price_id(
            obj, "checkout.session.completed"
        ) == D5_LAUNCH

    def test_from_items_data(self, server_module):
        obj = {"items": {"data": [{"price": {"id": D5_BUSINESS}}]}}
        assert server_module._extract_stripe_price_id(
            obj, "customer.subscription.created"
        ) == D5_BUSINESS

    def test_from_line_items(self, server_module):
        obj = {"line_items": {"data": [{"price": {"id": D5_PREMIUM}}]}}
        assert server_module._extract_stripe_price_id(
            obj, "checkout.session.completed"
        ) == D5_PREMIUM

    def test_returns_empty_when_missing(self, server_module):
        assert server_module._extract_stripe_price_id({}, "checkout.session.completed") == ""

    def test_metadata_wins_over_items(self, server_module):
        """If both are present, metadata.price_id is authoritative (set by Payment Links)."""
        obj = {
            "metadata": {"price_id": D5_LAUNCH},
            "items": {"data": [{"price": {"id": D5_PREMIUM}}]},
        }
        assert server_module._extract_stripe_price_id(
            obj, "checkout.session.completed"
        ) == D5_LAUNCH


class TestWebhookDetectsD5:
    def test_d5_launch_enqueues_and_returns_200(self, client, server_module):
        body = _checkout_session_completed_body(
            event_id="evt_d5_launch_001", price_id=D5_LAUNCH,
        )
        sig = _sign(body)
        r = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["d5_enqueued"] is True
        assert data["d5_tier"] == "launch"

        with server_module.get_db() as conn:
            cur = conn.execute(
                "SELECT site_id, tier, stripe_price_id, customer_email, status "
                "FROM d5_build_queue WHERE stripe_event_id = ?",
                ("evt_d5_launch_001",),
            )
            row = cur.fetchone()
        assert row is not None
        # sqlite3.Row supports indexed access
        assert row[1] == "launch"
        assert row[2] == D5_LAUNCH
        assert row[3] == "owner@testbiz.ie"
        assert row[4] == "queued"

    def test_d5_business_enqueues(self, client):
        body = _checkout_session_completed_body(
            event_id="evt_d5_business_001", price_id=D5_BUSINESS,
        )
        sig = _sign(body)
        r = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        assert r.status_code == 200
        assert r.json()["d5_tier"] == "business"

    def test_d5_premium_enqueues(self, client):
        body = _checkout_session_completed_body(
            event_id="evt_d5_premium_001", price_id=D5_PREMIUM,
        )
        sig = _sign(body)
        r = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        assert r.status_code == 200
        assert r.json()["d5_tier"] == "premium"

    def test_creates_owl_sites_row(self, client, server_module):
        """D5 detection must auto-create an owl_sites row (placeholder until
        the build runner publishes)."""
        body = _checkout_session_completed_body(
            event_id="evt_d5_sites_001",
            price_id=D5_LAUNCH,
            email="site_test@example.ie",
        )
        sig = _sign(body)
        r = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        assert r.status_code == 200
        with server_module.get_db() as conn:
            cur = conn.execute(
                "SELECT tier FROM owl_sites WHERE lead_email = ?",
                ("site_test@example.ie",),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "launch"


class TestWebhookNonD5IsNoOp:
    def test_non_d5_price_does_not_enqueue(self, client, server_module):
        body = _checkout_session_completed_body(
            event_id="evt_non_d5_001", price_id=NON_D5_PRICE,
        )
        sig = _sign(body)
        r = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        # Still 200 — non-D5 events must continue to work
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["d5_enqueued"] is False
        assert data["d5_tier"] is None

        with server_module.get_db() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM d5_build_queue WHERE stripe_event_id = ?",
                ("evt_non_d5_001",),
            )
            n = cur.fetchone()[0]
        assert n == 0, "non-D5 webhook must not insert into d5_build_queue"

    def test_event_with_no_price_id_is_no_op(self, client, server_module):
        """checkout.session.completed without any extractable price_id
        — e.g. an old test event — must not enqueue."""
        body_dict = {
            "id": "evt_no_price_001",
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_x", "customer_details": {"email": "x@x.ie"}}},
        }
        body = json.dumps(body_dict).encode("utf-8")
        sig = _sign(body)
        r = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        assert r.status_code == 200
        assert r.json()["d5_enqueued"] is False


class TestIdempotency:
    def test_replay_does_not_double_insert(self, client, server_module):
        """Replaying the exact same event must not create a second queue row.
        The UNIQUE on stripe_event_id in owl_payments already returns
        {ok:true, deduped:true} and short-circuits before reaching the D5
        branch — so the D5 row is created exactly once even across replays."""
        body = _checkout_session_completed_body(
            event_id="evt_d5_idem_001", price_id=D5_LAUNCH,
        )
        sig = _sign(body)
        r1 = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        r2 = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Second call returns deduped via owl_payments UNIQUE
        assert r2.json().get("deduped") is True

        with server_module.get_db() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM d5_build_queue WHERE stripe_event_id = ?",
                ("evt_d5_idem_001",),
            )
            n = cur.fetchone()[0]
        assert n == 1, f"expected 1 d5_build_queue row after replay, got {n}"


class TestSignatureRequired:
    def test_bad_signature_rejected(self, client):
        body = _checkout_session_completed_body(event_id="evt_bad_sig_001")
        bad_sig = _sign(body, secret="wrong_secret")
        r = client.post(
            "/owl/stripe/webhook",
            content=body,
            headers={"stripe-signature": bad_sig, "content-type": "application/json"},
        )
        assert r.status_code == 401
