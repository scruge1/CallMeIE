"""AUD-038 — schema + connection helper tests."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import billing.db as billing_db  # noqa: E402

pytestmark = pytest.mark.unit


class TestSchema:
    def test_init_creates_tables(self, tmp_path):
        db = tmp_path / "agency.sqlite"
        billing_db.init_db(db)
        conn = sqlite3.connect(str(db))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "clients" in tables
        assert "usage_records" in tables

    def test_init_idempotent(self, tmp_path):
        db = tmp_path / "agency.sqlite"
        billing_db.init_db(db)
        billing_db.init_db(db)
        # No exception, schema unchanged
        conn = sqlite3.connect(str(db))
        n = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        assert n == 0

    def test_get_db_auto_inits(self, tmp_path, monkeypatch):
        db = tmp_path / "agency.sqlite"
        monkeypatch.setattr(billing_db, "DB_PATH", db)
        # No explicit init_db
        with billing_db.get_db(db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        assert n == 0


class TestUniqueConstraints:
    def test_vapi_call_id_unique(self, tmp_path):
        db = tmp_path / "agency.sqlite"
        billing_db.init_db(db)
        with billing_db.get_db(db) as conn:
            conn.execute(
                """INSERT INTO clients (display_name, admin_token)
                   VALUES ('X', 'tok_a')"""
            )

        # First insert OK
        with billing_db.get_db(db) as conn:
            conn.execute(
                """INSERT INTO usage_records (vapi_call_id, client_id, duration_seconds)
                   VALUES ('call_1', 1, 60)"""
            )

        # Second insert with same vapi_call_id → IntegrityError
        with pytest.raises(billing_db.IntegrityError):
            with billing_db.get_db(db) as conn:
                conn.execute(
                    """INSERT INTO usage_records (vapi_call_id, client_id, duration_seconds)
                       VALUES ('call_1', 1, 90)"""
                )

    def test_admin_token_unique(self, tmp_path):
        db = tmp_path / "agency.sqlite"
        billing_db.init_db(db)
        with billing_db.get_db(db) as conn:
            conn.execute(
                "INSERT INTO clients (display_name, admin_token) VALUES ('A', 'shared')"
            )
        with pytest.raises(billing_db.IntegrityError):
            with billing_db.get_db(db) as conn:
                conn.execute(
                    "INSERT INTO clients (display_name, admin_token) VALUES ('B', 'shared')"
                )


class TestRollback:
    def test_exception_rolls_back(self, tmp_path):
        db = tmp_path / "agency.sqlite"
        billing_db.init_db(db)
        with pytest.raises(RuntimeError):
            with billing_db.get_db(db) as conn:
                conn.execute(
                    "INSERT INTO clients (display_name, admin_token) VALUES ('toBeRolledBack', 'tok')"
                )
                raise RuntimeError("fail mid-transaction")
        # Confirm not committed
        with billing_db.get_db(db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM clients WHERE display_name='toBeRolledBack'"
            ).fetchone()[0]
        assert n == 0
