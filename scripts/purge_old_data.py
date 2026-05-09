#!/usr/bin/env python3
"""
P5-2 — GDPR retention + erasure cron.

Daily-run script that enforces the retention windows promised in
legal/privacy.html §4 + dpa.html across the 7 PII-bearing tables:

  - discovery_submissions  : 90-day hard delete (no PII trail required)
  - submissions (unprovisioned) : 180-day soft-delete, 30-day hard purge
  - owl_leads              : 90-day soft-delete, 30-day hard purge
  - owl_tickets            : closed >30 days OR closed-status >90 days hard delete
  - leads (Vapi-captured)  : 365-day hard delete
  - submissions (already suppressed via /admin/api/erase) : 30-day hard purge
  - clients                : SKIPPED (active customer registry; 6-year Revenue retention)
  - call_events            : SKIPPED (low-PII metadata; Vapi-side wider retention)
  - call_diagnostics       : SKIPPED (low-PII metadata)

Designed to run as a daily cron (GitHub Actions or Coolify scheduled job)
at 03:00 UTC = 03:00 / 04:00 Dublin time (off-peak).

Default mode is --dry-run (prints what WOULD be deleted; no writes).
Use --apply to actually run the deletes. This is a hard requirement —
first cron run with default behavior must NOT silently nuke historical data.

Usage:
    python scripts/purge_old_data.py --dry-run     # default; safe
    python scripts/purge_old_data.py --apply       # commit deletes

Env:
    DATABASE_URL   Postgres connection string (preferred path on Render/Coolify)
    DB_PATH        SQLite fallback path (local dev / sqlite tests)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

# --- DB adapter (mirrors server.py shape so we share dialect handling) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_USE_PG = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")
if _USE_PG and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DB_PATH = os.environ.get("DB_PATH", "/var/data/callmeie.db")

if _USE_PG:
    try:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
    except ImportError:
        print("[purge] DATABASE_URL set but psycopg not installed — falling back to SQLite", file=sys.stderr)
        _USE_PG = False


def _connect():
    if _USE_PG:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    if not os.path.exists(DB_PATH):
        # Local-dev convenience: don't fail loudly if DB doesn't exist; just exit clean.
        raise FileNotFoundError(f"DB_PATH does not exist: {DB_PATH}")
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _q(sql: str) -> str:
    """Translate ? placeholders to %s on Postgres."""
    return sql.replace("?", "%s") if _USE_PG else sql


def _scan(conn, sql: str, params: tuple = ()) -> int:
    """SELECT COUNT(*) helper — returns N for the WHERE clause."""
    if _USE_PG:
        cur = conn.cursor()
        cur.execute(_q(sql), params)
    else:
        cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return 0
    # Backend-agnostic: psycopg dict_row → dict; sqlite3.Row → tuple-like.
    # Both support [0] indexing, so use that uniformly.
    try:
        # psycopg dict_row returns a dict whose first value is the count
        if isinstance(row, dict):
            return int(next(iter(row.values())))
        return int(row[0])
    except Exception:
        return 0


def _interval(days: int) -> str:
    """Dialect-correct 'created_at < N days ago' WHERE fragment."""
    if _USE_PG:
        return f"NOW() - INTERVAL '{days} days'"
    return f"datetime('now', '-{days} days')"


def _execute(conn, sql: str, params: tuple = (), apply: bool = False) -> int:
    """Run an UPDATE/DELETE; return rowcount. No-op + scan when apply=False."""
    if not apply:
        # Translate the action into a SELECT COUNT(*) so dry-run reports the
        # exact row count that would have been affected.
        scan_sql = sql
        # Strip the leading verb up to the WHERE clause — we want
        # `SELECT COUNT(*) FROM table WHERE ...`
        upper = sql.upper().lstrip()
        if upper.startswith("DELETE FROM "):
            after = sql.split(" ", 2)[2]  # "table WHERE ..."
            scan_sql = f"SELECT COUNT(*) FROM {after}"
        elif upper.startswith("UPDATE "):
            # UPDATE tbl SET col = X WHERE ...  →  SELECT COUNT(*) FROM tbl WHERE ...
            tbl = sql.split()[1]
            where_idx = sql.upper().find(" WHERE ")
            where = sql[where_idx:] if where_idx >= 0 else ""
            scan_sql = f"SELECT COUNT(*) FROM {tbl}{where}"
        return _scan(conn, scan_sql, params)

    if _USE_PG:
        cur = conn.cursor()
        cur.execute(_q(sql), params)
        return cur.rowcount or 0
    cur = conn.execute(sql, params)
    return cur.rowcount or 0


def _print_row(label: str, n: int, action: str, apply: bool) -> None:
    verb = "[APPLY]" if apply else "[DRY-RUN]"
    print(f"  {verb} {label}: {n} rows {action}")


def purge(apply: bool = False) -> dict[str, Any]:
    """Run all retention rules. Returns summary dict."""
    started = datetime.now(timezone.utc).isoformat()
    print(f"[purge] started_utc={started} mode={'APPLY' if apply else 'DRY-RUN'} dialect={'pg' if _USE_PG else 'sqlite'}")

    summary: dict[str, dict[str, int]] = {}

    # Dialect-correct "now" expression — used in UPDATE ... SET suppressed_at = X
    now_expr = "NOW()" if _USE_PG else "datetime('now')"

    conn = _connect()
    try:
        # --- 1. discovery_submissions: hard delete >90 days ---
        sql = f"DELETE FROM discovery_submissions WHERE created_at < {_interval(90)}"
        n = _execute(conn, sql, (), apply)
        summary["discovery_submissions"] = {"hard_deleted": n}
        _print_row("discovery_submissions (>90d)", n, "hard-deleted", apply)

        # --- 2. submissions (unprovisioned): 180d soft, 30d hard ---
        # Soft: mark suppressed_at on rows older than 180d that never got
        # provisioned (vapi_assistant_id is NULL = never converted to client).
        soft_sql = (
            f"UPDATE submissions SET suppressed_at = {now_expr} "
            f"WHERE created_at < {_interval(180)} "
            f"AND vapi_assistant_id IS NULL "
            f"AND suppressed_at IS NULL"
        )
        n_soft = _execute(conn, soft_sql, (), apply)
        # Hard: delete rows where suppressed_at is >30d old.
        hard_sql = f"DELETE FROM submissions WHERE suppressed_at IS NOT NULL AND suppressed_at < {_interval(30)}"
        n_hard = _execute(conn, hard_sql, (), apply)
        summary["submissions"] = {"soft_deleted": n_soft, "hard_purged": n_hard}
        _print_row("submissions (>180d, unprovisioned)", n_soft, "soft-deleted (suppressed_at set)", apply)
        _print_row("submissions (suppressed >30d)", n_hard, "hard-purged", apply)

        # --- 3. owl_leads: 90d soft, 30d hard ---
        soft_sql = (
            f"UPDATE owl_leads SET suppressed_at = {now_expr} "
            f"WHERE ts < {_interval(90)} "
            f"AND suppressed_at IS NULL"
        )
        n_soft = _execute(conn, soft_sql, (), apply)
        hard_sql = f"DELETE FROM owl_leads WHERE suppressed_at IS NOT NULL AND suppressed_at < {_interval(30)}"
        n_hard = _execute(conn, hard_sql, (), apply)
        summary["owl_leads"] = {"soft_deleted": n_soft, "hard_purged": n_hard}
        _print_row("owl_leads (>90d)", n_soft, "soft-deleted", apply)
        _print_row("owl_leads (suppressed >30d)", n_hard, "hard-purged", apply)

        # --- 4. owl_tickets: closed AND closed_at >30d → DELETE.
        # Fallback: closed AND no closed_at (legacy rows pre-migration)
        # AND ts >90d → DELETE (proxy: the row is at-least 90d old AND
        # status='closed', so the close-event is necessarily older than
        # the row creation - 0d). 30d window respected via the longer
        # 90-day fallback.
        sql_a = f"DELETE FROM owl_tickets WHERE status = 'closed' AND closed_at IS NOT NULL AND closed_at < {_interval(30)}"
        n_a = _execute(conn, sql_a, (), apply)
        sql_b = f"DELETE FROM owl_tickets WHERE status = 'closed' AND closed_at IS NULL AND ts < {_interval(90)}"
        n_b = _execute(conn, sql_b, (), apply)
        summary["owl_tickets"] = {"hard_deleted_via_closed_at": n_a, "hard_deleted_legacy": n_b}
        _print_row("owl_tickets (closed_at >30d)", n_a, "hard-deleted", apply)
        _print_row("owl_tickets (legacy closed >90d)", n_b, "hard-deleted (no closed_at)", apply)

        # --- 5. leads (Vapi-captured): 365d hard delete ---
        sql = f"DELETE FROM leads WHERE created_at < {_interval(365)}"
        n = _execute(conn, sql, (), apply)
        summary["leads"] = {"hard_deleted": n}
        _print_row("leads (>365d)", n, "hard-deleted", apply)

        # --- 6. SAR-erasure suppressed rows past their 30d grace ---
        # Same 30d hard-purge sweep across owl_tickets / discovery_submissions
        # for rows the /admin/api/erase route marked. (submissions + owl_leads
        # already covered above.)
        for tbl in ("owl_tickets", "discovery_submissions"):
            sql = f"DELETE FROM {tbl} WHERE suppressed_at IS NOT NULL AND suppressed_at < {_interval(30)}"
            n = _execute(conn, sql, (), apply)
            summary.setdefault(tbl, {})["sar_hard_purged"] = n
            _print_row(f"{tbl} (SAR-suppressed >30d)", n, "hard-purged", apply)

        # --- 7. clients: SKIP (active customer registry) ---
        summary["clients"] = {"skipped": "active customer registry; 6-year Revenue retention"}
        print("  [SKIP] clients — active customer registry; 6-year Revenue retention")

        # --- 8. call_events / call_diagnostics: SKIP (low-PII) ---
        summary["call_events"] = {"skipped": "low-PII metadata; Vapi-side retention applies"}
        summary["call_diagnostics"] = {"skipped": "low-PII metadata"}
        print("  [SKIP] call_events / call_diagnostics — low-PII metadata")

        if apply:
            conn.commit()
            print("[purge] COMMITTED")
        else:
            print("[purge] dry-run — no rows changed; rerun with --apply to execute")

    finally:
        conn.close()

    finished = datetime.now(timezone.utc).isoformat()
    summary["_meta"] = {"started_utc": started, "finished_utc": finished, "mode": "apply" if apply else "dry-run"}
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="GDPR retention + erasure purge cron")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete. Default is dry-run (safe).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Default mode — print what would be deleted; no writes.")
    args = parser.parse_args()

    if args.apply and args.dry_run:
        print("[purge] --apply and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    apply = bool(args.apply)
    summary = purge(apply=apply)

    print("\n[purge] summary:")
    for table, info in summary.items():
        if table.startswith("_"):
            continue
        print(f"  {table}: {info}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
