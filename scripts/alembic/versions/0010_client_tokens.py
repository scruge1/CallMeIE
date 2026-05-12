"""client_tokens + call_notes (client-side dashboard auth + notes)

Revision ID: 0010_client_tokens
Revises: 0009_tenant_discovery_attribution
Create Date: 2026-05-12

Adds two tables enabling tenant-scoped client.callmeie.ie dashboard:
  - client_tokens   — magic-link tokens granting per-tenant read access
  - call_notes      — internal notes attached to specific call_ids

Tenant scope = an array of Vapi assistant_ids. Every /client/api/* endpoint
filters by `assistant_id IN (client_tokens.assistant_ids)` — load-bearing
isolation property covered by a CI test (see tests/test_client_isolation.py).

See PRD-CLIENT-DASHBOARD-2026-05-12.md §4 for full schema rationale.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_client_tokens"
# NOTE: production alembic_version may be at a later revision (0009 was
# applied via psql out-of-band per MEMORY.md). Before running `alembic upgrade`
# verify with: SELECT version_num FROM alembic_version;
# If prod is at 0009_tenant_discovery_attribution, change this to that string.
down_revision = "0003_unified_leads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_tokens",
        sa.Column("token", sa.Text, primary_key=True),
        sa.Column("tenant_slug", sa.Text, nullable=False),
        sa.Column("tenant_display_name", sa.Text, nullable=False),
        sa.Column("assistant_ids", postgresql.ARRAY(sa.Text), nullable=False,
                  server_default="{}"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_by", sa.Text, nullable=False, server_default="adam"),
    )
    op.create_index("idx_client_tokens_tenant", "client_tokens", ["tenant_slug"])
    op.create_index("idx_client_tokens_active", "client_tokens",
                    ["tenant_slug"], postgresql_where=sa.text("revoked_at IS NULL"))

    op.create_table(
        "call_notes",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("call_id", sa.Text, nullable=False),
        sa.Column("tenant_slug", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),  # 'client' or 'adam'
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("idx_call_notes_call", "call_notes", ["call_id"])
    op.create_index("idx_call_notes_tenant", "call_notes", ["tenant_slug"])


def downgrade() -> None:
    op.drop_index("idx_call_notes_tenant", table_name="call_notes")
    op.drop_index("idx_call_notes_call", table_name="call_notes")
    op.drop_table("call_notes")
    op.drop_index("idx_client_tokens_active", table_name="client_tokens")
    op.drop_index("idx_client_tokens_tenant", table_name="client_tokens")
    op.drop_table("client_tokens")
