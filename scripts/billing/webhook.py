"""AUD-038 — Vapi end-of-call HMAC receiver.

HMAC verify pattern matches the Owl Studio gold standard handler at
``callmeie-fix/scripts/server.py:_owl_verify_stripe_sig``: timestamp + v1
header parts, ±300s replay tolerance, ``hmac.compare_digest`` comparison.

Vapi signs with HMAC-SHA512 (Stripe uses SHA-256). Two helpers, two secrets,
two distinct headers — never share a single helper across vendors.

Known caveat: Vapi sometimes ships ``end-of-call-report`` without
``durationSeconds`` — re-pull ``GET /call/{callId}`` as authoritative
fallback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from billing.db import IntegrityError, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

VAPI_WEBHOOK_SECRET = os.environ.get("VAPI_WEBHOOK_SECRET", "").strip()
VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "").strip()
TOLERANCE_SECONDS = 300


def _verify_vapi_sig(payload: bytes, sig_header: str, secret: str) -> bool:
    """Stripe-shape signature: ``t=NNNN,v1=HEX``.

    Returns False on:
      - missing secret or header
      - missing/non-int timestamp
      - replay outside ±TOLERANCE_SECONDS
      - HMAC mismatch
    """
    if not secret or not sig_header:
        return False
    parts: dict[str, str] = {}
    for p in sig_header.split(","):
        p = p.strip()
        if "=" not in p:
            continue
        k, _, v = p.partition("=")
        parts[k.strip()] = v.strip()
    t = parts.get("t", "")
    v1 = parts.get("v1", "")
    if not t or not v1:
        return False
    try:
        t_int = int(t)
    except ValueError:
        return False
    if abs(time.time() - t_int) > TOLERANCE_SECONDS:
        return False
    signed = f"{t}.{payload.decode('utf-8', errors='replace')}"
    expected = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, v1)


async def _backfill_call(call_id: str) -> dict[str, Any]:
    """Re-pull GET /call/{id} when end-of-call-report is missing fields."""
    if not VAPI_API_KEY or not call_id:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as cx:
            r = await cx.get(
                f"https://api.vapi.ai/call/{call_id}",
                headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
            )
            if r.status_code == 200:
                return r.json() or {}
    except Exception as e:
        logger.warning("vapi backfill failed call=%s err=%s", call_id, e)
    return {}


def _extract_fields(msg: dict, call: dict) -> dict[str, Any]:
    """Best-effort extract of (call_id, assistant_id, duration, ended_reason, cost)."""
    return {
        "call_id": call.get("id") or msg.get("callId") or "",
        "assistant_id": call.get("assistantId") or msg.get("assistantId") or "",
        "duration": int(msg.get("durationSeconds") or call.get("duration") or 0),
        "ended_reason": msg.get("endedReason") or "",
        "cost": float(msg.get("cost") or 0.0),
    }


@router.post("/webhook/vapi")
async def vapi_webhook(request: Request) -> JSONResponse:
    payload = await request.body()
    sig = request.headers.get("x-vapi-signature", "")
    if not _verify_vapi_sig(payload, sig, VAPI_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        event = json.loads(payload.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    msg = event.get("message") or event
    if msg.get("type") != "end-of-call-report":
        return JSONResponse({"ok": True, "ignored": msg.get("type", "?")})

    call = msg.get("call") or {}
    fields = _extract_fields(msg, call)

    if not fields["duration"] and fields["call_id"]:
        backfill = await _backfill_call(fields["call_id"])
        if backfill:
            fields["duration"] = int(
                backfill.get("duration") or backfill.get("durationSeconds") or 0
            )
            if not fields["assistant_id"]:
                fields["assistant_id"] = backfill.get("assistantId") or ""

    if not fields["call_id"] or not fields["duration"] or not fields["assistant_id"]:
        # Drop with 200 so Vapi doesn't retry forever; log for manual recon.
        logger.info("dropped vapi event missing fields: %s", fields)
        return JSONResponse({"ok": True, "dropped": True, "reason": "missing fields"})

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE vapi_assistant_id = ?",
            (fields["assistant_id"],),
        ).fetchone()
    if not row:
        return JSONResponse({"ok": True, "dropped": True, "reason": "unknown assistant"})

    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO usage_records
                   (vapi_call_id, client_id, duration_seconds, ended_reason, cost_estimate_eur)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    fields["call_id"],
                    row["id"],
                    fields["duration"],
                    fields["ended_reason"],
                    fields["cost"],
                ),
            )
    except IntegrityError:
        return JSONResponse({"ok": True, "deduped": True})

    return JSONResponse({
        "ok": True,
        "call_id": fields["call_id"],
        "seconds": fields["duration"],
    })


__all__ = ["router", "_verify_vapi_sig", "TOLERANCE_SECONDS"]
