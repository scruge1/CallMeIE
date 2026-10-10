"""Call-report credential boundary with synthetic headers and no provider/DB IO."""
import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing.webhook as webhook

SECRET = "synthetic-call-report-only-secret-20261009"


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(webhook, "VAPI_CALL_REPORT_SECRET", SECRET)
    reached = []
    app = FastAPI()

    @app.post("/report", dependencies=[Depends(webhook.require_call_report_auth)])
    async def report(request: Request):
        reached.append(True)
        await request.json()
        return {"ok": True}

    with TestClient(app) as client:
        yield client, reached


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-synthetic"},
    {"Authorization": "Basic synthetic"},
    {"X-Vapi-Secret": "wrong-synthetic"},
    {"Authorization": "Bearer "+SECRET, "X-Vapi-Secret": "wrong-synthetic"},
    {"Authorization": "Bearer wrong-synthetic", "X-Vapi-Secret": SECRET},
    {"Authorization": "", "X-Vapi-Secret": SECRET},
    [("Authorization", "Bearer "+SECRET), ("Authorization", "Bearer "+SECRET)],
    [("X-Vapi-Secret", SECRET), ("X-Vapi-Secret", SECRET)],
    {"X-Vapi-Signature": "synthetic-unverified-format"},
])
def test_denial_precedes_body_and_handler(boundary, headers):
    client, reached = boundary
    response = client.post("/report", headers=headers, content="not-json")
    assert response.status_code == 401
    assert reached == []
    assert SECRET not in response.text


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer "+SECRET},
    {"Authorization": SECRET},
    {"X-Vapi-Secret": SECRET},
    {"Authorization": "Bearer "+SECRET, "X-Vapi-Secret": SECRET},
    {"authorization": "bearer "+SECRET},
])
def test_supported_static_transports(boundary, headers):
    client, reached = boundary
    assert client.post("/report", headers=headers, json={}).status_code == 200
    assert reached == [True]


@pytest.mark.parametrize("secret", ["", "changeme", "a"*31, "é"*40])
def test_missing_or_invalid_configuration_fails_closed(boundary, monkeypatch, secret):
    client, reached = boundary
    monkeypatch.setattr(webhook, "VAPI_CALL_REPORT_SECRET", secret)
    response = client.post("/report", headers={"Authorization": "Bearer "+SECRET}, content="not-json")
    assert response.status_code == 503
    assert reached == []


def test_billing_secret_is_not_call_report_authority(boundary, monkeypatch):
    client, reached = boundary
    monkeypatch.setattr(webhook, "VAPI_WEBHOOK_SECRET", "synthetic-billing-only-secret-20261009")
    assert client.post("/report", headers={"X-Vapi-Secret": webhook.VAPI_WEBHOOK_SECRET}, json={}).status_code == 401
    assert reached == []
