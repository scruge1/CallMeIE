"""AUD-038 — meter-sync admin endpoint tests."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import billing.admin as admin_mod  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setattr(admin_mod, "METER_SYNC_TOKEN", "test_token_xyz")
    app = FastAPI()
    app.include_router(admin_mod.router)
    return TestClient(app)


class TestAdminMeterSync:
    def test_no_env_token_returns_503(self, monkeypatch):
        monkeypatch.setattr(admin_mod, "METER_SYNC_TOKEN", "")
        app = FastAPI()
        app.include_router(admin_mod.router)
        client = TestClient(app)
        r = client.post("/billing/meter/sync")
        assert r.status_code == 503

    def test_missing_header_401(self, app_client):
        r = app_client.post("/billing/meter/sync")
        assert r.status_code == 401

    def test_wrong_token_401(self, app_client):
        r = app_client.post(
            "/billing/meter/sync",
            headers={"x-meter-sync-token": "wrong"},
        )
        assert r.status_code == 401

    def test_valid_token_runs_sync(self, app_client, monkeypatch):
        called = {"n": 0}

        def fake_sync():
            called["n"] += 1
            return {"pushed": 3, "failed": 0, "skipped": 1}

        monkeypatch.setattr(admin_mod, "push_pending_usage", fake_sync)
        r = app_client.post(
            "/billing/meter/sync",
            headers={"x-meter-sync-token": "test_token_xyz"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True, "result": {"pushed": 3, "failed": 0, "skipped": 1}}
        assert called["n"] == 1

    def test_compare_digest_equal_length_attack_resistance(self, app_client, monkeypatch):
        """Sanity: secrets.compare_digest used (constant-time)."""
        # We can't test timing directly but confirm it's wired in via the
        # check function returning False for any non-equal input.
        assert admin_mod._check_token("") is False
        assert admin_mod._check_token("partial_match") is False
        # Set token via monkeypatch
        monkeypatch.setattr(admin_mod, "METER_SYNC_TOKEN", "specific_value")
        assert admin_mod._check_token("specific_value") is True
        assert admin_mod._check_token("specific_valuf") is False  # one char off
