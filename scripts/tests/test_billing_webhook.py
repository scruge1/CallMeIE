"""AUD-038 — Vapi webhook + dedupe + backfill tests."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Allow imports of billing package when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import billing.db as billing_db  # noqa: E402
import billing.webhook as webhook_mod  # noqa: E402

pytestmark = pytest.mark.unit


SECRET = "vapi_test_secret_xyz"
ASSISTANT_ID = "asst_test_123"


def _sign(payload: bytes, secret: str = SECRET, t: int | None = None) -> str:
    if t is None:
        t = int(time.time())
    signed = f"{t}.{payload.decode('utf-8')}"
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha512).hexdigest()
    return f"t={t},v1={digest}"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "agency.sqlite"
    monkeypatch.setattr(billing_db, "DB_PATH", db_path)
    billing_db.init_db(db_path)

    # Insert a test client linked to ASSISTANT_ID
    with billing_db.get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO clients (display_name, admin_token, status, vapi_assistant_id)
               VALUES (?, ?, 'active', ?)""",
            ("Test Co", "tok_admin_abc", ASSISTANT_ID),
        )

    return db_path


@pytest.fixture
def client(isolated_db, monkeypatch):
    """FastAPI test client with the webhook router mounted + secret set."""
    monkeypatch.setattr(webhook_mod, "VAPI_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(webhook_mod, "VAPI_API_KEY", "")  # disable backfill by default
    app = FastAPI()
    app.include_router(webhook_mod.router)
    return TestClient(app)


def _payload(call_id: str = "call_abc", duration: int = 120) -> bytes:
    body = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": call_id, "assistantId": ASSISTANT_ID, "duration": duration},
            "durationSeconds": duration,
            "endedReason": "customer-ended-call",
            "cost": 0.04,
        }
    }
    return json.dumps(body).encode("utf-8")


class TestSignatureVerification:
    def test_good_sig_accepted(self, client):
        body = _payload()
        sig = _sign(body)
        r = client.post(
            "/billing/webhook/vapi",
            content=body,
            headers={"x-vapi-signature": sig, "content-type": "application/json"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_bad_sig_rejected(self, client):
        body = _payload()
        bad_sig = _sign(body, secret="wrong_secret")
        r = client.post(
            "/billing/webhook/vapi",
            content=body,
            headers={"x-vapi-signature": bad_sig, "content-type": "application/json"},
        )
        assert r.status_code == 401

    def test_missing_sig_rejected(self, client):
        r = client.post("/billing/webhook/vapi", content=_payload())
        assert r.status_code == 401

    def test_replay_outside_tolerance_rejected(self, client):
        body = _payload()
        old_t = int(time.time()) - 1000  # > TOLERANCE
        sig = _sign(body, t=old_t)
        r = client.post(
            "/billing/webhook/vapi",
            content=body,
            headers={"x-vapi-signature": sig, "content-type": "application/json"},
        )
        assert r.status_code == 401

    def test_unknown_event_type_ignored(self, client):
        body = json.dumps({"message": {"type": "transcript-update"}}).encode()
        sig = _sign(body)
        r = client.post(
            "/billing/webhook/vapi",
            content=body,
            headers={"x-vapi-signature": sig},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert "ignored" in r.json()


class TestRowPersistence:
    def test_row_inserted_on_first_call(self, client, isolated_db):
        body = _payload(call_id="unique_call_1", duration=180)
        r = client.post(
            "/billing/webhook/vapi",
            content=body,
            headers={"x-vapi-signature": _sign(body)},
        )
        assert r.status_code == 200
        with billing_db.get_db(isolated_db) as conn:
            row = conn.execute(
                "SELECT * FROM usage_records WHERE vapi_call_id = ?",
                ("unique_call_1",),
            ).fetchone()
        assert row is not None
        assert row["duration_seconds"] == 180
        assert row["pushed_to_stripe"] == 0

    def test_idempotent_dedupe(self, client, isolated_db):
        body = _payload(call_id="dup_call", duration=60)
        for _ in range(3):
            sig = _sign(body)
            r = client.post(
                "/billing/webhook/vapi",
                content=body,
                headers={"x-vapi-signature": sig},
            )
            assert r.status_code == 200
        with billing_db.get_db(isolated_db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM usage_records WHERE vapi_call_id = ?",
                ("dup_call",),
            ).fetchone()[0]
        assert count == 1

    def test_unknown_assistant_dropped(self, client, isolated_db):
        body = json.dumps({
            "message": {
                "type": "end-of-call-report",
                "call": {
                    "id": "stranger",
                    "assistantId": "unknown_asst",
                    "duration": 30,
                },
                "durationSeconds": 30,
            }
        }).encode("utf-8")
        sig = _sign(body)
        r = client.post(
            "/billing/webhook/vapi",
            content=body,
            headers={"x-vapi-signature": sig},
        )
        assert r.status_code == 200
        assert r.json().get("dropped") is True
        with billing_db.get_db(isolated_db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM usage_records WHERE vapi_call_id = ?",
                ("stranger",),
            ).fetchone()[0]
        assert count == 0


class TestBackfill:
    def test_backfill_fetches_when_duration_missing(self, client, isolated_db, monkeypatch):
        # Provide an API key so backfill is attempted, then stub the HTTP call.
        monkeypatch.setattr(webhook_mod, "VAPI_API_KEY", "vk_test")

        async def fake_backfill(call_id):
            assert call_id == "missing_dur"
            return {"duration": 240, "assistantId": ASSISTANT_ID}

        monkeypatch.setattr(webhook_mod, "_backfill_call", fake_backfill)

        body = json.dumps({
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "missing_dur", "assistantId": ASSISTANT_ID},
                # durationSeconds intentionally missing
            }
        }).encode("utf-8")
        sig = _sign(body)
        r = client.post(
            "/billing/webhook/vapi",
            content=body,
            headers={"x-vapi-signature": sig},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        with billing_db.get_db(isolated_db) as conn:
            row = conn.execute(
                "SELECT duration_seconds FROM usage_records WHERE vapi_call_id = ?",
                ("missing_dur",),
            ).fetchone()
        assert row is not None
        assert row["duration_seconds"] == 240


class TestSigParser:
    def test_parses_stripe_shape(self):
        body = b'{"x":1}'
        sig = _sign(body)
        assert webhook_mod._verify_vapi_sig(body, sig, SECRET)

    def test_handles_extra_whitespace(self):
        body = b'{"x":1}'
        sig = _sign(body)
        # Insert spaces — must still verify
        spaced = sig.replace(",", " , ")
        assert webhook_mod._verify_vapi_sig(body, spaced, SECRET)

    def test_rejects_non_int_timestamp(self):
        body = b'{"x":1}'
        assert not webhook_mod._verify_vapi_sig(body, "t=abc,v1=def", SECRET)

    def test_rejects_missing_v1(self):
        body = b'{"x":1}'
        assert not webhook_mod._verify_vapi_sig(body, f"t={int(time.time())}", SECRET)
