"""Alembic environment for callmeie-fix.

Reads DATABASE_URL from env (same source as server.py:225). Runs in
online mode against the live Postgres on Coolify Hetzner. Imperative
migrations (no SQLAlchemy ORM models — DDL is hand-rolled in versions/
because the project never adopted SQLAlchemy proper).
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Pull URL from env at runtime (psycopg3-compatible — container ships psycopg + psycopg-binary v3, NOT psycopg2).
db_url = os.environ.get("DATABASE_URL", "").strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
# Force psycopg3 dialect — SQLAlchemy default "postgresql://" maps to psycopg2.
if db_url.startswith("postgresql://") and not db_url.startswith("postgresql+psycopg://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
if not db_url:
    print(
        "[alembic.env] DATABASE_URL not set — refusing to run. "
        "Set it before invoking `alembic upgrade head`.",
        file=sys.stderr,
    )
    sys.exit(2)

config.set_main_option("sqlalchemy.url", db_url)

# No ORM models — DDL is hand-rolled in versions/. target_metadata=None
# disables autogenerate; revisions are written by hand.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
