#!/usr/bin/env sh
# callmeie-fix container entrypoint (P5-4)
#
# Runs alembic upgrade head BEFORE uvicorn so the schema is current
# at boot. If DATABASE_URL is set, alembic runs against Postgres;
# if not, alembic refuses (env.py exits 2) and we fall through to
# uvicorn anyway — server.py will use SQLite at DB_PATH (legacy
# local-dev path; production always has DATABASE_URL).
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
    echo "[entrypoint] alembic upgrade FAILED — server starting anyway, schema may be stale"
    # Don't exit — keep service running so Adam can connect to debug.
    # In an empty/fresh DB this is fatal at first INSERT, but in the
    # existing-DB case (which is the common path) the schema is already
    # there from the prior runtime _ddl_fix() pass.
  }
else
  echo "[entrypoint] DATABASE_URL not set — skipping alembic, using SQLite path"
fi

echo "[entrypoint] uvicorn server:app --host 0.0.0.0 --port 8080"
exec uvicorn server:app --host 0.0.0.0 --port 8080
