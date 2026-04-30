"""AUD-038 — Stripe Billing Meter sync.

Aggregates pending usage_records and pushes one ``billing.MeterEvent.create``
per record. Stripe folds overage into the next subscription invoice
automatically. Idempotent at three layers:

1. SQLite: ``pushed_to_stripe = 0`` filter — only pending rows.
2. Stripe edge: ``identifier=vapi-{call_id}`` — Stripe rejects duplicate
   identifiers within the meter's dedupe window (24h).
3. Local row state: on success, write ``pushed_to_stripe = 1`` + the
   returned identifier.

Run via cron at 02:15 Europe/Dublin (after Vapi has settled all
end-of-call reports for the prior day):

    cd /opt/ai-agency && python -m billing.meter
"""
from __future__ import annotations

import logging
import os
import time

import stripe

from billing.db import get_db

logger = logging.getLogger(__name__)

# Read at module load so cron-spawned processes pick up env. The
# .strip() defends against trailing newlines in Render-style env vars.
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()

# Stripe Meter ``event_name`` configured in Dashboard. One meter per
# dimension — minutes is the obvious one for per-call billing.
METER_EVENT_NAME = os.environ.get("STRIPE_METER_NAME", "vapi_minutes").strip()


def _seconds_to_minutes(secs: int) -> int:
    """Round UP to the nearest minute — telephony convention."""
    if secs <= 0:
        return 0
    return (secs + 59) // 60


def push_pending_usage(*, batch_limit: int = 5000) -> dict[str, int]:
    """Push every pending usage record. Returns {pushed, failed, skipped}."""
    if not stripe.api_key:
        logger.warning("STRIPE_SECRET_KEY missing — meter sync disabled")
        return {"pushed": 0, "failed": 0, "skipped": 0, "no_key": 1}

    pushed = 0
    failed = 0
    skipped = 0

    with get_db() as conn:
        rows = conn.execute(
            """SELECT u.id, u.vapi_call_id, u.duration_seconds, u.created_at,
                      c.stripe_customer_id
               FROM usage_records u
               JOIN clients c ON c.id = u.client_id
               WHERE u.pushed_to_stripe = 0
               ORDER BY u.created_at ASC
               LIMIT ?""",
            (batch_limit,),
        ).fetchall()

    for r in rows:
        if not r["stripe_customer_id"]:
            skipped += 1
            continue
        minutes = _seconds_to_minutes(r["duration_seconds"])
        if minutes <= 0:
            skipped += 1
            continue

        identifier = f"vapi-{r['vapi_call_id']}"
        try:
            evt = stripe.billing.MeterEvent.create(
                event_name=METER_EVENT_NAME,
                payload={
                    "stripe_customer_id": r["stripe_customer_id"],
                    "value": str(minutes),
                },
                identifier=identifier,
                timestamp=int(time.time()),
            )
            with get_db() as conn:
                conn.execute(
                    "UPDATE usage_records SET pushed_to_stripe = 1, "
                    "stripe_meter_event_id = ?, pushed_at = datetime('now') WHERE id = ?",
                    (getattr(evt, "identifier", identifier), r["id"]),
                )
            pushed += 1
        except stripe.error.StripeError as e:
            logger.warning("meter push failed call=%s err=%s", r["vapi_call_id"], e)
            with get_db() as conn:
                conn.execute(
                    "UPDATE usage_records SET pushed_to_stripe = 2 WHERE id = ?",
                    (r["id"],),
                )
            failed += 1
        except Exception as e:
            logger.exception("unexpected meter push error call=%s err=%s", r["vapi_call_id"], e)
            failed += 1

    return {"pushed": pushed, "failed": failed, "skipped": skipped}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = push_pending_usage()
    logger.info("meter sync complete: %s", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["push_pending_usage", "_seconds_to_minutes", "METER_EVENT_NAME"]
