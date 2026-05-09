# Alembic setup runbook — callmeie-fix (P5-4)

**Goal:** replace the runtime `init_db()` + `_ddl_fix()` schema-creation path in `scripts/server.py` with versioned alembic migrations. Future schema changes live in `scripts/alembic/versions/000N_*.py`, applied at container boot via `entrypoint.sh`.

**State after this PR (no Adam-keyboard yet):**
- alembic skeleton lives at `scripts/alembic.ini`, `scripts/alembic/env.py`, `scripts/alembic/script.py.mako`, `scripts/alembic/versions/`
- baseline migration `0001_baseline.py` captures all 10 tables + 11 indexes + retention columns + lead expansion columns
- `Dockerfile` now invokes `entrypoint.sh` (was direct uvicorn)
- `entrypoint.sh` runs `alembic upgrade head` before starting uvicorn (idempotent — no-op when DB already at head)
- `requirements.txt` adds `alembic==1.13.2` + `SQLAlchemy==2.0.32`
- `server.py` `init_db()` + `_ddl_fix()` + `_owl_init_tables()` + `_owl_init_payments_table()` **NOT YET REMOVED** — they stay as runtime fallback for local SQLite dev. Removal is a follow-up after the live Coolify run is clean for 7 days.

---

## Step 1 — Adam-keyboard: stamp the existing Coolify DB once (~2 min)

The Coolify Postgres (`zpy3t4torksez48k8attrzbr`) already has all the tables from the runtime DDL path. Running `alembic upgrade head` against it would fail (table-already-exists). The fix is `alembic stamp` — tells alembic "this DB is at revision X, no DDL needed."

From local repo root:

```powershell
$env:DATABASE_URL = "<pull from ~/.claude/routes/.env COOLIFY_PG_INTERNAL_URL or the public URL exposed via Coolify dashboard>"
cd "C:\Users\a33_s\Desktop\callmeie-fix\scripts"
pip install alembic==1.13.2 sqlalchemy==2.0.32
alembic stamp 0001_baseline
# expect: "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl. ... Running stamp_revision  -> 0001_baseline"
alembic current
# expect: 0001_baseline (head)
```

Now the DB is tracked. Future migrations land via `alembic upgrade head` cleanly.

## Step 2 — Deploy the container (Coolify auto-deploys on push)

Once `entrypoint.sh` lands in the image:
- container boots
- `entrypoint.sh` calls `alembic upgrade head`
- alembic sees current revision = `0001_baseline`, head = `0001_baseline`, no-op
- entrypoint falls through to `uvicorn server:app`
- server.py still calls `init_db()` at module-import (line 724) — those `CREATE TABLE IF NOT EXISTS` are also no-ops because tables already exist

Verify post-deploy via Coolify shell:

```sh
alembic current             # 0001_baseline (head)
alembic history             # one row
```

## Step 3 — Future migrations (the new workflow)

When schema changes are needed:

```sh
# in scripts/
alembic revision -m "add_foo_to_bar"
# edit alembic/versions/000N_add_foo_to_bar.py — fill upgrade() + downgrade()
# commit + push
# Coolify rebuild → entrypoint runs `alembic upgrade head` → migration applies
```

No more ad-hoc `ALTER TABLE` in `init_db()` (lead_column_migrations / retention_targets). Those become first-class versioned migrations.

## Step 4 — Decommission `init_db()` (deferred 7 days post-Coolify-clean)

After 7 days of clean alembic-driven boots:
1. Strip `_ddl_fix`, `init_db()`, `_owl_init_tables()`, `_owl_init_payments_table()` from `server.py`
2. Drop `init_db()` call at line 724
3. Drop runtime SQLite fallback (`DB_PATH` + `_USE_PG=False` branch in `_DbProxy.__init__`) — Coolify Postgres is the only prod target after Render decommission 2026-05-16
4. Remove `psycopg`-not-installed fallback (lines 234-237) — psycopg is now mandatory
5. Drop the conditional `_DbIntegrityError` (line 305-308) — single Postgres path
6. server.py loses ~250 lines of dialect-bridging code

## Rollback

If alembic-driven path breaks:

1. Coolify dashboard → callmeie-api → Deploy tab → revert to previous deploy
2. The reverted image has the old `Dockerfile` (`CMD uvicorn server:app`) — uvicorn boots without alembic, runtime DDL path takes over, identical schema state
3. Rollback time: < 2 min

The DB itself is not affected by rollback because `alembic stamp` only writes to `alembic_version` table — schema unchanged.

---

## Files

| Path | Purpose |
|---|---|
| `scripts/alembic.ini` | Config (script_location + sqlalchemy.url empty — env.py fills from DATABASE_URL) |
| `scripts/alembic/env.py` | Online + offline modes; refuses to run without DATABASE_URL |
| `scripts/alembic/script.py.mako` | Stdlib alembic revision template |
| `scripts/alembic/versions/0001_baseline.py` | All 10 tables + 11 indexes + retention columns + lead expansion |
| `scripts/Dockerfile` | Now COPYs alembic + entrypoint.sh, CMD invokes entrypoint |
| `scripts/entrypoint.sh` | `alembic upgrade head` then `exec uvicorn server:app` |
| `scripts/requirements.txt` | + alembic==1.13.2 + SQLAlchemy==2.0.32 |

## Why this matters

Today the schema is implicit — to know what columns exist on `leads`, you read 5 places: server.py:578-595 (CREATE TABLE), server.py:666-678 (4 ad-hoc ALTERs at boot), server.py:692-700 (suppressed_at retention column), `purge_old_data.py` (which uses suppressed_at), and the live DB. After P5-4, the schema is one file — `0001_baseline.py` — and every change after this commit is a numbered revision.

Production benefits:
- Schema changes traceable in git
- Coolify deploy validates schema before serving traffic (fails-fast on DDL syntax error)
- No-op for unchanged schema on every boot — boot cost ≈ unchanged
- Simplifies DR: alembic + DB dump = full reconstruction

Open follow-ups (next focused session):
- Strip runtime DDL path per Step 4
- Migrate `lead_column_migrations` ad-hoc ALTERs to a future `0002_*.py` if any new columns surface
- Wire P5-6 two-DB consolidation (server.py vs billing/db.py `clients` shape) — alembic now exists for the tracked side
