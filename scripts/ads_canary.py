#!/usr/bin/env python3
"""ads_canary.py — hourly safety net for live Meta campaigns (Phase C).

For each ACTIVE campaign:
  - Read today's spend via insights
  - If spend >= 80% of META_AD_DAILY_BUDGET_MAX_USD, auto-pause
  - Send Telegram alert per pause
  - Log run to cron_runs heartbeat table (or stdout if DB not reachable)

Idempotent: pausing an already-paused campaign is a no-op. Safe to run
hourly via GH Actions or Coolify scheduled job.

Env vars (read from container):
  META_MARKETING_TOKEN, META_AD_ACCOUNT_ID, META_AD_DAILY_BUDGET_MAX_USD,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

THRESHOLD_PCT = 80.0


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def telegram(msg: str) -> None:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot or not chat:
        log("telegram skipped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return
    url = f"https://api.telegram.org/bot{bot}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": msg,
        "parse_mode": "Markdown",
    }).encode("utf-8")
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            r.read()
    except Exception as e:
        log(f"telegram send failed: {e}")


def record_heartbeat(name: str, summary: dict) -> None:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        log(f"heartbeat skipped — DATABASE_URL missing: {summary}")
        return
    try:
        import psycopg2
    except ImportError:
        log("heartbeat skipped — psycopg2 not installed")
        return
    try:
        conn = psycopg2.connect(db_url)
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cron_runs (job_name, ran_at, summary) "
                "VALUES (%s, NOW(), %s::jsonb) "
                "ON CONFLICT (job_name) DO UPDATE SET ran_at = EXCLUDED.ran_at, summary = EXCLUDED.summary",
                (name, json.dumps(summary)),
            )
        conn.close()
    except Exception as e:
        log(f"heartbeat write failed: {e}")


def main() -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import meta_ads
    except Exception as e:
        log(f"ABORT: meta_ads import failed: {e}")
        return 2

    if not meta_ads.is_configured():
        log("ABORT: meta_ads not configured (token + account_id required)")
        return 2

    try:
        rows = meta_ads.list_active_campaign_spend()
    except Exception as e:
        log(f"ABORT: list_active_campaign_spend failed: {e}")
        return 2

    paused = []
    warned = []
    for r in rows:
        if r["pct_of_cap"] >= THRESHOLD_PCT:
            try:
                meta_ads.set_campaign_status(r["id"], "PAUSED")
                paused.append(r)
                log(f"PAUSED {r['id']} {r['name']} — {r['pct_of_cap']}% of cap")
                telegram(
                    f"⚠️ *Auto-paused campaign*\n"
                    f"`{r['name']}`\n"
                    f"Spend today: ${r['spend_today']:.2f} / ${r['daily_cap_usd']:.2f} "
                    f"({r['pct_of_cap']}%)\n"
                    f"ID: `{r['id']}`"
                )
            except Exception as e:
                log(f"FAILED to pause {r['id']}: {e}")
                warned.append({**r, "pause_error": str(e)[:200]})
        elif r["pct_of_cap"] >= 50:
            warned.append(r)

    summary = {
        "active_count": len(rows),
        "paused_count": len(paused),
        "warned_count": len(warned),
        "paused_ids": [p["id"] for p in paused],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    log(f"DONE: {summary}")
    record_heartbeat("ads_canary", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
