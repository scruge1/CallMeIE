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


def list_pages() -> dict[str, Any]:
    """List FB pages the System User can access (needed to attach ads to a page)."""
    token, _ = _config()
    with httpx.Client(timeout=10) as h:
        r = h.get(f"{GRAPH_BASE}/me/accounts",
                  params={"fields": "id,name,access_token", "access_token": token})
        r.raise_for_status()
        return r.json()


def create_draft_adset(
    campaign_id: str,
    name: str,
    daily_budget_usd: float,
    targeting: dict[str, Any],
    optimization_goal: str = "LEAD_GENERATION",
    billing_event: str = "IMPRESSIONS",
) -> dict[str, Any]:
    """Phase B — adset under campaign, status=PAUSED. Hardcap enforced."""
    token, account_id = _config()
    if daily_budget_usd <= 0:
        raise ValueError("daily_budget must be > 0")
    daily_budget_cents = int(round(daily_budget_usd * 100))
    _enforce_budget_cap(daily_budget_cents)

    payload = {
        "name": name[:255],
        "campaign_id": campaign_id,
        "status": "PAUSED",
        "daily_budget": daily_budget_cents,
        "billing_event": billing_event,
        "optimization_goal": optimization_goal,
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "targeting": json.dumps(targeting),
        "access_token": token,
    }
    with httpx.Client(timeout=20) as h:
        r = h.post(f"{GRAPH_BASE}/{account_id}/adsets", data=payload)
        if r.status_code >= 400:
            return {"error": r.json(), "status_code": r.status_code}
        return r.json()


def create_draft_creative(
    page_id: str,
    name: str,
    headline: str,
    body: str,
    link_url: str,
    cta_type: str = "LEARN_MORE",
    image_hash: str | None = None,
) -> dict[str, Any]:
    """Phase B — ad creative. image_hash optional (None = text-only link spec)."""
    token, account_id = _config()
    link_data: dict[str, Any] = {
        "link": link_url,
        "message": body[:300],
        "name": headline[:40],
        "call_to_action": {"type": cta_type, "value": {"link": link_url}},
    }
    if image_hash:
        link_data["image_hash"] = image_hash

    creative_spec = {
        "page_id": page_id,
        "link_data": link_data,
    }
    payload = {
        "name": name[:255],
        "object_story_spec": json.dumps(creative_spec),
        "access_token": token,
    }
    with httpx.Client(timeout=20) as h:
        r = h.post(f"{GRAPH_BASE}/{account_id}/adcreatives", data=payload)
        if r.status_code >= 400:
            return {"error": r.json(), "status_code": r.status_code}
        return r.json()


def create_draft_ad(
    adset_id: str,
    name: str,
    creative_id: str,
) -> dict[str, Any]:
    """Phase B — ad linking creative to adset, status=PAUSED."""
    token, account_id = _config()
    payload = {
        "name": name[:255],
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": creative_id}),
        "status": "PAUSED",
        "access_token": token,
    }
    with httpx.Client(timeout=20) as h:
        r = h.post(f"{GRAPH_BASE}/{account_id}/ads", data=payload)
        if r.status_code >= 400:
            return {"error": r.json(), "status_code": r.status_code}
        return r.json()


def create_draft_triple(
    template_key: str,
    page_id: str,
    daily_budget_usd: float,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase B one-shot — campaign + adset + creative + ad, all PAUSED.

    Returns {campaign_id, adset_id, creative_id, ad_id, template_key}.
    Atomic-ish: if any step fails, returns partial state w/ error so caller
    can decide whether to clean up. No automatic rollback (Meta API is async).
    """
    from meta_ad_templates import TEMPLATES  # noqa
    if template_key not in TEMPLATES:
        raise ValueError(f"unknown template: {template_key}")
    tpl = dict(TEMPLATES[template_key])
    if overrides:
        tpl.update(overrides)

    out: dict[str, Any] = {"template_key": template_key}

    # 1. Campaign
    camp = create_draft_campaign(
        name=tpl["campaign_name"],
        objective=tpl["objective"],
        daily_budget_usd=daily_budget_usd,
    )
    if "error" in camp:
        out["error"] = {"step": "campaign", **camp}
        return out
    out["campaign_id"] = camp.get("id")

    # 2. Adset
    adset = create_draft_adset(
        campaign_id=out["campaign_id"],
        name=tpl["adset_name"],
        daily_budget_usd=daily_budget_usd,
        targeting=tpl["targeting"],
        optimization_goal=tpl.get("optimization_goal", "LEAD_GENERATION"),
        billing_event=tpl.get("billing_event", "IMPRESSIONS"),
    )
    if "error" in adset:
        out["error"] = {"step": "adset", **adset}
        return out
    out["adset_id"] = adset.get("id")

    # 3. Creative
    creative = create_draft_creative(
        page_id=page_id,
        name=tpl["creative_name"],
        headline=tpl["headline"],
        body=tpl["body"],
        link_url=tpl["link_url"],
        cta_type=tpl.get("cta_type", "LEARN_MORE"),
    )
    if "error" in creative:
        out["error"] = {"step": "creative", **creative}
        return out
    out["creative_id"] = creative.get("id")

    # 4. Ad
    ad = create_draft_ad(
        adset_id=out["adset_id"],
        name=tpl["ad_name"],
        creative_id=out["creative_id"],
    )
    if "error" in ad:
        out["error"] = {"step": "ad", **ad}
        return out
    out["ad_id"] = ad.get("id")
    return out


def get_account_spend_today() -> dict[str, Any]:
    """Phase C canary helper — total spend today for the account."""
    token, account_id = _config()
    with httpx.Client(timeout=15) as h:
        r = h.get(
            f"{GRAPH_BASE}/{account_id}/insights",
            params={
                "fields": "spend,impressions,clicks",
                "date_preset": "today",
                "access_token": token,
            },
        )
        r.raise_for_status()
        return r.json()


def list_active_campaign_spend(default_max_usd: float | None = None) -> list[dict[str, Any]]:
    """Phase C canary — for every ACTIVE campaign, return today's spend +
    pct-of-cap. default_max_usd defaults to META_AD_DAILY_BUDGET_MAX_USD.
    """
    if default_max_usd is None:
        default_max_usd, _ = _budget_caps()
    token, account_id = _config()
    fields = ("id,name,status,daily_budget,"
              "insights.date_preset(today){spend,impressions,clicks}")
    with httpx.Client(timeout=15) as h:
        r = h.get(
            f"{GRAPH_BASE}/{account_id}/campaigns",
            params={"fields": fields, "filtering": json.dumps([
                {"field": "effective_status", "operator": "IN",
                 "value": ["ACTIVE"]}
            ]), "access_token": token},
        )
        r.raise_for_status()
        data = r.json().get("data", [])

    out = []
    for c in data:
        ins = (c.get("insights") or {}).get("data") or [{}]
        spend_eur = float(ins[0].get("spend") or 0)
        camp_cap_usd = float(c.get("daily_budget") or 0) / 100 or default_max_usd
        pct = (spend_eur / camp_cap_usd * 100) if camp_cap_usd > 0 else 0
        out.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "spend_today": spend_eur,
            "daily_cap_usd": camp_cap_usd,
            "pct_of_cap": round(pct, 1),
            "should_pause": pct >= 80,
        })
    return out


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
