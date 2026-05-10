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

# Standard Ireland targeting — countries=IE, ages 25-65
_BASE_IE_TARGETING = {
    "geo_locations": {"countries": ["IE"]},
    "age_min": 25,
    "age_max": 65,
    "publisher_platforms": ["facebook", "instagram"],
    "facebook_positions": ["feed", "marketplace"],
    "instagram_positions": ["stream", "explore"],
    "device_platforms": ["mobile", "desktop"],
}


def _merge_targeting(extra_interests: list[dict] | None = None) -> dict:
    """Merge base IE targeting with service-specific interests."""
    base = dict(_BASE_IE_TARGETING)
    base["geo_locations"] = dict(base["geo_locations"])
    if extra_interests:
        base["flexible_spec"] = [{"interests": extra_interests}]
    return base


TEMPLATES: dict[str, dict] = {
    # --------------------------------------------------------------------
    # Receptionist — AI phone answering for Irish SMBs
    # --------------------------------------------------------------------
    "receptionist": {
        "campaign_name": "CallMeIE Receptionist — IE — TBD",
        "adset_name": "Receptionist — IE — SMB owners",
        "ad_name": "Receptionist — Headline test 1",
        "creative_name": "Receptionist creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "headline": "AI receptionist that rings back",
        "body": ("Missed calls cost work. Claire answers in your voice, "
                 "books the diary, texts you the lead. Sound? €149 setup, "
                 "€297/month. Ireland-based. No setup fee for first 10."),
        "link_url": "https://callmeie.ie/receptionist/",
        "cta_type": "LEARN_MORE",
        "targeting": _merge_targeting([
            {"id": "6003020834693", "name": "Small business"},
            {"id": "6003277229371", "name": "Entrepreneurship"},
        ]),
    },

    # --------------------------------------------------------------------
    # Doc Ops — SOPs / contracts / invoicing automation
    # --------------------------------------------------------------------
    "docops": {
        "campaign_name": "CallMeIE Doc Ops — IE — TBD",
        "adset_name": "Doc Ops — IE — Operations leads",
        "ad_name": "Doc Ops — Headline test 1",
        "creative_name": "Doc Ops creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "headline": "Stop chasing paperwork",
        "body": ("AI extracts your invoices, contracts, SOPs into the "
                 "systems you already use. €99 audit shows what to "
                 "automate. From €249/month full handling."),
        "link_url": "https://callmeie.ie/docs/",
        "cta_type": "LEARN_MORE",
        "targeting": _merge_targeting([
            {"id": "6003020834693", "name": "Small business"},
            {"id": "6003397425735", "name": "Business administration"},
        ]),
    },

    # --------------------------------------------------------------------
    # Websites — AI-first sites for trades + restaurants
    # --------------------------------------------------------------------
    "websites": {
        "campaign_name": "CallMeIE Websites — IE — TBD",
        "adset_name": "Websites — IE — Trades & hospitality",
        "ad_name": "Websites — Headline test 1",
        "creative_name": "Websites creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "headline": "Website that books work for you",
        "body": ("Plumbers, electricians, restaurants — AI-first site "
                 "that takes the call, books the diary, captures the "
                 "lead. From €695. Care plans from €45/month."),
        "link_url": "https://callmeie.ie/websites/",
        "cta_type": "LEARN_MORE",
        "targeting": _merge_targeting([
            {"id": "6003020834693", "name": "Small business"},
            {"id": "6003522631307", "name": "Restaurant"},
        ]),
    },

    # --------------------------------------------------------------------
    # Audit — €99 ops + tech audit funnel (lead magnet)
    # --------------------------------------------------------------------
    "audit": {
        "campaign_name": "CallMeIE Audit — IE — TBD",
        "adset_name": "Audit — IE — Owner-operators",
        "ad_name": "Audit — Headline test 1",
        "creative_name": "Audit creative v1",
        "objective": "OUTCOME_LEADS",
        "optimization_goal": "LEAD_GENERATION",
        "billing_event": "IMPRESSIONS",
        "headline": "€99 ops audit — see what's leaking",
        "body": ("60-minute review. We map your phone, paperwork and "
                 "site. You get a 1-page punch list with the cheapest "
                 "fixes first. Apply against any package later."),
        "link_url": "https://callmeie.ie/local-seo/",
        "cta_type": "GET_QUOTE",
        "targeting": _merge_targeting([
            {"id": "6003020834693", "name": "Small business"},
            {"id": "6003397425735", "name": "Business administration"},
        ]),
    },
}


def list_template_keys() -> list[str]:
    return list(TEMPLATES.keys())


def get_template(key: str) -> dict:
    if key not in TEMPLATES:
        raise ValueError(f"unknown template: {key}")
    return dict(TEMPLATES[key])
