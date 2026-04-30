"""
One-shot Stripe provisioner for Owl Studio.

Creates (idempotent — safe to re-run):
  - 4 Products:      Audit · Essential Care · Growth Care · Concierge Care
  - 7 Prices:        Audit €99 (one-off) + each care tier monthly + yearly
  - 7 Payment Links: hosted Stripe checkout URLs for each
  - 1 Webhook endpoint: POST https://callmeie.onrender.com/owl/stripe/webhook
    subscribed to: checkout.session.completed, customer.subscription.*,
    invoice.payment_failed, invoice.paid

Outputs:
  - Payment Link URLs (save in vault + paste on site if desired)
  - Webhook signing secret (save to Render env as OWL_STRIPE_WEBHOOK_SECRET)

Requires STRIPE_API or OWL_STRIPE_API env var (live or test sk_*).

Usage:
  source "C:/Users/a33_s/.claude/routes/.env"
  export STRIPE_API  # use whatever key name lives in your env
  python scripts/provision-stripe.py
  # or
  python scripts/provision-stripe.py --key sk_live_xxx
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

BASE = "https://api.stripe.com/v1"

# Identifier put in metadata so re-runs find existing items by lookup_key / metadata.
TAG = "owl-studio"

# Source of truth — everything that Stripe needs to know.
CATALOGUE = [
    {
        "key": "audit",
        "name": "Owl Studio · €99 style-match audit",
        "description": "One-page written design direction + quote within 48 hours. €99 credited against any project booked within 30 days.",
        "prices": [
            {"key": "audit-once", "amount": 9900, "interval": None},
        ],
    },
    {
        "key": "care-essential",
        "name": "Owl Studio · Essential care plan",
        "description": "Hosting, SSL, backups, monitoring + 30 min edits/month. 30-day cancellation.",
        "prices": [
            {"key": "care-essential-monthly", "amount": 4500, "interval": "month"},
            {"key": "care-essential-yearly",  "amount": 45000, "interval": "year"},
        ],
    },
    {
        "key": "care-growth",
        "name": "Owl Studio · Growth care plan",
        "description": "Everything in Essential + 2 hours edits/month, quarterly SEO + speed review, monthly 1-page report. 30-day cancellation.",
        "prices": [
            {"key": "care-growth-monthly", "amount": 9500, "interval": "month"},
            {"key": "care-growth-yearly",  "amount": 95000, "interval": "year"},
        ],
    },
    {
        "key": "care-concierge",
        "name": "Owl Studio · Concierge care plan",
        "description": "Everything in Growth + full content hands-off: we draft + publish blog posts, GBP posts, form triage, monthly 20-min planning call.",
        "prices": [
            {"key": "care-concierge-monthly", "amount": 19500, "interval": "month"},
            {"key": "care-concierge-yearly",  "amount": 195000, "interval": "year"},
        ],
    },
    # AUD-019 — Site build deposits (50% upfront via Stripe; balance on
    # handover). Tier names + amounts match the live pricing block in
    # `interactive-gallery.html` (Starter €695 / Pro €1,595 / Custom from €2,950).
    # Custom tier: deposit pays for the €99 audit which is credited against
    # the final invoice. The Payment Link for Custom intentionally points
    # at the €99 audit Price — `audit-once` — so the prospect commits to
    # the scoping work before a full quote is issued.
    {
        "key": "site-starter-deposit",
        "name": "Owl Studio · Starter site deposit (€348)",
        "description": "50% deposit on the €695 Starter site. Balance €347 invoiced on handover, typically 7 days. Includes single landing page, mobile/tablet/desktop builds, 1 round revisions, 1 year hosting + SSL + GDPR banner.",
        "prices": [
            {"key": "site-starter-deposit", "amount": 34800, "interval": None},
        ],
    },
    {
        "key": "site-pro-deposit",
        "name": "Owl Studio · Pro site deposit (€798)",
        "description": "50% deposit on the €1,595 Pro site. Balance €797 invoiced on handover, typically 14 days. Includes 5-page site, custom adaptation, lightweight CMS, copy + GBP setup, basic SEO, 2 years hosting, 2 rounds revisions.",
        "prices": [
            {"key": "site-pro-deposit", "amount": 79800, "interval": None},
        ],
    },
]

WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
]


def api(key: str) -> httpx.Client:
    return httpx.Client(auth=(key, ""), timeout=30)


def find_product(client: httpx.Client, product_key: str) -> dict[str, Any] | None:
    """List products and find ours by metadata.owl_key."""
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


def ensure_product(client: httpx.Client, spec: dict[str, Any]) -> dict[str, Any]:
    existing = find_product(client, spec["key"])
    if existing:
        print(f"  [exists]  product  {spec['key']:20s}  {existing['id']}")
        return existing
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
    print(f"  [created] product  {spec['key']:20s}  {p['id']}")
    return p


def find_price_by_lookup(client: httpx.Client, lookup_key: str) -> dict[str, Any] | None:
    r = client.get(f"{BASE}/prices", params={"lookup_keys[]": lookup_key, "limit": 1, "active": "true"})
    r.raise_for_status()
    d = r.json()
    return d["data"][0] if d["data"] else None


def ensure_price(client: httpx.Client, product: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    existing = find_price_by_lookup(client, p["key"])
    if existing:
        print(f"  [exists]  price    {p['key']:28s}  {existing['id']}  {existing['unit_amount']/100} {existing['currency']}")
        return existing
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
    print(f"  [created] price    {p['key']:28s}  {price['id']}  {price['unit_amount']/100} {price['currency']}")
    return price


def find_payment_link(client: httpx.Client, metadata_key: str) -> dict[str, Any] | None:
    """Scan active payment links for one with matching metadata."""
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


def ensure_payment_link(client: httpx.Client, price: dict[str, Any], key: str, is_subscription: bool) -> dict[str, Any]:
    existing = find_payment_link(client, key)
    if existing:
        print(f"  [exists]  link     {key:28s}  {existing['url']}")
        return existing
    data: dict[str, Any] = {
        "line_items[0][price]": price["id"],
        "line_items[0][quantity]": 1,
        "metadata[owl_tag]": TAG,
        "metadata[owl_key]": key,
        "allow_promotion_codes": "true",
        # AUD-004 — IE-billed entity, EU VAT mandatory ≥€10k/yr B2C.
        # Stripe Tax registered for IE jurisdiction; flip automatic_tax on
        # every new payment link. Address collection becomes "required" (was
        # "auto") because tax calc needs a customer address.
        "billing_address_collection": "required",
        "automatic_tax[enabled]": "true",
        "tax_id_collection[enabled]": "true",
    }
    if not is_subscription:
        # one-off payment — customer can optionally save card
        data["after_completion[type]"] = "hosted_confirmation"
        data["after_completion[hosted_confirmation][custom_message]"] = "Thanks — the owner will email you within 24 hours to start your project."
    else:
        data["after_completion[type]"] = "hosted_confirmation"
        data["after_completion[hosted_confirmation][custom_message]"] = "Welcome to the care plan. Your dashboard access email lands in your inbox within 30 min."
    r = client.post(f"{BASE}/payment_links", data=data)
    if r.status_code >= 400:
        raise SystemExit(f"payment_link create failed: {r.text}")
    link = r.json()
    print(f"  [created] link     {key:28s}  {link['url']}")
    return link


def find_webhook_endpoint(client: httpx.Client, url: str) -> dict[str, Any] | None:
    starting_after = None
    while True:
        params: dict[str, Any] = {"limit": 100}
        if starting_after:
            params["starting_after"] = starting_after
        r = client.get(f"{BASE}/webhook_endpoints", params=params)
        r.raise_for_status()
        d = r.json()
        for w in d["data"]:
            if w.get("url") == url and w.get("status") == "enabled":
                return w
        if not d.get("has_more"):
            return None
        starting_after = d["data"][-1]["id"]


def ensure_webhook(client: httpx.Client, url: str, events: list[str]) -> tuple[dict[str, Any], str | None]:
    """Return (endpoint, secret_or_none). Secret is only returned on create — Stripe never returns it again."""
    existing = find_webhook_endpoint(client, url)
    if existing:
        print(f"  [exists]  webhook  {existing['id']}  {url}")
        return existing, None
    # httpx dict-form: repeated keys are expressed as a list value
    data = {
        "url": url,
        "metadata[owl_tag]": TAG,
        "enabled_events[]": events,
    }
    r = client.post(f"{BASE}/webhook_endpoints", data=data)
    if r.status_code >= 400:
        raise SystemExit(f"webhook create failed: {r.text}")
    w = r.json()
    secret = w.get("secret")
    print(f"  [created] webhook  {w['id']}  {url}")
    return w, secret


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="Stripe secret key. Falls back to STRIPE_API / OWL_STRIPE_API env var")
    ap.add_argument("--webhook-url", default="https://callmeie.onrender.com/owl/stripe/webhook")
    args = ap.parse_args()

    key = args.key or os.environ.get("STRIPE_API") or os.environ.get("OWL_STRIPE_API", "")
    key = key.strip()
    if not key:
        raise SystemExit("Missing Stripe key. Set STRIPE_API env var or pass --key sk_xxx")
    mode = "LIVE" if key.startswith("sk_live") else ("TEST" if key.startswith("sk_test") else "UNKNOWN")
    print(f"Stripe key: {key[:8]}...  mode={mode}\n")

    results: dict[str, Any] = {"products": {}, "prices": {}, "links": {}, "webhook": None}

    with api(key) as client:
        print("[1/4] products")
        products_by_key: dict[str, dict[str, Any]] = {}
        for spec in CATALOGUE:
            p = ensure_product(client, spec)
            products_by_key[spec["key"]] = p
            results["products"][spec["key"]] = p["id"]

        print("\n[2/4] prices")
        prices_by_key: dict[str, dict[str, Any]] = {}
        for spec in CATALOGUE:
            product = products_by_key[spec["key"]]
            for p in spec["prices"]:
                price = ensure_price(client, product, p)
                prices_by_key[p["key"]] = price
                results["prices"][p["key"]] = {"id": price["id"], "amount": price["unit_amount"], "interval": p["interval"]}

        print("\n[3/4] payment links")
        for spec in CATALOGUE:
            for p in spec["prices"]:
                link = ensure_payment_link(client, prices_by_key[p["key"]], p["key"], is_subscription=bool(p["interval"]))
                results["links"][p["key"]] = link["url"]

        print(f"\n[4/4] webhook endpoint -> {args.webhook_url}")
        wh, secret = ensure_webhook(client, args.webhook_url, WEBHOOK_EVENTS)
        results["webhook"] = {"id": wh["id"], "url": wh["url"], "secret": secret}

    print("\n" + "=" * 72)
    print("DONE")
    print("=" * 72)
    print()
    print("PAYMENT LINKS (paste these into your admin vault):\n")
    for k, url in results["links"].items():
        print(f"  {k:28s}  {url}")

    print()
    if results["webhook"]["secret"]:
        print(">>> NEW WEBHOOK SIGNING SECRET (one-time-only output from Stripe) <<<")
        print(f"OWL_STRIPE_WEBHOOK_SECRET={results['webhook']['secret']}")
        print("Save to Render env vars (Environment -> Add).")
        print("Stripe will NEVER show this value again after this run.")
    else:
        print("Webhook already existed — signing secret was generated on its first run and must be fetched from the Stripe dashboard.")
        print(f"  https://dashboard.stripe.com/webhooks/{results['webhook']['id']}")


if __name__ == "__main__":
    main()
