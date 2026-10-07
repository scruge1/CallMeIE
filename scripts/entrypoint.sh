#!/usr/bin/env sh
# callmeie-fix container entrypoint (P5-4)
#
# Runs alembic upgrade head BEFORE uvicorn so the schema is current
# at boot. If DATABASE_URL is set, alembic runs against Postgres;
# a failed migration stops startup before the server starts. If
# DATABASE_URL is absent, skip migration and let server.py use SQLite
# at DB_PATH (the existing local-development path).
#
# This entrypoint is idempotent. `alembic upgrade head` is a no-op
# when the DB is already at head. On first deploy against the existing
# Coolify Postgres, run `alembic stamp 0001_baseline` ONCE manually
# (see MIGRATION-ALEMBIC-SETUP.md) so the live schema is treated as
# baseline and future migrations apply cleanly.
set -e

if [ -n "$DATABASE_URL" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head || {
    migration_status=$?
    echo "[entrypoint] alembic upgrade FAILED — aborting server startup" >&2
    exit "$migration_status"
  }
else
  echo "[entrypoint] DATABASE_URL not set — skipping alembic, using SQLite path"
fi

echo "[entrypoint] uvicorn server:app --host 0.0.0.0 --port 8080"
exec uvicorn server:app --host 0.0.0.0 --port 8080
