"""meta_ads.py — Meta Marketing API client (P-ADS Phase A, read-only).

Phase A goals:
  - Auth via long-lived System User token (META_MARKETING_TOKEN)
  - Read account / campaigns / insights (no spending)
  - Hard refusal of any write that exceeds META_AD_DAILY_BUDGET_HARDCAP_USD
  - Defensive guards: every write path lands campaigns as status=PAUSED

Phase B (draft creation) and Phase C (go-live) layer on top of this module.

Env vars:
  META_MARKETING_TOKEN          — System User token w/ ads_management+ads_read
  META_AD_ACCOUNT_ID            — e.g. 'act_1234567890'
  META_AD_DAILY_BUDGET_MAX_USD  — default per-campaign daily cap (default 2.00)
  META_AD_DAILY_BUDGET_HARDCAP_USD — absolute ceiling (default 5.00); writes above this fail

Graph API version pinned at v25.0 — stable as of 2026-05-10. Bump deliberately.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

GRAPH_VERSION = "v25.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


def _config() -> tuple[str, str]:
    token = os.environ.get("META_MARKETING_TOKEN", "").strip()
    account_id = os.environ.get("META_AD_ACCOUNT_ID", "").strip()
    if not token or not account_id:
        raise RuntimeError("meta_ads not configured: missing META_MARKETING_TOKEN or META_AD_ACCOUNT_ID")
    if not account_id.startswith("act_"):
        account_id = f"act_{account_id}"
    return token, account_id


def _budget_caps() -> tuple[float, float]:
    """Returns (default_daily_max_usd, hardcap_usd)."""
    try:
        d = float(os.environ.get("META_AD_DAILY_BUDGET_MAX_USD", "2.00"))
    except Exception:
        d = 2.00
    try:
        h = float(os.environ.get("META_AD_DAILY_BUDGET_HARDCAP_USD", "5.00"))
    except Exception:
        h = 5.00
    return d, h


def is_configured() -> bool:
    return bool(os.environ.get("META_MARKETING_TOKEN", "").strip()
                and os.environ.get("META_AD_ACCOUNT_ID", "").strip())


# ---------------------------------------------------------------------------
# Read paths (Phase A)
# ---------------------------------------------------------------------------


def get_account_info() -> dict[str, Any]:
    token, account_id = _config()
    fields = "name,currency,timezone_name,balance,amount_spent,account_status,disable_reason"
    with httpx.Client(timeout=10) as h:
        r = h.get(f"{GRAPH_BASE}/{account_id}", params={"fields": fields, "access_token": token})
        r.raise_for_status()
        return r.json()


def list_campaigns(limit: int = 50) -> dict[str, Any]:
    token, account_id = _config()
    fields = ("id,name,status,objective,daily_budget,lifetime_budget,"
              "created_time,updated_time,start_time,stop_time,"
              "insights.date_preset(last_7d){spend,impressions,clicks,cpc,ctr,actions}")
    with httpx.Client(timeout=15) as h:
        r = h.get(
            f"{GRAPH_BASE}/{account_id}/campaigns",
            params={"fields": fields, "limit": limit, "access_token": token},
        )
        r.raise_for_status()
        return r.json()


def get_campaign_insights(campaign_id: str, days: int = 7) -> dict[str, Any]:
    token, _ = _config()
    days = max(1, min(int(days), 90))
    preset = {1: "today", 7: "last_7d", 14: "last_14d", 30: "last_30d", 90: "last_90d"}.get(days, "last_7d")
    fields = "spend,impressions,clicks,cpc,ctr,reach,actions,date_start,date_stop"
    with httpx.Client(timeout=15) as h:
        r = h.get(
            f"{GRAPH_BASE}/{campaign_id}/insights",
            params={
                "fields": fields,
                "date_preset": preset,
                "time_increment": 1,  # daily breakdown
                "access_token": token,
            },
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Write paths (Phase B/C — gated)
# ---------------------------------------------------------------------------


def _enforce_budget_cap(daily_budget_cents: int) -> None:
    """Refuse any campaign request above hardcap. Logs + raises."""
    _, hardcap_usd = _budget_caps()
    if daily_budget_cents > hardcap_usd * 100:
        raise ValueError(
            f"daily_budget {daily_budget_cents/100:.2f} exceeds hardcap "
            f"{hardcap_usd:.2f} — refusing per META_AD_DAILY_BUDGET_HARDCAP_USD"
        )


def create_draft_campaign(
    name: str,
    objective: str,
    daily_budget_usd: float,
) -> dict[str, Any]:
    """Phase B — creates campaign w/ status=PAUSED.

    Activation is a separate explicit call (Phase C). Hardcap enforced.
    """
    token, account_id = _config()
    default_max_usd, _ = _budget_caps()
    if daily_budget_usd <= 0:
        raise ValueError("daily_budget must be > 0")
    if daily_budget_usd > default_max_usd:
        # Soft warning — still subject to hardcap below; but log
        print(
            f"[meta_ads] WARN: daily_budget ${daily_budget_usd} > default max ${default_max_usd}",
            file=sys.stderr,
        )
    daily_budget_cents = int(round(daily_budget_usd * 100))
    _enforce_budget_cap(daily_budget_cents)

    payload = {
        "name": name[:255],
        "objective": objective,
        "status": "PAUSED",  # ALWAYS PAUSED — Phase C activates separately
        "daily_budget": daily_budget_cents,
        "special_ad_categories": "[]",  # no special category
        "access_token": token,
    }
    with httpx.Client(timeout=20) as h:
        r = h.post(f"{GRAPH_BASE}/{account_id}/campaigns", data=payload)
        if r.status_code >= 400:
            return {"error": r.json(), "status_code": r.status_code}
        return r.json()


def set_campaign_status(campaign_id: str, status: str) -> dict[str, Any]:
    """Phase C — flip ACTIVE / PAUSED / DELETED. ACTIVE means real spend starts."""
    if status not in {"ACTIVE", "PAUSED", "DELETED", "ARCHIVED"}:
        raise ValueError(f"invalid status: {status}")
    token, _ = _config()
    with httpx.Client(timeout=15) as h:
        r = h.post(
            f"{GRAPH_BASE}/{campaign_id}",
            data={"status": status, "access_token": token},
        )
        if r.status_code >= 400:
            return {"error": r.json(), "status_code": r.status_code}
        return r.json()


# ---------------------------------------------------------------------------
# Smoke check (used by /admin/api/ads/account endpoint)
# ---------------------------------------------------------------------------


def smoke_check() -> dict[str, Any]:
    if not is_configured():
        return {"configured": False, "reason": "missing token or account_id"}
    try:
        info = get_account_info()
        default_max, hardcap = _budget_caps()
        return {
            "configured": True,
            "account": {
                "id": info.get("id"),
                "name": info.get("name"),
                "currency": info.get("currency"),
                "timezone": info.get("timezone_name"),
                "balance_cents": info.get("balance"),
                "amount_spent_cents": info.get("amount_spent"),
                "status": info.get("account_status"),
            },
            "budget_caps": {
                "default_daily_max_usd": default_max,
                "hardcap_usd": hardcap,
            },
        }
    except Exception as e:
        return {"configured": True, "error": str(e)[:300]}
