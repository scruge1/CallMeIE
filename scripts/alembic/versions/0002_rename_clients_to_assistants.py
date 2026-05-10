"""rename clients table to assistants (P5-6 two-DB split disambiguation)

Revision ID: 0002_rename_clients_to_assistants
Revises: 0001_baseline
Create Date: 2026-05-10

P5-6 fix: callmeie-fix had TWO tables named `clients` in two separate
DBs serving two different concerns:

  - server.py `clients`     -> per-Vapi-assistant operational config
                               (name / owner_phone / from_number /
                               calendar_id / submission_id). Lookup by
                               assistant_id.
  - billing/db.py `clients` -> paying-tier billing entity (display_name,
                               admin_token, stripe_customer_id, tier,
                               included_minutes). Lookup by admin_token
                               or stripe_customer_id.

These are NOT the same logical entity. They co-existed because the
operational table predated the billing system. To stop confusing
readers + future maintainers, this migration renames the OPERATIONAL
table to `assistants`. The BILLING table keeps the `clients` name
since `client` is the right word for a paying customer.

The rename:
  - ALTER TABLE clients RENAME TO assistants
  - ALTER INDEX idx_clients_created_at RENAME TO idx_assistants_created_at
  - existing rows + retention `suppressed_at` column preserved unchanged

Server-side code (server.py:907 / :2731 / :2962 + init_db CREATE TABLE)
must be updated in the same commit so the running container reads the
right table after this migration applies. See:
  - callmeie-fix/scripts/CLIENTS-VS-ASSISTANTS.md (rationale)
  - server.py runtime DDL change (rename CREATE TABLE clients -> assistants)
"""
from __future__ import annotations

from alembic import op


revision = "0002_rename_clients_to_assistants"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("clients", "assistants")
    op.execute("ALTER INDEX IF EXISTS idx_clients_created_at RENAME TO idx_assistants_created_at")


def downgrade() -> None:
    op.execute("ALTER INDEX IF EXISTS idx_assistants_created_at RENAME TO idx_clients_created_at")
    op.rename_table("assistants", "clients")
