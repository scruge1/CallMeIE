"""AUD-038 — Stripe Customer Portal endpoint tests."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import billing.db as billing_db  # noqa: E402
import billing.portal as portal_mod  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "agency.sqlite"
    monkeypatch.setattr(billing_db, "DB_PATH", db_path)
    billing_db.init_db(db_path)
    return db_path


@pytest.fixture
def client_with_stripe(isolated_db):
    with billing_db.get_db(isolated_db) as conn:
        conn.execute(
            """INSERT INTO clients (display_name, admin_token, status,
                                    stripe_customer_id, vapi_assistant_id)
               VALUES ('Test Co', 'tok_active', 'active', 'cus_abc', 'asst_x')"""
        )
        conn.execute(
            """INSERT INTO clients (display_name, admin_token, status,
                                    stripe_customer_id, vapi_assistant_id)
               VALUES ('Test No Cust', 'tok_no_cust', 'active', NULL, 'asst_y')"""
        )
        conn.execute(
            """INSERT INTO clients (display_name, admin_token, status,
                                    stripe_customer_id, vapi_assistant_id)
               VALUES ('Cancelled', 'tok_cancelled', 'cancelled', 'cus_def', 'asst_z')"""
        )
    return isolated_db


@pytest.fixture
def app_client(client_with_stripe, monkeypatch):
    monkeypatch.setattr(portal_mod.stripe, "api_key", "sk_test_x")
    fake_session = MagicMock()
    fake_session.url = "https://billing.stripe.com/p/session/test_xyz"
    monkeypatch.setattr(
        portal_mod.stripe.billing_portal.Session,
        "create",
        MagicMock(return_value=fake_session),
    )
    app = FastAPI()
    app.include_router(portal_mod.router)
    return TestClient(app)


class TestPortalSession:
    def test_no_token_401(self, app_client):
        r = app_client.post("/billing/portal")
        assert r.status_code == 401

    def test_no_stripe_key_503(self, app_client, monkeypatch):
        monkeypatch.setattr(portal_mod.stripe, "api_key", "")
        r = app_client.post("/billing/portal", params={"token": "tok_active"})
        assert r.status_code == 503

    def test_unknown_token_404(self, app_client):
        r = app_client.post("/billing/portal", params={"token": "nonexistent"})
        assert r.status_code == 404

    def test_cancelled_client_404(self, app_client):
        r = app_client.post("/billing/portal", params={"token": "tok_cancelled"})
        assert r.status_code == 404

    def test_no_customer_404(self, app_client):
        r = app_client.post("/billing/portal", params={"token": "tok_no_cust"})
        assert r.status_code == 404
        assert "stripe customer" in r.json()["detail"].lower()

    def test_active_client_returns_url(self, app_client):
        r = app_client.post("/billing/portal", params={"token": "tok_active"})
        assert r.status_code == 200
        assert r.json()["url"] == "https://billing.stripe.com/p/session/test_xyz"

    def test_passes_customer_to_stripe(self, app_client, monkeypatch):
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            sess = MagicMock()
            sess.url = "https://x"
            return sess

        monkeypatch.setattr(
            portal_mod.stripe.billing_portal.Session, "create", fake_create
        )
        app_client.post("/billing/portal", params={"token": "tok_active"})
        assert captured.get("customer") == "cus_abc"
