"""AUD-038 — meter sync tests (stripe SDK mocked)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import billing.db as billing_db  # noqa: E402
import billing.meter as meter_mod  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "agency.sqlite"
    monkeypatch.setattr(billing_db, "DB_PATH", db_path)
    billing_db.init_db(db_path)
    return db_path


def _seed_client(db_path, *, with_stripe: bool = True) -> int:
    cust = "cus_test_abc" if with_stripe else None
    with billing_db.get_db(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO clients
               (display_name, admin_token, status, vapi_assistant_id, stripe_customer_id)
               VALUES (?, ?, 'active', ?, ?)""",
            ("Test Co", "tok_xyz", "asst_test", cust),
        )
        return cur.lastrowid


def _seed_usage(db_path, client_id: int, call_id: str, secs: int) -> None:
    with billing_db.get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO usage_records (vapi_call_id, client_id, duration_seconds) VALUES (?, ?, ?)",
            (call_id, client_id, secs),
        )


class TestSecondsToMinutes:
    @pytest.mark.parametrize(
        "secs,expected",
        [
            (0, 0),
            (-5, 0),
            (1, 1),
            (59, 1),
            (60, 1),
            (61, 2),
            (119, 2),
            (120, 2),
            (121, 3),
            (3600, 60),
        ],
    )
    def test_round_up(self, secs, expected):
        assert meter_mod._seconds_to_minutes(secs) == expected


class TestPushPendingUsage:
    def test_no_stripe_key_returns_no_key(self, isolated_db, monkeypatch):
        monkeypatch.setattr(meter_mod.stripe, "api_key", "")
        result = meter_mod.push_pending_usage()
        assert result.get("no_key") == 1
        assert result["pushed"] == 0

    def test_pushes_pending(self, isolated_db, monkeypatch):
        monkeypatch.setattr(meter_mod.stripe, "api_key", "sk_test_x")
        client_id = _seed_client(isolated_db)
        _seed_usage(isolated_db, client_id, "call_a", 90)
        _seed_usage(isolated_db, client_id, "call_b", 30)

        fake_evt = MagicMock()
        fake_evt.identifier = "vapi-call_a"
        fake_create = MagicMock(return_value=fake_evt)
        monkeypatch.setattr(meter_mod.stripe.billing.MeterEvent, "create", fake_create)

        result = meter_mod.push_pending_usage()
        assert result["pushed"] == 2
        assert result["failed"] == 0
        assert fake_create.call_count == 2

        # Both rows should now be marked pushed
        with billing_db.get_db(isolated_db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM usage_records WHERE pushed_to_stripe = 1"
            ).fetchone()[0]
        assert n == 2

    def test_skips_client_without_stripe_customer(self, isolated_db, monkeypatch):
        monkeypatch.setattr(meter_mod.stripe, "api_key", "sk_test_x")
        client_id = _seed_client(isolated_db, with_stripe=False)
        _seed_usage(isolated_db, client_id, "call_skip", 60)
        fake_create = MagicMock()
        monkeypatch.setattr(meter_mod.stripe.billing.MeterEvent, "create", fake_create)

        result = meter_mod.push_pending_usage()
        assert result["skipped"] == 1
        assert result["pushed"] == 0
        assert fake_create.call_count == 0

    def test_failed_push_marks_status_2(self, isolated_db, monkeypatch):
        monkeypatch.setattr(meter_mod.stripe, "api_key", "sk_test_x")
        client_id = _seed_client(isolated_db)
        _seed_usage(isolated_db, client_id, "call_fail", 60)

        class _StripeErr(meter_mod.stripe.error.StripeError):
            pass

        def boom(**kw):
            raise _StripeErr("api went away")

        monkeypatch.setattr(meter_mod.stripe.billing.MeterEvent, "create", boom)

        result = meter_mod.push_pending_usage()
        assert result["failed"] == 1
        assert result["pushed"] == 0
        with billing_db.get_db(isolated_db) as conn:
            row = conn.execute(
                "SELECT pushed_to_stripe FROM usage_records WHERE vapi_call_id='call_fail'"
            ).fetchone()
        assert row["pushed_to_stripe"] == 2

    def test_idempotency_identifier_uses_call_id(self, isolated_db, monkeypatch):
        monkeypatch.setattr(meter_mod.stripe, "api_key", "sk_test_x")
        client_id = _seed_client(isolated_db)
        _seed_usage(isolated_db, client_id, "call_unique_xyz", 90)

        fake_create = MagicMock()
        fake_evt = MagicMock()
        fake_evt.identifier = "vapi-call_unique_xyz"
        fake_create.return_value = fake_evt
        monkeypatch.setattr(meter_mod.stripe.billing.MeterEvent, "create", fake_create)

        meter_mod.push_pending_usage()
        kwargs = fake_create.call_args.kwargs
        assert kwargs.get("identifier") == "vapi-call_unique_xyz"
