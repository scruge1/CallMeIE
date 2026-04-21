"""
One-command client onboarding for Owl Studio. Chains:

  1. POST /owl/sites               (register the site + generate admin token)
  2. POST stripe.com/v1/customers  (create Stripe customer ready for invoicing)
  3. Append admin token to ~/.claude/routes/.env under OWL_ADMIN_TOKEN_<SLUG>
  4. (Optional) Render the client site via render-site.py if profile exists
  5. Print the remaining manual-steps checklist (Uptime Kuma monitor,
     Cloudflare Pages account invite, Google Business Profile manager
     request, Vaultwarden vault creation, welcome email draft)

Usage:
  source ~/.claude/routes/.env
  export OWL_OWNER_TOKEN STRIPE_API
  python scripts/onboard-client.py \\
    --slug rathborne-dental \\
    --display "Rathborne Dental" \\
    --tier pro \\
    --care growth \\
    --email aoife@rathbornedental.ie \\
    --phone "+353 87 555 0129" \\
    --live-url https://rathbornedental.ie

Idempotent: 409 on existing site_id is treated as "already registered" and
the flow continues; existing Stripe customer is found by email.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

CALLMEIE = "https://callmeie.onrender.com"
ROUTES_ENV = Path(os.path.expanduser("~")) / ".claude" / "routes" / ".env"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent / "New repos" / "owl-studio-website-directions"


def register_site(owner_token: str, args) -> dict:
    payload = {
        "site_id": args.slug,
        "display_name": args.display,
        "tier": args.tier,
        "lead_email": args.email,
        "edit_emails": [args.email],
        "live_url": args.live_url,
    }
    if args.care:
        payload["care_tier"] = args.care
    if args.phone:
        payload["lead_sms"] = args.phone
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{CALLMEIE}/owl/sites", params={"token": owner_token}, json=payload)
        if r.status_code == 409:
            print(f"  [exists] site_id={args.slug}")
            return {"existing": True}
        if r.status_code >= 400:
            raise SystemExit(f"site registration failed {r.status_code}: {r.text}")
        j = r.json()
        print(f"  [ok] registered  admin_url={CALLMEIE}{j['admin_url']}")
        return j


def stripe_customer(stripe_key: str, args) -> dict:
    # Check for existing by email first (idempotent)
    with httpx.Client(auth=(stripe_key, ""), timeout=30) as c:
        r = c.get("https://api.stripe.com/v1/customers", params={"email": args.email, "limit": 1})
        r.raise_for_status()
        hits = r.json().get("data") or []
        if hits:
            cust = hits[0]
            print(f"  [exists] stripe customer {cust['id']} ({args.email})")
            return cust
        # Create new
        data = {"email": args.email, "name": args.display, "metadata[owl_site_id]": args.slug, "metadata[owl_tag]": "owl-studio"}
        if args.phone:
            data["phone"] = args.phone
        r = c.post("https://api.stripe.com/v1/customers", data=data)
        r.raise_for_status()
        cust = r.json()
        print(f"  [ok] created     stripe customer {cust['id']}")
        return cust


def save_token_to_vault(slug: str, admin_token: str) -> None:
    if not admin_token or admin_token == "(existing)":
        return
    key = f"OWL_ADMIN_TOKEN_{slug.upper().replace('-', '_')}"
    existing = ROUTES_ENV.read_text(encoding="utf-8") if ROUTES_ENV.is_file() else ""
    if key in existing:
        print(f"  [skip] {key} already in vault")
        return
    with ROUTES_ENV.open("a", encoding="utf-8") as f:
        f.write(f"\n# onboarded {slug}\n{key}={admin_token}\n")
    print(f"  [ok] vault updated  {key}")


def maybe_render_site(slug: str) -> None:
    profile = REPO_ROOT / "profiles" / f"{slug}.json"
    renderer = REPO_ROOT / "scripts" / "render-site.py"
    if not profile.is_file():
        print(f"  [skip] no profile at profiles/{slug}.json (stage-2 discovery will fill this)")
        return
    if not renderer.is_file():
        print(f"  [skip] renderer not found at {renderer}")
        return
    print(f"  [ok] rendering site from profile")
    try:
        subprocess.run(
            [sys.executable, str(renderer), "--profile", slug],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
        out_path = REPO_ROOT / "client-builds" / slug / "index.html"
        print(f"       -> {out_path.relative_to(REPO_ROOT)}")
    except subprocess.CalledProcessError as e:
        print(f"  [warn] render failed: {e.stderr[:200]}")


def care_payment_link(env: dict, care: str, yearly: bool = False) -> str:
    if not care:
        return ""
    suffix = "YEARLY" if yearly else "MONTHLY"
    key = f"OWL_STRIPE_LINK_CARE_{care.upper()}_{suffix}"
    return env.get(key, "(not in vault — check ~/.claude/routes/.env)")


def load_env() -> dict:
    out = {}
    if ROUTES_ENV.is_file():
        for line in ROUTES_ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def print_checklist(args, reg: dict, cust: dict, env: dict) -> None:
    bar = "=" * 72
    print(f"\n{bar}\nREMAINING MANUAL STEPS for {args.display}\n{bar}\n")
    admin_url = f"{CALLMEIE}{reg.get('admin_url', '')}" if reg.get('admin_url') else "(re-fetch from /owl/sites?token=OWNER)"
    print(f"1. Admin dashboard — share with client:\n   {admin_url}\n")
    if args.care:
        print(f"2. Care-plan payment link (send in welcome email):")
        print(f"   monthly: {care_payment_link(env, args.care, yearly=False)}")
        print(f"   yearly : {care_payment_link(env, args.care, yearly=True)}\n")
    print(f"3. Uptime Kuma monitor — open https://uptime.owlzone.trade")
    print(f"     type: HTTP(s)")
    print(f"     URL : {args.live_url}")
    print(f"     name: {args.slug}")
    print(f"     interval: 5 min")
    print(f"     alert : owner@owlzone.trade + {args.email}\n")
    print(f"4. Vaultwarden vault folder — open https://vault.owlzone.trade")
    print(f"     folder name: OwlStudio - {args.display}")
    print(f"     seed: stripe cust {cust.get('id', '?')}, admin_url above,")
    print(f"           Cloudflare/Google/domain creds as captured from client\n")
    print(f"5. Cloudflare Pages — ask client to:")
    print(f"   - sign up free at dash.cloudflare.com")
    print(f"   - invite scruge@pm.me as Pages:Edit on their account")
    print(f"   - we deploy their site from client-builds/{args.slug}/\n")
    print(f"6. Google Business Profile (if applicable) — ask client for Manager invite\n")
    print(f"7. Welcome email draft — paste into Gmail:\n")
    print(_welcome_email_body(args, admin_url, env))


def _welcome_email_body(args, admin_url: str, env: dict) -> str:
    care_line = ""
    if args.care:
        monthly = care_payment_link(env, args.care, yearly=False)
        yearly = care_payment_link(env, args.care, yearly=True)
        care_line = (
            f"\n\nYour care-plan payment links (pick one):\n"
            f"  monthly: {monthly}\n"
            f"  yearly (2 months free): {yearly}\n"
        )
    return (
        f"""   ------------------------------------------------------------------
   Subject: Welcome to Owl Studio - {args.display}

   Hi {args.display.split()[0]},

   {args.display} is registered. Your admin dashboard — bookmark it,
   rotate the token any time:

   {admin_url}

   Every contact form on your site shows up in that dashboard within
   ~2 seconds of being submitted. Forms also ping my phone via SMS
   so I see them too — handy during launch week.

   For any content edit you need (staff photo, holiday hours, a new
   service), email care@callmeie.ie with the word "{args.slug.split('-')[0].title()}" in
   the subject. Growth care plan is 2 working days turnaround.{care_line}

   Any questions, just ring.
   Owl Studio
   ------------------------------------------------------------------
"""
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--display", required=True)
    ap.add_argument("--tier", required=True, choices=["starter", "pro", "custom"])
    ap.add_argument("--care", default="", choices=["", "essential", "growth", "concierge"])
    ap.add_argument("--email", required=True)
    ap.add_argument("--phone", default="")
    ap.add_argument("--live-url", required=True, dest="live_url")
    args = ap.parse_args()

    env = load_env()
    owner = (env.get("OWL_OWNER_TOKEN") or os.environ.get("OWL_OWNER_TOKEN") or "").strip()
    stripe_key = (env.get("STRIPE_API") or os.environ.get("STRIPE_API") or "").strip()
    if not owner:
        raise SystemExit("Missing OWL_OWNER_TOKEN (in ~/.claude/routes/.env or env)")
    if not stripe_key:
        raise SystemExit("Missing STRIPE_API (in ~/.claude/routes/.env or env)")

    print(f"[1/4] registering site {args.slug}...")
    reg = register_site(owner, args)
    admin_token = reg.get("admin_token", "(existing)")

    print(f"\n[2/4] stripe customer...")
    cust = stripe_customer(stripe_key, args)

    print(f"\n[3/4] vault update...")
    save_token_to_vault(args.slug, admin_token)

    print(f"\n[4/4] optional site render...")
    maybe_render_site(args.slug)

    print_checklist(args, reg, cust, env)


if __name__ == "__main__":
    main()
