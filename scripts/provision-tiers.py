"""AUD-021 — one-shot Stripe Price provisioner for AI Agency tier ladder.

After Adam picks 3-tier (current PRICING-STRATEGY) or 4-tier (PDR-P1-STRAT),
run with --ladder to mint the Stripe products/prices that pair the
recurring base fee with the metered overage component (Stripe Meter
``vapi_minutes`` = mtr_61UbGf0xrOZ6vdadl41CEqG2AuI1zWoq).

Idempotent — re-run safe. Existing products with the same lookup_key are
reused; only missing prices get created.

USAGE
-----

    # 3-tier (Basic/Pro/Max — current canonical PRICING-STRATEGY)
    python scripts/provision-tiers.py --ladder 3tier

    # 4-tier (Starter/Growth/Pro/Concierge — PDR-P1-STRAT, Concierge anchor)
    python scripts/provision-tiers.py --ladder 4tier

    # Dry-run print
    python scripts/provision-tiers.py --ladder 3tier --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

BASE = "https://api.stripe.com/v1"
TAG = "ai-agency"
METER_ID = "mtr_61UbGf0xrOZ6vdadl41CEqG2AuI1zWoq"
METER_NAME = "vapi_minutes"

# Ladder definitions. Each tier: lookup_key, name, description, base price
# (monthly + yearly), included minutes, overage rate (cents per minute).
LADDERS: dict[str, list[dict[str, Any]]] = {
    "3tier": [
        {
            "key": "tier-basic",
            "name": "CallMeIE · Basic",
            "description": "AI receptionist, ~500 calls/month, GoogleCalendar booking, SMS confirms.",
            "base_monthly": 14900,
            "base_yearly": 149000,
            "included_minutes": 500,
            "overage_cents_per_minute": 18,  # €0.18/min
        },
        {
            "key": "tier-pro",
            "name": "CallMeIE · Pro",
            "description": "Everything in Basic + ~1,000 calls/month, multi-line support.",
            "base_monthly": 29900,
            "base_yearly": 299000,
            "included_minutes": 1000,
            "overage_cents_per_minute": 18,
        },
        {
            "key": "tier-max",
            "name": "CallMeIE · Max",
            "description": "Everything in Pro + ~4,000 calls/month, multi-location, custom voice, dedicated CSM.",
            "base_monthly": 150000,
            "base_yearly": 1500000,
            "included_minutes": 4000,
            "overage_cents_per_minute": 15,
        },
    ],
    "4tier": [
        {
            "key": "tier-starter",
            "name": "CallMeIE · Starter",
            "description": "AI receptionist, 200 included minutes/month.",
            "base_monthly": 9900,
            "base_yearly": 99000,
            "included_minutes": 200,
            "overage_cents_per_minute": 20,
        },
        {
            "key": "tier-growth",
            "name": "CallMeIE · Growth",
            "description": "AI receptionist, 800 included minutes/month, multi-line.",
            "base_monthly": 29900,
            "base_yearly": 299000,
            "included_minutes": 800,
            "overage_cents_per_minute": 18,
        },
        {
            "key": "tier-pro",
            "name": "CallMeIE · Pro",
            "description": "AI receptionist, 2000 included minutes/month, custom voice.",
            "base_monthly": 69900,
            "base_yearly": 699000,
            "included_minutes": 2000,
            "overage_cents_per_minute": 15,
        },
        {
            "key": "tier-concierge",
            "name": "CallMeIE · Concierge",
            "description": "Unlimited minutes, multi-location, dedicated success manager (anchor tier).",
            "base_monthly": 150000,
            "base_yearly": 1500000,
            "included_minutes": 999999,
            "overage_cents_per_minute": 10,
        },
    ],
}


def api(key: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {key}"},
        timeout=20.0,
    )


def find_product(client: httpx.Client, lookup_key: str) -> dict[str, Any] | None:
    """Search products by metadata lookup_key tag."""
    r = client.get("/products/search", params={
        "query": f"metadata['lookup_key']:'{lookup_key}'",
        "limit": 1,
    })
    if r.status_code != 200:
        return None
    data = r.json().get("data", [])
    return data[0] if data else None


def ensure_product(client: httpx.Client, spec: dict[str, Any]) -> dict[str, Any]:
    existing = find_product(client, spec["key"])
    if existing:
        print(f"  [exists]  product   {spec['key']:<22s}  {existing['id']}")
        return existing
    r = client.post("/products", data={
        "name": spec["name"],
        "description": spec["description"],
        "metadata[lookup_key]": spec["key"],
        "metadata[owl_tag]": TAG,
        "metadata[included_minutes]": str(spec["included_minutes"]),
        "tax_code": "txcd_10000000",  # software-as-a-service
    })
    if r.status_code != 200:
        raise SystemExit(f"product create failed: {r.text}")
    p = r.json()
    print(f"  [created] product   {spec['key']:<22s}  {p['id']}")
    return p


def find_price(client: httpx.Client, lookup_key: str) -> dict[str, Any] | None:
    r = client.get("/prices", params={
        "lookup_keys[]": lookup_key,
        "limit": 1,
        "active": True,
    })
    if r.status_code != 200:
        return None
    data = r.json().get("data", [])
    return data[0] if data else None


def ensure_base_price(
    client: httpx.Client,
    product_id: str,
    spec: dict[str, Any],
    interval: str,
) -> dict[str, Any]:
    """Create the recurring base fee (flat monthly/yearly licensed price)."""
    amount = spec["base_monthly"] if interval == "month" else spec["base_yearly"]
    lookup_key = f"{spec['key']}-{interval}ly"
    existing = find_price(client, lookup_key)
    if existing:
        print(f"  [exists]  base      {lookup_key:<28s}  {existing['id']}  €{amount/100:.2f}")
        return existing
    r = client.post("/prices", data={
        "product": product_id,
        "currency": "eur",
        "unit_amount": str(amount),
        "lookup_key": lookup_key,
        "recurring[interval]": interval,
        "recurring[usage_type]": "licensed",
        "tax_behavior": "exclusive",
        "metadata[role]": "base",
    })
    if r.status_code != 200:
        raise SystemExit(f"base price create failed for {lookup_key}: {r.text}")
    p = r.json()
    print(f"  [created] base      {lookup_key:<28s}  {p['id']}  €{amount/100:.2f}")
    return p


def ensure_meter_overage_price(
    client: httpx.Client,
    product_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Create the metered overage price tied to the vapi_minutes Meter."""
    lookup_key = f"{spec['key']}-overage"
    existing = find_price(client, lookup_key)
    if existing:
        print(
            f"  [exists]  overage   {lookup_key:<28s}  {existing['id']}  "
            f"€{spec['overage_cents_per_minute']/100:.2f}/min"
        )
        return existing
    r = client.post("/prices", data={
        "product": product_id,
        "currency": "eur",
        "unit_amount": str(spec["overage_cents_per_minute"]),
        "lookup_key": lookup_key,
        "recurring[interval]": "month",
        "recurring[usage_type]": "metered",
        "recurring[meter]": METER_ID,
        "tax_behavior": "exclusive",
        "metadata[role]": "overage",
        "metadata[included_minutes]": str(spec["included_minutes"]),
    })
    if r.status_code != 200:
        raise SystemExit(f"overage price create failed for {lookup_key}: {r.text}")
    p = r.json()
    print(
        f"  [created] overage   {lookup_key:<28s}  {p['id']}  "
        f"€{spec['overage_cents_per_minute']/100:.2f}/min × Meter"
    )
    return p


def main() -> int:
    parser = argparse.ArgumentParser(description="AUD-021 tier provisioner")
    parser.add_argument("--ladder", required=True, choices=sorted(LADDERS.keys()))
    parser.add_argument("--key", help="Stripe secret key (else STRIPE_API env)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    key = args.key or os.environ.get("STRIPE_API") or os.environ.get("OWL_STRIPE_API", "")
    if not key:
        print("ERROR: pass --key sk_xxx or set STRIPE_API env", file=sys.stderr)
        return 2

    spec_list = LADDERS[args.ladder]

    if args.dry_run:
        print(f"=== DRY-RUN: {args.ladder} ladder ===")
        for s in spec_list:
            print(
                f"  {s['key']:<18s}  €{s['base_monthly']/100:.0f}/mo  "
                f"€{s['base_yearly']/100:.0f}/yr  {s['included_minutes']} min incl  "
                f"€{s['overage_cents_per_minute']/100:.2f}/min over"
            )
        return 0

    print(f"=== Provisioning {args.ladder} ladder ===")
    print(f"Meter: {METER_NAME} ({METER_ID})\n")

    results = {"products": {}, "base_prices": {}, "overage_prices": {}}
    with api(key) as client:
        for spec in spec_list:
            print(f"-- {spec['key']} --")
            product = ensure_product(client, spec)
            results["products"][spec["key"]] = product["id"]
            for interval in ("month", "year"):
                price = ensure_base_price(client, product["id"], spec, interval)
                results["base_prices"][f"{spec['key']}-{interval}ly"] = price["id"]
            overage = ensure_meter_overage_price(client, product["id"], spec)
            results["overage_prices"][f"{spec['key']}-overage"] = overage["id"]
            print()

    print("\n=== summary ===")
    print(json.dumps(results, indent=2))
    print("\nNext: in Stripe Dashboard, build a Subscription with the base price")
    print("plus the matching overage price as a separate line item; or use")
    print("Checkout Session line_items[] with both price ids.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
