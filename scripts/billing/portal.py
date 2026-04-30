"""AUD-038 — Stripe Customer Portal endpoint.

Mints a short-lived portal session URL for the client whose
``admin_token`` matches. Same auth pattern as Owl Studio's per-site
admin_token (callmeie-fix/scripts/server.py:_owl_admin_lookup).

Security model:
  - Server-side token-to-customer lookup. Never accept ``customer_id``
    from the request — bind from authenticated client row only.
  - Token is the long-lived per-client admin secret. Rotation flips the
    column in the ``clients`` table.
"""
from __future__ import annotations

import os

import stripe
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from billing.db import get_db

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()

PORTAL_RETURN_URL = os.environ.get(
    "BILLING_PORTAL_RETURN_URL",
    "https://callmeie.ie/account",
).strip()


@router.post("/portal")
async def create_portal_session(token: str = Query("")) -> JSONResponse:
    """Create a Stripe Customer Portal session for the requesting client.

    Returns 503 if Stripe is not configured (key missing).
    Returns 401 if no token provided.
    Returns 404 if no active client matches the token, or the client has
    no Stripe customer linked.
    Returns 200 ``{url}`` on success.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="stripe not configured")
    if not token:
        raise HTTPException(status_code=401, detail="token required")

    with get_db() as conn:
        client = conn.execute(
            "SELECT id, display_name, stripe_customer_id "
            "FROM clients WHERE admin_token = ? AND status = 'active'",
            (token,),
        ).fetchone()
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    if not client["stripe_customer_id"]:
        raise HTTPException(status_code=404, detail="no stripe customer")

    portal = stripe.billing_portal.Session.create(
        customer=client["stripe_customer_id"],
        return_url=f"{PORTAL_RETURN_URL}?cid={client['id']}",
    )
    return JSONResponse({"url": portal.url})


__all__ = ["router"]
