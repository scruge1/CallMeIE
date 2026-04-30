"""AUD-038 — SQLite schema + connection helper for AI Agency billing.

clients
  - Per-customer config: tier, included minutes, Stripe + Vapi linkages,
    short-lived admin_token for Customer Portal access.

usage_records
  - One row per Vapi call. ``vapi_call_id`` UNIQUE — entire dedupe story.
  - ``pushed_to_stripe`` 0/1/2 = pending / sent / failed.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Re-exported so callers don't have to ``import sqlite3`` separately.
IntegrityError = sqlite3.IntegrityError


def _resolve_db_path() -> Path:
    env = os.environ.get("AGENCY_DB_PATH", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    # Render persistent disk default (matches DB_PATH pattern in server.py).
    # Falls back to local home dir for dev/test.
    if Path("/var/data").is_dir():
        return Path("/var/data/agency.sqlite")
    return Path(os.path.expanduser("~/.ai-agency/agency.sqlite"))


DB_PATH = _resolve_db_path()


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name           TEXT NOT NULL,
    admin_token            TEXT NOT NULL UNIQUE,
    status                 TEXT NOT NULL DEFAULT 'active',
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    tier                   TEXT,
    included_minutes       INTEGER,
    vapi_assistant_id      TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clients_stripe_cust ON clients(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_clients_vapi_asst   ON clients(vapi_assistant_id);
CREATE INDEX IF NOT EXISTS idx_clients_admin_token ON clients(admin_token);

CREATE TABLE IF NOT EXISTS usage_records (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    vapi_call_id          TEXT UNIQUE NOT NULL,
    client_id             INTEGER NOT NULL,
    duration_seconds      INTEGER NOT NULL,
    ended_reason          TEXT,
    cost_estimate_eur     REAL,
    pushed_to_stripe      INTEGER NOT NULL DEFAULT 0,
    stripe_meter_event_id TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    pushed_at             TEXT,
    FOREIGN KEY (client_id) REFERENCES clients(id)
);
CREATE INDEX IF NOT EXISTS idx_usage_pending ON usage_records(pushed_to_stripe, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_client  ON usage_records(client_id, created_at);
"""


def init_db(db_path: Path | str | None = None) -> Path:
    """Idempotent schema init. Returns the resolved path."""
    target = Path(db_path) if db_path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return target


@contextmanager
def get_db(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Short-lived connection. Auto-commits on exit; rolls back on error."""
    target = Path(db_path) if db_path else DB_PATH
    if not target.exists():
        init_db(target)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = ["DB_PATH", "SCHEMA", "init_db", "get_db", "IntegrityError"]
