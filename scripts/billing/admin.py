"""AUD-038 — admin endpoint for triggering meter sync from external cron.

Token-gated POST endpoint. GitHub Actions (or any external scheduler)
hits this once a day; the body invocation is identical to running
``python -m billing.meter`` locally.

Why not a Render Cron Job service: Render bills per-cron-service. GitHub
Actions has free 2000 build-minutes/month — more than enough for one
30-second curl per day.
"""
from __future__ import annotations

import logging
import os
import secrets

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from billing.meter import push_pending_usage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

METER_SYNC_TOKEN = os.environ.get("METER_SYNC_TOKEN", "").strip()


def _check_token(provided: str) -> bool:
    if not METER_SYNC_TOKEN or not provided:
        return False
    # secrets.compare_digest avoids timing leaks on token comparison.
    return secrets.compare_digest(provided, METER_SYNC_TOKEN)


@router.post("/meter/sync")
def trigger_meter_sync(
    x_meter_sync_token: str = Header(default=""),
) -> JSONResponse:
    """Run one nightly meter sync. Returns counters {pushed,failed,skipped}.

    Header: ``X-Meter-Sync-Token: <token>`` — must match the
    ``METER_SYNC_TOKEN`` env var set on the deploy.
    """
    if not METER_SYNC_TOKEN:
        # Fail-loud — token must be set explicitly. Prevents anyone from
        # discovering an open admin endpoint on a misconfigured deploy.
        raise HTTPException(status_code=503, detail="meter sync not configured")
    if not _check_token(x_meter_sync_token):
        raise HTTPException(status_code=401, detail="invalid token")

    result = push_pending_usage()
    logger.info("meter sync via admin endpoint: %s", result)
    return JSONResponse({"ok": True, "result": result})


__all__ = ["router"]
