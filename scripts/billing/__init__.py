"""AUD-038 — AI Agency metered billing MVP.

Three modules:
  - db.py       SQLite schema + connection helper
  - webhook.py  Vapi end-of-call HMAC receiver -> usage_records
  - meter.py    Nightly Stripe Meter sync
  - portal.py   Stripe Customer Portal endpoint

Environment variables:
  AGENCY_DB_PATH         Path to SQLite DB (default: ~/.ai-agency/agency.sqlite)
  VAPI_WEBHOOK_SECRET    HMAC secret shared with Vapi server URL
  VAPI_API_KEY           For backfill GET /call/{id} when fields missing
  STRIPE_SECRET_KEY      stripe-python sk_live_* / sk_test_*
  STRIPE_METER_NAME      Meter event_name (default: vapi_minutes)
"""
from billing.webhook import router as vapi_webhook_router  # noqa: F401
from billing.portal import router as portal_router  # noqa: F401

__all__ = ["vapi_webhook_router", "portal_router"]
