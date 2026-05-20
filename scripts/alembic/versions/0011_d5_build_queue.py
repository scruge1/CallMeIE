"""d5_build_queue (Gap A — D5 managed-website Stripe-to-build pipeline)

Revision ID: 0011_d5_build_queue
Revises: 0010_client_tokens
Create Date: 2026-05-21

Creates the d5_build_queue table. Every D5 Stripe checkout
(price IDs in PRICING-SSOT.md §6a — Launch €69, Business €100,
Premium €149) writes one row from the webhook handler in server.py.
The d5_build_runner.py daemon polls for `status='queued'` rows and
invokes the /new-service-site skill via the `claude` CLI.

Schema rationale: a new table (cheaper than column-loading the
append-only `owl_payments` audit log). One row per build job.
Mirrors the docops-rescue-daemon precedent (INFRA.md §14.5) — same
60s polling shape, same coolify network, same Postgres backend.

GDPR (PB3 from draft press-back): `intake_payload` is retained for
90 days post-build for support handoff, then anonymised. The
`anonymise_at` column is set to `finished_at + 90 days` on success;
the purge_old_data.py retention script processes it nightly (added
to that script in a follow-up commit if not already wired).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0011_d5_build_queue"
down_revision = "0010_client_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "d5_build_queue",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("stripe_event_id", sa.Text, nullable=False, unique=True),
        sa.Column("site_id", sa.Text, nullable=False),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column("stripe_price_id", sa.Text, nullable=False),
        sa.Column("customer_email", sa.Text, nullable=False),
        sa.Column("customer_name", sa.Text),
        sa.Column("customer_phone", sa.Text),
        sa.Column(
            "intake_payload",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "queued_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("notified_owner_at", sa.TIMESTAMP(timezone=True)),
        # PB3 — GDPR retention. Set to finished_at + 90 days by the
        # runner on success; purge_old_data.py nulls intake_payload
        # after this timestamp passes (90-day storage limitation).
        sa.Column("anonymise_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "tier IN ('launch','business','premium')",
            name="d5_build_queue_tier_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','blocked')",
            name="d5_build_queue_status_check",
        ),
    )
    op.create_index(
        "idx_d5_build_status_queued",
        "d5_build_queue",
        ["status", "queued_at"],
        postgresql_where=sa.text("status IN ('queued','failed')"),
    )
    op.create_index("idx_d5_build_site", "d5_build_queue", ["site_id"])


def downgrade() -> None:
    op.drop_index("idx_d5_build_site", table_name="d5_build_queue")
    op.drop_index("idx_d5_build_status_queued", table_name="d5_build_queue")
    op.drop_table("d5_build_queue")
