"""unified_leads pipeline (P2-4 Phase 1)

Revision ID: 0003_unified_leads
Revises: 0002_rename_clients_to_assistants
Create Date: 2026-05-10

Adds three tables:
- unified_leads        — single cross-channel pipeline view (5 channels)
- lead_status_log      — append-only audit trail of status transitions
- cron_runs            — heartbeat for morning roll-up + future cron jobs

Backfills existing rows from discovery_submissions / submissions / owl_leads
using ON CONFLICT (dedupe_key) DO NOTHING — idempotent on re-run.

Channel enum values: discovery / onboard / owl_form / email / whatsapp / audit / manual
Status enum: new / contacted / qualified / closed_won / closed_lost / spam

See PDR-NEXT-P2-4-UNIFIED-LEADS.md for full design rationale.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0003_unified_leads"
down_revision = "0002_rename_clients_to_assistants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----- unified_leads ---------------------------------------------------
    op.create_table(
        "unified_leads",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("dedupe_key", sa.Text, nullable=False, unique=True),
        sa.Column("dedupe_kind", sa.Text, nullable=False),  # email|phone|synthetic
        sa.Column("contact_email", sa.Text),
        sa.Column("contact_phone", sa.Text),
        sa.Column("contact_name", sa.Text),
        sa.Column("business_name", sa.Text),
        sa.Column("primary_channel", sa.Text, nullable=False),
        sa.Column("channels", postgresql.ARRAY(sa.Text), nullable=False,
                  server_default="{}"),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("source_refs", postgresql.JSONB, nullable=False,
                  server_default="{}"),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False,
                  server_default="{}"),
        sa.Column("notes", sa.Text),
        sa.Column("assigned_to", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_touch_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("status_changed_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("qualified_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_index("idx_unified_leads_status_touch", "unified_leads",
                    ["status", sa.text("last_touch_at DESC")])
    op.create_index("idx_unified_leads_channel_created", "unified_leads",
                    ["primary_channel", sa.text("created_at DESC")])
    op.create_index("idx_unified_leads_email_partial", "unified_leads",
                    ["contact_email"],
                    postgresql_where=sa.text("contact_email IS NOT NULL"))
    op.create_index("idx_unified_leads_channels_gin", "unified_leads",
                    ["channels"], postgresql_using="gin")
    op.create_index("idx_unified_leads_source_refs_gin", "unified_leads",
                    ["source_refs"], postgresql_using="gin")

    # ----- lead_status_log -------------------------------------------------
    op.create_table(
        "lead_status_log",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("from_status", sa.Text),  # NULL on initial creation
        sa.Column("to_status", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False, server_default="system"),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["lead_id"], ["unified_leads.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_lead_status_log_lead_created", "lead_status_log",
                    ["lead_id", sa.text("created_at DESC")])

    # ----- cron_runs --------------------------------------------------------
    op.create_table(
        "cron_runs",
        sa.Column("job_name", sa.Text, primary_key=True),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_status", sa.Text),
        sa.Column("last_error", sa.Text),
        sa.Column("last_summary", postgresql.JSONB),
    )

    # ----- backfill from existing channels ---------------------------------
    # Idempotent — re-running the migration cannot double-insert thanks to
    # the UNIQUE(dedupe_key) constraint + ON CONFLICT DO NOTHING.
    #
    # Order matters: oldest-channel-first so primary_channel reflects the
    # earliest sighting. discovery -> submissions -> owl_leads.

    # Channel 1 — discovery_submissions (rows w/ contact_email)
    op.execute("""
        INSERT INTO unified_leads (
          dedupe_key, dedupe_kind, contact_email, contact_name,
          primary_channel, channels, status, source_refs, raw_payload,
          created_at, last_touch_at, status_changed_at
        )
        SELECT
          LOWER(TRIM(d.contact_email)) AS dedupe_key,
          'email'::text,
          d.contact_email,
          d.contact_name,
          'discovery'::text,
          ARRAY['discovery']::text[],
          'new'::text,
          jsonb_build_object('discovery_submission_id', d.id::text),
          to_jsonb(d.*),
          d.created_at,
          d.created_at,
          d.created_at
        FROM discovery_submissions d
        WHERE d.contact_email IS NOT NULL
          AND d.contact_email <> ''
          AND d.suppressed_at IS NULL
        ON CONFLICT (dedupe_key) DO NOTHING;
    """)

    # Channel 2 — submissions (onboarding form). Email primary; fallback to
    # phone if no email; synthetic if neither.
    op.execute("""
        INSERT INTO unified_leads (
          dedupe_key, dedupe_kind, contact_email, contact_phone, contact_name,
          business_name, primary_channel, channels, status, source_refs,
          raw_payload, created_at, last_touch_at, status_changed_at
        )
        SELECT
          CASE
            WHEN s.contact_email IS NOT NULL AND s.contact_email <> ''
              THEN LOWER(TRIM(s.contact_email))
            WHEN s.contact_phone IS NOT NULL AND s.contact_phone <> ''
              THEN 'phone:' || REGEXP_REPLACE(s.contact_phone, '[^0-9+]', '', 'g')
            ELSE 'syn:' || ENCODE(SHA256((s.id::text || s.created_at::text)::bytea), 'hex')
          END AS dedupe_key,
          CASE
            WHEN s.contact_email IS NOT NULL AND s.contact_email <> '' THEN 'email'
            WHEN s.contact_phone IS NOT NULL AND s.contact_phone <> '' THEN 'phone'
            ELSE 'synthetic'
          END,
          s.contact_email,
          s.contact_phone,
          s.contact_name,
          s.business_name,
          'onboard'::text,
          ARRAY['onboard']::text[],
          'new'::text,
          jsonb_build_object('submission_id', s.id::text),
          to_jsonb(s.*),
          s.created_at,
          s.created_at,
          s.created_at
        FROM submissions s
        WHERE s.suppressed_at IS NULL
        ON CONFLICT (dedupe_key) DO UPDATE
          SET channels = (
                SELECT ARRAY_AGG(DISTINCT c)
                FROM UNNEST(unified_leads.channels || EXCLUDED.channels) AS c
              ),
              source_refs = unified_leads.source_refs || EXCLUDED.source_refs,
              last_touch_at = GREATEST(unified_leads.last_touch_at, EXCLUDED.last_touch_at),
              business_name = COALESCE(unified_leads.business_name, EXCLUDED.business_name),
              contact_phone = COALESCE(unified_leads.contact_phone, EXCLUDED.contact_phone);
    """)

    # Channel 3 — owl_leads. JSONB blob; extract email/phone best-effort.
    op.execute("""
        INSERT INTO unified_leads (
          dedupe_key, dedupe_kind, contact_email, contact_phone, contact_name,
          primary_channel, channels, status, source_refs, raw_payload,
          created_at, last_touch_at, status_changed_at
        )
        SELECT
          CASE
            WHEN COALESCE(payload_json::jsonb->>'email', '') <> ''
              THEN LOWER(TRIM(payload_json::jsonb->>'email'))
            WHEN COALESCE(payload_json::jsonb->>'phone', '') <> ''
              THEN 'phone:' || REGEXP_REPLACE(payload_json::jsonb->>'phone', '[^0-9+]', '', 'g')
            ELSE 'syn:' || ENCODE(SHA256((id::text || ts::text)::bytea), 'hex')
          END AS dedupe_key,
          CASE
            WHEN COALESCE(payload_json::jsonb->>'email', '') <> '' THEN 'email'
            WHEN COALESCE(payload_json::jsonb->>'phone', '') <> '' THEN 'phone'
            ELSE 'synthetic'
          END,
          NULLIF(payload_json::jsonb->>'email', ''),
          NULLIF(payload_json::jsonb->>'phone', ''),
          NULLIF(payload_json::jsonb->>'name', ''),
          'owl_form'::text,
          ARRAY['owl_form']::text[],
          'new'::text,
          jsonb_build_object('owl_lead_id', id::text, 'site_id', site_id),
          payload_json::jsonb,
          ts,
          ts,
          ts
        FROM owl_leads
        WHERE suppressed_at IS NULL
        ON CONFLICT (dedupe_key) DO UPDATE
          SET channels = (
                SELECT ARRAY_AGG(DISTINCT c)
                FROM UNNEST(unified_leads.channels || EXCLUDED.channels) AS c
              ),
              source_refs = unified_leads.source_refs || EXCLUDED.source_refs,
              last_touch_at = GREATEST(unified_leads.last_touch_at, EXCLUDED.last_touch_at);
    """)

    # Seed cron_runs row for the morning roll-up.
    op.execute("""
        INSERT INTO cron_runs (job_name, last_status)
        VALUES ('morning_rollup', 'pending')
        ON CONFLICT (job_name) DO NOTHING;
    """)


def downgrade() -> None:
    op.drop_table("cron_runs")
    op.drop_index("idx_lead_status_log_lead_created", table_name="lead_status_log")
    op.drop_table("lead_status_log")
    op.drop_index("idx_unified_leads_source_refs_gin", table_name="unified_leads")
    op.drop_index("idx_unified_leads_channels_gin", table_name="unified_leads")
    op.drop_index("idx_unified_leads_email_partial", table_name="unified_leads")
    op.drop_index("idx_unified_leads_channel_created", table_name="unified_leads")
    op.drop_index("idx_unified_leads_status_touch", table_name="unified_leads")
    op.drop_table("unified_leads")
