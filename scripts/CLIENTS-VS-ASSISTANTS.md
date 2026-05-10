# `clients` vs `assistants` table disambiguation (P5-6 fix)

## Why this doc exists

Until 2026-05-10, callmeie-fix had **two tables both named `clients`** in **two different databases**, serving **two different concerns**:

```
+-------------------------+         +-------------------------+
| server.py (Postgres)    |         | billing/db.py (SQLite)  |
| -------------------     |         | -------------------     |
| `clients` table:        |         | `clients` table:        |
|   per-Vapi-assistant    |         |   paying-tier billing   |
|   operational config    |  ≠      |   entity                |
+-------------------------+         +-------------------------+
```

These are **NOT the same logical entity**. The naming collision was a footgun for any future maintainer trying to reason about either codepath.

## The fix (alembic 0002 + server.py reads + DDL update)

`server.py` table renamed `clients` → `assistants`.  
`billing/db.py` table keeps `clients` — it really is the right word for a paying customer.

| Table | DB | Lookup key | Columns | Used by |
|---|---|---|---|---|
| `assistants` (was `clients`) | server.py Postgres | `assistant_id` (Vapi UUID) | `name`, `owner_phone`, `from_number`, `calendar_id`, `submission_id`, `status`, `suppressed_at` | `get_client(assistant_id)` (server.py:902) · `/admin/api/clients` (server.py:2727) · `dashboard_stats` query (server.py:2966) |
| `clients` | billing/db.py SQLite | `admin_token` / `stripe_customer_id` | `display_name`, `admin_token`, `stripe_customer_id`, `stripe_subscription_id`, `tier`, `included_minutes`, `vapi_assistant_id` | Customer Portal magic-link auth · Stripe webhook → meter event mapping · Vapi metered billing roll-up |

## What changed

- `scripts/alembic/versions/0002_rename_clients_to_assistants.py` — alembic migration: `ALTER TABLE clients RENAME TO assistants` + `ALTER INDEX idx_clients_created_at RENAME TO idx_assistants_created_at`. Idempotent index rename via `IF EXISTS`.
- `scripts/server.py:608` (init_db) — runtime DDL `CREATE TABLE clients` → `CREATE TABLE assistants`.
- `scripts/server.py:644` (P5-1 index loop) — `idx_clients_created_at ON clients` → `idx_assistants_created_at ON assistants`.
- `scripts/server.py:699` (P5-2 retention targets) — `("clients", "suppressed_at")` → `("assistants", "suppressed_at")`.
- `scripts/server.py:907` `get_client()` — `SELECT * FROM clients` → `SELECT * FROM assistants`.
- `scripts/server.py:2731` `/admin/api/clients` — `SELECT * FROM clients` → `SELECT * FROM assistants`. Endpoint URL kept (back-compat with admin.html JS); rename to `/admin/api/assistants` is a follow-up that requires admin.html update.
- `scripts/server.py:2966` `dashboard_stats` — `SELECT FROM clients WHERE status='active'` → `SELECT FROM assistants WHERE status='active'`.
- `billing/db.py` — **untouched**. The `clients` table name there is correct for its concern.

## Deployment order

1. Push commit. Coolify rebuilds image with the renamed code paths.
2. Container boots via `entrypoint.sh` → `alembic upgrade head` runs migration 0002 → table renamed.
3. Server starts, reads/writes against the new `assistants` name.
4. **Existing rows preserved** — `ALTER TABLE RENAME` is metadata-only, no data movement.

## Rollback path

If 0002 has to be reverted:
1. `alembic downgrade 0001_baseline` — runs the `downgrade()` reverse (rename `assistants` back to `clients`).
2. Pre-rename code revision rolls forward via Coolify revert-deploy.
3. Total downtime: < 30 seconds (alembic metadata-only operation).

## Open follow-up

- `/admin/api/clients` endpoint URL is now misleading. Rename to `/admin/api/assistants` once `callmeie-fix/scripts/admin.html` JS is updated (cross-file coordination — defer to dedicated session).
- `billing/db.py` `clients` is the only `clients` left. If anyone asks "what is `clients`?" the answer is now unambiguous: paying-tier billing entity.

## Why not full consolidation?

Original P5-6 spec offered two options:
- (a) consolidate both tables into one richer `clients` shape (server.py reads migrate to billing/db.py shape)
- (b) keep them split + add an explicit-sync function at provisioning time

Path (a) is invasive. The two tables have different shapes because they serve different concerns. Forcing them together would mean adding ~7 nullable columns to satisfy both readers, AND merging the two DBs. The rename achieves the disambiguation goal (no more confused readers, no more ambiguous queries) without merging concerns that are legitimately separate.

If a future product change actually does want to fuse the two (e.g. every paying customer's `vapi_assistant_id` joins their billing `clients` row to operational `assistants` row), that's a clean schema-design exercise on top of clear naming — easier than starting from "wait, which `clients` table do you mean?"
