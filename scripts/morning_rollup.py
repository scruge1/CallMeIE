"""morning_rollup.py — daily lead pipeline summary (P2-4 Phase 1).

Reads unified_leads + lead_status_log + cron_runs from Coolify Postgres,
computes a 24h pipeline summary, sends Resend email to hello@callmeie.ie
+ Telegram one-line ping. Idempotent: skips if cron_runs.morning_rollup
ran in last 12h (unless --force).

Invocation paths:
  python scripts/morning_rollup.py             # live send
  python scripts/morning_rollup.py --dry-run   # print only, no send, no DB write
  python scripts/morning_rollup.py --force     # ignore 12h cooldown

Env required:
  DATABASE_URL    — Coolify Postgres
  RESEND_API_KEY  — for email send
  TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID — for ping

Designed to run from:
  - GitHub Actions cron (existing pattern from purge-old-data.yml)
  - Coolify cron belt-and-braces (separate workflow)
  - In-app APScheduler (future — call run() directly)

Schedule target: 07:30 Europe/Dublin daily.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_OWNER_CHAT_ID = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").strip()
ROLLUP_TO = os.environ.get("ROLLUP_TO_EMAIL", "hello@callmeie.ie").strip()
ROLLUP_FROM = os.environ.get("ROLLUP_FROM_EMAIL", "alerts@callmeie.ie").strip()

if not DATABASE_URL:
    print("[rollup] DATABASE_URL not set; aborting", file=sys.stderr)
    sys.exit(2)


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)


def _check_cooldown(conn) -> bool:
    """Return True if we should run; False if cooled down."""
    if FORCE:
        return True
    cur = conn.execute(
        "SELECT last_run_at FROM cron_runs WHERE job_name = 'morning_rollup'"
    )
    row = cur.fetchone()
    if not row or not row.get("last_run_at"):
        return True
    age = datetime.now(timezone.utc) - row["last_run_at"]
    if age < timedelta(hours=12):
        print(f"[rollup] cooldown — last run {age} ago; skip (use --force to override)")
        return False
    return True


def _gather(conn) -> dict:
    """Read summary numbers + actionable lists."""
    out: dict = {}

    # Last 24h channel breakdown
    last_24h = conn.execute("""
        SELECT primary_channel, status, count(*) AS n
        FROM unified_leads
        WHERE created_at >= NOW() - INTERVAL '24 hours'
          AND status_changed_at >= NOW() - INTERVAL '24 hours'
        GROUP BY primary_channel, status
    """).fetchall()
    out["last_24h"] = list(last_24h)

    # Status counts (24h, by status alone)
    by_status_24h = conn.execute("""
        SELECT to_status AS status, count(*) AS n
        FROM lead_status_log
        WHERE created_at >= NOW() - INTERVAL '24 hours'
        GROUP BY to_status
    """).fetchall()
    out["status_changes_24h"] = {r["status"]: int(r["n"]) for r in by_status_24h}

    # Pipeline counts (current state)
    pipeline = conn.execute("""
        SELECT status, count(*) AS n
        FROM unified_leads
        WHERE status NOT IN ('closed_won', 'closed_lost', 'spam')
        GROUP BY status
    """).fetchall()
    out["pipeline"] = {r["status"]: int(r["n"]) for r in pipeline}

    # Top 3 untouched (status=new, oldest first, oldest >48h prioritised)
    untouched = conn.execute("""
        SELECT id::text AS id, primary_channel, contact_email, contact_phone,
               contact_name, business_name, created_at, last_touch_at
        FROM unified_leads
        WHERE status = 'new'
          AND created_at < NOW() - INTERVAL '48 hours'
        ORDER BY created_at ASC
        LIMIT 3
    """).fetchall()
    out["untouched_top3"] = list(untouched)

    # Stale contacted (>5d, no further action)
    stale_contacted = conn.execute("""
        SELECT count(*) AS n
        FROM unified_leads
        WHERE status = 'contacted'
          AND status_changed_at < NOW() - INTERVAL '5 days'
    """).fetchone()
    out["stale_contacted"] = int(stale_contacted["n"]) if stale_contacted else 0

    # Recent qualified (24h)
    qualified_24h = conn.execute("""
        SELECT contact_email, business_name, primary_channel, qualified_at
        FROM unified_leads
        WHERE qualified_at >= NOW() - INTERVAL '24 hours'
        ORDER BY qualified_at DESC
        LIMIT 5
    """).fetchall()
    out["qualified_24h"] = list(qualified_24h)

    # Total
    total = conn.execute("SELECT count(*) AS n FROM unified_leads").fetchone()
    out["total_leads"] = int(total["n"]) if total else 0

    return out


def _format_email(stats: dict) -> tuple[str, str]:
    """Return (subject, body)."""
    sc = stats.get("status_changes_24h", {})
    pipeline = stats.get("pipeline", {})
    n_new = sc.get("new", 0)
    n_qualified = sc.get("qualified", 0)
    n_won = sc.get("closed_won", 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    subject = f"CallMeIE Leads — {n_new} new, {n_qualified} qualified, {n_won} won [{today}]"

    lines = ["Morning Adam,", ""]
    lines.append("Last 24h:")
    last_24h_chan: dict[str, int] = {}
    for r in stats.get("last_24h", []):
        last_24h_chan[r["primary_channel"]] = last_24h_chan.get(r["primary_channel"], 0) + int(r["n"])
    if last_24h_chan:
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(last_24h_chan.items(), key=lambda x: -x[1]))
        lines.append(f"- New: {n_new} ({breakdown})")
    else:
        lines.append(f"- New: {n_new}")
    lines.append(f"- Qualified: {n_qualified}")
    lines.append(f"- Closed: {n_won} won, {sc.get('closed_lost', 0)} lost")
    lines.append("")

    lines.append("Pipeline now:")
    new_unt = sum(1 for r in stats.get("untouched_top3", []))
    lines.append(f"- New (untouched >48h): {new_unt}{'  <- needs action' if new_unt else ''}")
    lines.append(f"- Contacted (no reply >5d): {stats.get('stale_contacted', 0)}{'  <- needs nudge' if stats.get('stale_contacted', 0) else ''}")
    lines.append(f"- Qualified (open): {pipeline.get('qualified', 0)}")
    lines.append("")

    if stats.get("untouched_top3"):
        lines.append("Top untouched:")
        for r in stats["untouched_top3"]:
            who = r.get("contact_email") or r.get("contact_phone") or r.get("business_name") or "(no contact)"
            age = (datetime.now(timezone.utc) - r["created_at"]).days
            lines.append(f"- {r['primary_channel']} — {r.get('business_name') or r.get('contact_name') or '(unknown)'} — {who} — {age}d ago")
        lines.append("")

    if stats.get("qualified_24h"):
        lines.append("Qualified in last 24h:")
        for r in stats["qualified_24h"]:
            who = r.get("contact_email") or r.get("business_name") or "(no contact)"
            lines.append(f"- {r['primary_channel']} — {r.get('business_name') or who}")
        lines.append("")

    lines.append("View pipeline: https://api.callmeie.ie/admin (Leads tab)")
    lines.append("")
    lines.append(f"Total leads in system: {stats.get('total_leads', 0)}")
    return subject, "\n".join(lines)


def _format_telegram_ping(stats: dict, subject: str) -> str:
    sc = stats.get("status_changes_24h", {})
    parts = [f"[rollup] {sc.get('new', 0)} new"]
    if sc.get("qualified"):
        parts.append(f"{sc['qualified']} qualified")
    if sc.get("closed_won"):
        parts.append(f"{sc['closed_won']} won")
    if stats.get("untouched_top3"):
        top = stats["untouched_top3"][0]
        who = top.get("contact_email") or top.get("business_name") or "?"
        parts.append(f"top: {who}")
    return " · ".join(parts)


def _send_resend(subject: str, body: str) -> bool:
    if not RESEND_API_KEY:
        print("[rollup] RESEND_API_KEY not set; skip email")
        return False
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps({
                "from": ROLLUP_FROM,
                "to": [ROLLUP_TO],
                "subject": subject,
                "text": body,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            ok = 200 <= r.status < 300
            print(f"[rollup] resend status={r.status}")
            return ok
    except Exception as e:
        print(f"[rollup] resend failed: {e}")
        return False


def _send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_OWNER_CHAT_ID:
        return False
    try:
        import urllib.parse
        import urllib.request
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_OWNER_CHAT_ID,
            "text": text[:4000],
        }).encode("utf-8")
        with urllib.request.urlopen(url, data=data, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[rollup] telegram failed: {e}")
        return False


def _record_run(conn, status: str, summary: dict, error: str | None = None) -> None:
    conn.execute("""
        INSERT INTO cron_runs (job_name, last_run_at, last_status, last_error, last_summary)
        VALUES ('morning_rollup', NOW(), %s, %s, %s::jsonb)
        ON CONFLICT (job_name) DO UPDATE
          SET last_run_at = NOW(), last_status = EXCLUDED.last_status,
              last_error = EXCLUDED.last_error, last_summary = EXCLUDED.last_summary
    """, (status, error, json.dumps(summary, default=str)))
    conn.commit()


def run() -> int:
    """Returns POSIX exit code (0 ok, non-zero error)."""
    conn = _connect()
    try:
        if not _check_cooldown(conn):
            return 0

        stats = _gather(conn)
        subject, body = _format_email(stats)
        ping = _format_telegram_ping(stats, subject)

        if DRY_RUN:
            print("=== DRY-RUN ===")
            print(f"Subject: {subject}")
            print(body)
            print(f"---\nTelegram ping: {ping}")
            return 0

        email_ok = _send_resend(subject, body)
        # If Resend failed, fall back to full Telegram (chunked)
        if not email_ok and TELEGRAM_BOT_TOKEN:
            _send_telegram(f"ROLLUP (resend-fail)\n{body[:3500]}")
        else:
            _send_telegram(ping)

        # Record run
        summary = {
            "new": stats.get("status_changes_24h", {}).get("new", 0),
            "qualified": stats.get("status_changes_24h", {}).get("qualified", 0),
            "closed_won": stats.get("status_changes_24h", {}).get("closed_won", 0),
            "email_sent": email_ok,
            "telegram_sent": bool(TELEGRAM_BOT_TOKEN),
        }
        _record_run(conn, "ok" if email_ok else "partial", summary, None if email_ok else "email_send_failed")
        return 0 if email_ok else 1
    except Exception as e:
        print(f"[rollup] FAILED: {e}", file=sys.stderr)
        try:
            _record_run(conn, "error", {}, str(e)[:500])
        except Exception:
            pass
        return 2
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.parse_args()  # consumed via global flags
    sys.exit(run())
