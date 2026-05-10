"""meta_ad_templates.py — pre-filled campaign templates per CallMeIE service.

Phase B uses these to bootstrap a campaign+adset+creative+ad triple from a
single dropdown choice. Adam can override individual fields per-launch.

Targeting rationale: Ireland-only, ages 25-65, English language. Each
template adds business-owner-leaning interests (small business management,
entrepreneurship) plus service-specific interests.

Copy is plain-spoken Irish-friendly per brand voice: "ring", "diary",
"sound", "grand". Anti-overpromise. Each link points to the live product
page on callmeie.ie.

Budget: every template inherits daily budget from caller. NEVER hardcoded.
Hardcap enforced at meta_ads._enforce_budget_cap.
"""
from __future__ import annotations

# Standard Ireland targeting — countries=IE, ages 25-65.
# Detailed-interest IDs intentionally OMITTED — Meta is deprecating these in
# 2026 (privacy push) and Advantage+ Audience broad targeting consistently
# outperforms hand-picked interests for service-business lead gen. Audience
# discovery happens via creative + landing page, not interest IDs.
_BASE_IE_TARGETING = {
    "geo_locations": {"countries": ["IE"]},
    "age_min": 25,
    "age_max": 65,
    "publisher_platforms": ["facebook", "instagram"],
    "facebook_positions": ["feed", "marketplace"],
    "instagram_positions": ["stream", "explore"],
    "device_platforms": ["mobile", "desktop"],
    # targeting_automation lets Meta find the audience — Advantage+ broad
    "targeting_automation": {"advantage_audience": 1},
}


def _merge_targeting(extra_interests: list[dict] | None = None) -> dict:
    """Return base IE targeting. extra_interests param kept for API compat
    but ignored — see comment on _BASE_IE_TARGETING about 2026 dep ladder."""
    base = dict(_BASE_IE_TARGETING)
    base["geo_locations"] = dict(base["geo_locations"])
    return base


_AD_IMG_DIR = "scripts/ad-images"  # repo-relative; resolved at upload time


TEMPLATES: dict[str, dict] = {
    # --------------------------------------------------------------------
    # Receptionist — AI phone answering for Irish SMBs
    # --------------------------------------------------------------------
    "receptionist": {
        "campaign_name": "CallMeIE Receptionist — IE — TBD",
        "adset_name": "Receptionist — IE — SMB owners",
        "ad_name": "Receptionist — Headline v1",
        "creative_name": "Receptionist creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        # Headline ≤ 7 words (2026 best practice — fits before mobile truncation)
        "headline": "Never miss a call again",
        # First 125 chars must hold (before "see more"): the value prop
        "body": ("Claire answers in your voice, books the diary, texts you the lead. "
                 "€149 setup + €297/month. Limerick-based."),
        "link_url": "https://callmeie.ie/receptionist/?utm_source=meta&utm_medium=paid&utm_campaign=receptionist",
        # CTA matches local service intent (research: CONTACT_US > LEARN_MORE for service)
        "cta_type": "CONTACT_US",
        "image_square": f"{_AD_IMG_DIR}/receptionist-1080.jpg",
        "image_link": f"{_AD_IMG_DIR}/receptionist-1200x628.jpg",
        "targeting": _merge_targeting(),
    },

    # --------------------------------------------------------------------
    # Doc Ops — invoice/VAT extraction + handling
    # --------------------------------------------------------------------
    "docops": {
        "campaign_name": "CallMeIE Doc Ops — IE — TBD",
        "adset_name": "Doc Ops — IE — Operations leads",
        "ad_name": "Doc Ops — Headline v1",
        "creative_name": "Doc Ops creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "headline": "Stop chasing paperwork",
        "body": ("AI lifts invoices and VAT receipts into the systems you already use. "
                 "€99 audit shows what to automate first."),
        "link_url": "https://callmeie.ie/docs/?utm_source=meta&utm_medium=paid&utm_campaign=docops",
        "cta_type": "CONTACT_US",
        "image_square": f"{_AD_IMG_DIR}/docops-1080.jpg",
        "image_link": f"{_AD_IMG_DIR}/docops-1200x628.jpg",
        "targeting": _merge_targeting(),
    },

    # --------------------------------------------------------------------
    # Websites — AI-first sites for trades + hospitality
    # --------------------------------------------------------------------
    "websites": {
        "campaign_name": "CallMeIE Websites — IE — TBD",
        "adset_name": "Websites — IE — Trades & hospitality",
        "ad_name": "Websites — Headline v1",
        "creative_name": "Websites creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "headline": "Website that books your work",
        "body": ("AI-first site that takes the call, books the diary, captures the lead. "
                 "Plumbers, electricians, restaurants. From €695."),
        "link_url": "https://callmeie.ie/websites/?utm_source=meta&utm_medium=paid&utm_campaign=websites",
        "cta_type": "CONTACT_US",
        "image_square": f"{_AD_IMG_DIR}/websites-1080.jpg",
        "image_link": f"{_AD_IMG_DIR}/websites-1200x628.jpg",
        "targeting": _merge_targeting(),
    },

    # --------------------------------------------------------------------
    # Audit — €99 ops + tech audit (lead magnet)
    # --------------------------------------------------------------------
    "audit": {
        "campaign_name": "CallMeIE Audit — IE — TBD",
        "adset_name": "Audit — IE — Owner-operators",
        "ad_name": "Audit — Headline v1",
        "creative_name": "Audit creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "headline": "€99 audit — see what's leaking",
        "body": ("60 minutes. We map your phone, paperwork and site. "
                 "You get a 1-page punch list. Cheapest fixes first."),
        "link_url": "https://callmeie.ie/local-seo/?utm_source=meta&utm_medium=paid&utm_campaign=audit",
        "cta_type": "GET_QUOTE",
        "image_square": f"{_AD_IMG_DIR}/audit-1080.jpg",
        "image_link": f"{_AD_IMG_DIR}/audit-1200x628.jpg",
        "targeting": _merge_targeting(),
    },
}


def list_template_keys() -> list[str]:
    return list(TEMPLATES.keys())


def get_template(key: str) -> dict:
    if key not in TEMPLATES:
        raise ValueError(f"unknown template: {key}")
    return dict(TEMPLATES[key])
