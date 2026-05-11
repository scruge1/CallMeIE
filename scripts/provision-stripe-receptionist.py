"""Stripe provisioner — Receptionist products (Path B per 2026-05-11 PRD).

Creates (idempotent — safe to re-run):
  - 3 Products with metadata.owl_tag=callmeie:
    - receptionist-professional  €249/mo  (Dental / Salon / Solicitor pitch tier)
    - receptionist-growth        €397/mo  (Motor Factors pitch tier)
    - receptionist-setup         €297 one-off (universal setup fee)
  - 3 Prices (lookup_keys):
    - receptionist-professional-monthly
    - receptionist-growth-monthly
    - receptionist-setup-once
  - 1 Payment Link for receptionist-setup-once (standalone — Adam can SMS this
    directly without going via Checkout Session). Monthly tiers do NOT get
    static Payment Links; they go via dynamic Checkout Sessions from the
    new /admin/api/send-setup-link endpoint (bundles tier + setup into one
    line item set per caller).

Sister script `provision-stripe.py` covers Owl Studio (owl-studio tag);
`provision-tiers.py` covers existing CallMeIE 4-tier metered ladder
(ai-agency tag). This adds the receptionist Path B parallel surface.

Per BRAND-DOMAIN-PRD §0.6: new products use `owl_tag=callmeie` for billing
separation; existing owl-studio + ai-agency products keep their tags for
continuity.

Webhook: reuses existing `/owl/stripe/webhook` endpoint (no changes needed).

Usage:
  source ~/.claude/routes/.env  # exposes STRIPE_API
  python scripts/provision-stripe-receptionist.py             # DRY-RUN
  python scripts/provision-stripe-receptionist.py --apply     # LIVE
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

BASE = "https://api.stripe.com/v1"
TAG = "callmeie"

CATALOGUE = [
    {
        "key": "receptionist-professional",
        "name": "CallMeIE Receptionist · Professional",
        "description": "AI phone receptionist for Irish small business. Answers 24/7, books appointments, texts back missed callers. Recommended tier for dental, salon, solicitor.",
        "prices": [
            {"key": "receptionist-professional-monthly", "amount": 24900, "interval": "month"},
        ],
    },
    {
        "key": "receptionist-growth",
        "name": "CallMeIE Receptionist · Growth",
        "description": "AI phone receptionist for higher-volume Irish small business. Multi-line, faster pickup, sector-specific scripts. Recommended tier for motor factors and trade suppliers.",
        "prices": [
            {"key": "receptionist-growth-monthly", "amount": 39700, "interval": "month"},
        ],
    },
    {
        "key": "receptionist-setup",
        "name": "CallMeIE Receptionist · Setup",
        "description": "One-off configuration fee: phone number provisioning, Google Calendar wiring, custom prompts per sector, voice tuning, GDPR compliance review, 30-min go-live call.",
        "prices": [
            {"key": "receptionist-setup-once", "amount": 29700, "interval": None},
        ],
    },
]

PAYMENT_LINK_KEYS = ["receptionist-setup-once"]  # only static link — monthlies via Checkout Session


def api(key: str) -> httpx.Client:
    return httpx.Client(auth=(key, ""), timeout=30)


def find_product(client: httpx.Client, product_key: str) -> dict[str, Any] | None:
    starting_after = None
    while True:
        params: dict[str, Any] = {"limit": 100, "active": "true"}
        if starting_after:
            params["starting_after"] = starting_after
        r = client.get(f"{BASE}/products", params=params)
        r.raise_for_status()
        d = r.json()
        for p in d["data"]:
            if p.get("metadata", {}).get("owl_key") == product_key:
                return p
        if not d.get("has_more"):
            return None
        starting_after = d["data"][-1]["id"]


def ensure_product(client: httpx.Client, spec: dict[str, Any], dry: bool) -> dict[str, Any]:
    existing = find_product(client, spec["key"])
    if existing:
        print(f"  [exists]  product  {spec['key']:32s}  {existing['id']}")
        return existing
    if dry:
        print(f"  [DRY-RUN] would create product  {spec['key']:32s}  name={spec['name']}")
        return {"id": "prod_DRY_RUN", "name": spec["name"]}
    r = client.post(
        f"{BASE}/products",
        data={
            "name": spec["name"],
            "description": spec["description"],
            "metadata[owl_tag]": TAG,
            "metadata[owl_key]": spec["key"],
        },
    )
    if r.status_code >= 400:
        raise SystemExit(f"product create failed: {r.text}")
    p = r.json()
    print(f"  [created] product  {spec['key']:32s}  {p['id']}")
    return p


def find_price_by_lookup(client: httpx.Client, lookup_key: str) -> dict[str, Any] | None:
    r = client.get(f"{BASE}/prices", params={"lookup_keys[]": lookup_key, "limit": 1, "active": "true"})
    r.raise_for_status()
    d = r.json()
    return d["data"][0] if d["data"] else None


def ensure_price(client: httpx.Client, product: dict[str, Any], p: dict[str, Any], dry: bool) -> dict[str, Any]:
    existing = find_price_by_lookup(client, p["key"])
    if existing:
        amt = existing["unit_amount"] / 100
        print(f"  [exists]  price    {p['key']:32s}  {existing['id']}  {amt} {existing['currency']}")
        return existing
    if dry:
        amt = p["amount"] / 100
        recur = p["interval"] or "one-off"
        print(f"  [DRY-RUN] would create price    {p['key']:32s}  {amt} EUR ({recur})")
        return {"id": "price_DRY_RUN", "lookup_key": p["key"]}
    data: dict[str, Any] = {
        "product": product["id"],
        "unit_amount": p["amount"],
        "currency": "eur",
        "lookup_key": p["key"],
        "metadata[owl_tag]": TAG,
        "metadata[owl_key]": p["key"],
    }
    if p["interval"]:
        data["recurring[interval]"] = p["interval"]
    r = client.post(f"{BASE}/prices", data=data)
    if r.status_code >= 400:
        raise SystemExit(f"price create failed: {r.text}")
    price = r.json()
    print(f"  [created] price    {p['key']:32s}  {price['id']}  {price['unit_amount']/100} {price['currency']}")
    return price


def find_payment_link(client: httpx.Client, metadata_key: str) -> dict[str, Any] | None:
    starting_after = None
    while True:
        params: dict[str, Any] = {"limit": 100, "active": "true"}
        if starting_after:
            params["starting_after"] = starting_after
        r = client.get(f"{BASE}/payment_links", params=params)
        r.raise_for_status()
        d = r.json()
        for pl in d["data"]:
            if pl.get("metadata", {}).get("owl_key") == metadata_key:
                return pl
        if not d.get("has_more"):
            return None
        starting_after = d["data"][-1]["id"]


def ensure_payment_link(client: httpx.Client, price: dict[str, Any], key: str, dry: bool) -> dict[str, Any]:
    existing = find_payment_link(client, key)
    if existing:
        print(f"  [exists]  link     {key:32s}  {existing['url']}")
        return existing
    if dry:
        print(f"  [DRY-RUN] would create payment link  {key:32s}  for price={price['id']}")
        return {"id": "plink_DRY_RUN", "url": "(dry-run)"}
    data: dict[str, Any] = {
        "line_items[0][price]": price["id"],
        "line_items[0][quantity]": 1,
        "metadata[owl_tag]": TAG,
        "metadata[owl_key]": key,
        "allow_promotion_codes": "true",
        "billing_address_collection": "required",
        "automatic_tax[enabled]": "true",
        "tax_id_collection[enabled]": "true",
        "after_completion[type]": "hosted_confirmation",
        "after_completion[hosted_confirmation][custom_message]": "Thanks — your setup is paid. The onboarding email lands in your inbox within 10 minutes. If you don't see it, check spam or email hello@callmeie.ie.",
    }
    r = client.post(f"{BASE}/payment_links", data=data)
    if r.status_code >= 400:
        raise SystemExit(f"payment_link create failed: {r.text}")
    link = r.json()
    print(f"  [created] link     {key:32s}  {link['url']}")
    return link


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="Stripe secret key. Falls back to STRIPE_API / OWL_STRIPE_API")
    ap.add_argument("--apply", action="store_true", help="LIVE write to Stripe. Default = dry-run.")
    args = ap.parse_args()

    key = args.key or os.environ.get("STRIPE_API") or os.environ.get("OWL_STRIPE_API", "")
    if not key:
        env = open(os.path.expanduser("~/.claude/routes/.env")).read()
        key = next((ln.split("=", 1)[1].strip().strip('"').strip("'")
                    for ln in env.splitlines() if ln.startswith("STRIPE_API=")), "")
    key = key.strip()
    if not key:
        raise SystemExit("Missing Stripe key. Set STRIPE_API env var or pass --key sk_xxx")
    mode = "LIVE" if key.startswith("sk_live") else ("TEST" if key.startswith("sk_test") else "UNKNOWN")
    print(f"Stripe key: {key[:8]}...  mode={mode}")
    print(f"Run mode: {'LIVE' if args.apply else 'DRY-RUN'}")
    print()

    results: dict[str, Any] = {"products": {}, "prices": {}, "links": {}}

    with api(key) as client:
        print("[1/3] products")
        products_by_key: dict[str, dict[str, Any]] = {}
        for spec in CATALOGUE:
            p = ensure_product(client, spec, not args.apply)
            products_by_key[spec["key"]] = p
            results["products"][spec["key"]] = p["id"]

        print("\n[2/3] prices")
        prices_by_key: dict[str, dict[str, Any]] = {}
        for spec in CATALOGUE:
            product = products_by_key[spec["key"]]
            for p in spec["prices"]:
                price = ensure_price(client, product, p, not args.apply)
                prices_by_key[p["key"]] = price
                results["prices"][p["key"]] = {"id": price["id"], "amount": p["amount"], "interval": p["interval"]}

        print("\n[3/3] payment links (one-off only)")
        for spec in CATALOGUE:
            for p in spec["prices"]:
                if p["key"] in PAYMENT_LINK_KEYS:
                    link = ensure_payment_link(client, prices_by_key[p["key"]], p["key"], not args.apply)
                    results["links"][p["key"]] = link.get("url", "(dry-run)")

    print("\n" + "=" * 72)
    print("DONE")
    print("=" * 72)
    print()
    print("PRICE IDs (paste into vault as STRIPE_RECEPTIONIST_PRICE_* keys):")
    for k, v in results["prices"].items():
        env_key = "STRIPE_RECEPTIONIST_" + k.upper().replace("RECEPTIONIST-", "").replace("-", "_")
        print(f"  {env_key}={v['id']}")
    print()
    print("PAYMENT LINKS:")
    for k, url in results["links"].items():
        env_key = "STRIPE_RECEPTIONIST_LINK_" + k.upper().replace("RECEPTIONIST-", "").replace("-", "_")
        print(f"  {env_key}={url}")

    print()
    if not args.apply:
        print("DRY-RUN complete. Re-run with --apply to provision live.")


if __name__ == "__main__":
    main()
