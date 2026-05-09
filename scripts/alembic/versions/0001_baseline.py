"""baseline schema (callmeie-fix, all 10 tables + indexes + retention columns + lead expansion columns)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-09

This baseline captures the schema as it is on the Coolify Postgres (after
all runtime _ddl_fix() / init_db() / _owl_init_tables() / _owl_init_payments_table()
+ ad-hoc ALTER TABLEs have run). For a fresh DB this migration creates
everything from zero. For the existing live DB on Coolify, run

    alembic stamp 0001_baseline

ONCE so future migrations are tracked without re-creating tables.

Migration runbook: callmeie-fix/MIGRATION-ALEMBIC-SETUP.md.

Tables (10):
  submissions / call_events / leads / call_diagnostics / clients /
  discovery_submissions / owl_sites / owl_leads / owl_tickets /
  owl_payments

Indexes (11):
  6 created_at DESC indexes (P5-1) +
  idx_owl_leads_site_ts + idx_owl_tickets_site_ts + idx_owl_pay_site +
  PK + unique constraints

Retention columns (P5-2 — already in baseline schema):
  suppressed_at on submissions / discovery_submissions / owl_leads /
  owl_tickets / leads / clients +
  closed_at on owl_tickets

Lead expansion columns (already in baseline):
  pain_point / estimated_missed_calls_per_week / next_action /
  callback_requested

Postgres-only — SQLite path in server.py:236-238 stays available for
local dev but alembic only runs against DATABASE_URL (postgres://).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----- submissions ------------------------------------------------------
    op.create_table(
        "submissions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=False), server_default=sa.func.now()),
        sa.Column("status", sa.Text, server_default="pending"),
        sa.Column("business_name", sa.Text),
        sa.Column("contact_name", sa.Text),
        sa.Column("contact_phone", sa.Text),
        sa.Column("contact_email", sa.Text),
        sa.Column("business_type", sa.Text),
        sa.Column("address", sa.Text),
        sa.Column("hours", sa.Text),
        sa.Column("services", sa.Text),
        sa.Column("emergency_number", sa.Text),
        sa.Column("calendar_email", sa.Text),
        sa.Column("plan", sa.Text),
        sa.Column("ai_name", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("vapi_assistant_id", sa.Text),
        sa.Column("provisioned_at", sa.Text),
        sa.Column("suppressed_at", sa.TIMESTAMP(timezone=True)),  # P5-2
    )
    op.create_index("idx_submissions_created_at", "submissions", [sa.text("created_at DESC")])

    # ----- call_events ------------------------------------------------------
    op.create_table(
        "call_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=False), server_default=sa.func.now()),
        sa.Column("call_id", sa.Text),
        sa.Column("event_type", sa.Text),
        sa.Column("assistant", sa.Text),
        sa.Column("summary", sa.Text),
        sa.Column("detail", sa.Text),
    )
    op.create_index("idx_call_events_created_at", "call_events", [sa.text("created_at DESC")])

    # ----- leads ------------------------------------------------------------
    op.create_table(
        "leads",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=False), server_default=sa.func.now()),
        sa.Column("call_id", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column("phone", sa.Text),
        sa.Column("business_type", sa.Text),
        sa.Column("interest", sa.Text),
        sa.Column("source", sa.Text, server_default="demo"),
        sa.Column("demo_completed", sa.Integer, server_default="0"),
        sa.Column("topics_discussed", sa.Text),
        sa.Column("interest_level", sa.Text),
        # Below 4 cols added via ad-hoc ALTERs at runtime; rolled into baseline.
        sa.Column("pain_point", sa.Text),
        sa.Column("estimated_missed_calls_per_week", sa.Text),
        sa.Column("next_action", sa.Text),
        sa.Column("callback_requested", sa.Integer, server_default="0"),
        sa.Column("suppressed_at", sa.TIMESTAMP(timezone=True)),  # P5-2
    )
    op.create_index("idx_leads_created_at", "leads", [sa.text("created_at DESC")])

    # ----- call_diagnostics ------------------------------------------------
    op.create_table(
        "call_diagnostics",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=False), server_default=sa.func.now()),
        sa.Column("call_id", sa.Text, unique=True),
        sa.Column("assistant", sa.Text),
        sa.Column("score", sa.Float),
        sa.Column("diagnosis", sa.Text),
        sa.Column("action", sa.Text),
    )
    op.create_index("idx_call_diagnostics_created_at", "call_diagnostics", [sa.text("created_at DESC")])

    # ----- clients ----------------------------------------------------------
    op.create_table(
        "clients",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("assistant_id", sa.Text, unique=True),
        sa.Column("name", sa.Text),
        sa.Column("owner_phone", sa.Text),
        sa.Column("from_number", sa.Text),
        sa.Column("calendar_id", sa.Text),
        sa.Column("status", sa.Text, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=False), server_default=sa.func.now()),
        sa.Column("submission_id", sa.Integer),
        sa.Column("suppressed_at", sa.TIMESTAMP(timezone=True)),  # P5-2
    )
    op.create_index("idx_clients_created_at", "clients", [sa.text("created_at DESC")])

    # ----- discovery_submissions ------------------------------------------
    op.create_table(
        "discovery_submissions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=False), server_default=sa.func.now()),
        sa.Column("page_context", sa.Text),
        sa.Column("business", sa.Text),
        sa.Column("pain", sa.Text),
        sa.Column("team_size", sa.Text),
        sa.Column("urgency", sa.Text),
        sa.Column("contact_email", sa.Text),
        sa.Column("contact_name", sa.Text),
        sa.Column("recommended_product", sa.Text),
        sa.Column("tier_anchor", sa.Text),
        sa.Column("result_text", sa.Text),
        sa.Column("ip_hash", sa.Text),
        sa.Column("user_agent", sa.Text),
        sa.Column("suppressed_at", sa.TIMESTAMP(timezone=True)),  # P5-2
    )
    op.create_index(
        "idx_discovery_submissions_created_at",
        "discovery_submissions",
        [sa.text("created_at DESC")],
    )

    # ----- owl_sites --------------------------------------------------------
    op.create_table(
        "owl_sites",
        sa.Column("site_id", sa.Text, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column("care_tier", sa.Text),
        sa.Column("lead_email", sa.Text, nullable=False),
        sa.Column("lead_sms", sa.Text),
        sa.Column("edit_emails", sa.Text, nullable=False, server_default="[]"),
        sa.Column("admin_token", sa.Text, nullable=False),
        sa.Column("live_url", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=False), nullable=False, server_default=sa.func.now()),
    )

    # ----- owl_leads --------------------------------------------------------
    op.create_table(
        "owl_leads",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("site_id", sa.Text, nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=False), nullable=False, server_default=sa.func.now()),
        sa.Column("form_type", sa.Text, nullable=False, server_default="contact"),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("submitter_ip", sa.Text),
        sa.Column("submitted_from", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("suppressed_at", sa.TIMESTAMP(timezone=True)),  # P5-2
    )
    op.create_index("idx_owl_leads_site_ts", "owl_leads", ["site_id", "ts"])

    # ----- owl_tickets ------------------------------------------------------
    op.create_table(
        "owl_tickets",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("site_id", sa.Text, nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=False), nullable=False, server_default=sa.func.now()),
        sa.Column("submitter_email", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("priority", sa.Text, nullable=False, server_default="normal"),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("sla_due", sa.Text),
        sa.Column("suppressed_at", sa.TIMESTAMP(timezone=True)),  # P5-2
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),  # P5-2
    )
    op.create_index("idx_owl_tickets_site_ts", "owl_tickets", ["site_id", "ts"])

    # ----- owl_payments -----------------------------------------------------
    op.create_table(
        "owl_payments",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("stripe_event_id", sa.Text, unique=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=False), nullable=False, server_default=sa.func.now()),
        sa.Column("site_id", sa.Text),
        sa.Column("customer_id", sa.Text),
        sa.Column("subscription_id", sa.Text),
        sa.Column("product_key", sa.Text),
        sa.Column("amount", sa.Integer),
        sa.Column("currency", sa.Text),
        sa.Column("status", sa.Text),
        sa.Column("payload_json", sa.Text),
    )
    op.create_index("idx_owl_pay_site", "owl_payments", ["site_id", "ts"])


def downgrade() -> None:
    # Reverse-create order matters because indexes depend on tables.
    op.drop_index("idx_owl_pay_site", table_name="owl_payments")
    op.drop_table("owl_payments")
    op.drop_index("idx_owl_tickets_site_ts", table_name="owl_tickets")
    op.drop_table("owl_tickets")
    op.drop_index("idx_owl_leads_site_ts", table_name="owl_leads")
    op.drop_table("owl_leads")
    op.drop_table("owl_sites")
    op.drop_index("idx_discovery_submissions_created_at", table_name="discovery_submissions")
    op.drop_table("discovery_submissions")
    op.drop_index("idx_clients_created_at", table_name="clients")
    op.drop_table("clients")
    op.drop_index("idx_call_diagnostics_created_at", table_name="call_diagnostics")
    op.drop_table("call_diagnostics")
    op.drop_index("idx_leads_created_at", table_name="leads")
    op.drop_table("leads")
    op.drop_index("idx_call_events_created_at", table_name="call_events")
    op.drop_table("call_events")
    op.drop_index("idx_submissions_created_at", table_name="submissions")
    op.drop_table("submissions")
