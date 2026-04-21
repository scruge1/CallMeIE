"""
One-shot provisioner for the Owl Studio backend on the callmeie
Render service.

Does all of:
  1. Finds the Render service by name
  2. GETs existing env vars, adds OWL_OWNER_TOKEN if missing (keeps everything else)
  3. PUTs the merged env vars back
  4. Triggers a deploy + polls until live
  5. Registers the first two test sites (owl-studio-sales + rathborne-dental-demo)
  6. Prints admin URLs + embed snippets for both

Requires:
  RENDER_API_KEY env var  OR  pass --key rnd_xxx

Usage:
  export RENDER_API_KEY=rnd_xxxx
  python provision-owl-backend.py
  # or
  python provision-owl-backend.py --key rnd_xxxx
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from typing import Any

import httpx

RENDER = "https://api.render.com/v1"
SERVICE_NAMES = ("CallMeIE", "ai-receptionist-server", "callmeie-receptionist")  # try each
CALLMEIE_URL = "https://callmeie.onrender.com"


def auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def find_service(key: str) -> dict[str, Any]:
    """List all services, pick the one whose URL matches the expected callmeie host
    or whose name is in SERVICE_NAMES. Robust against naming drift between
    render.yaml and the actual deployed service."""
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{RENDER}/services", headers=auth_headers(key), params={"limit": 100})
        r.raise_for_status()
        hits = r.json()

    services = [(h.get("service") or h) for h in hits]
    if not services:
        raise SystemExit("Render account has no services visible to this API key.")

    # 1. URL match (most reliable)
    want_host = CALLMEIE_URL.rstrip("/")
    for svc in services:
        details = svc.get("serviceDetails") or {}
        url = (details.get("url") or "").rstrip("/")
        if url and url == want_host:
            return svc

    # 2. Name match against known candidates
    for svc in services:
        if (svc.get("name") or "") in SERVICE_NAMES:
            return svc

    # 3. Fuzzy — anything containing 'call' or 'receptionist'
    for svc in services:
        n = (svc.get("name") or "").lower()
        if "callmeie" in n or "receptionist" in n:
            print(f"[warn] using fuzzy service match: {svc['name']}", file=sys.stderr)
            return svc

    raise SystemExit(
        "No matching Render service. Available:\n"
        + "\n".join(f"  - {s.get('name')} ({s.get('id')})" for s in services)
    )


def get_env_vars(key: str, service_id: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    with httpx.Client(timeout=30) as c:
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = c.get(f"{RENDER}/services/{service_id}/env-vars", headers=auth_headers(key), params=params)
            r.raise_for_status()
            page = r.json()
            for item in page:
                ev = item.get("envVar") or item
                out.append({"key": ev["key"], "value": ev["value"]})
            if len(page) < 100:
                break
            cursor = page[-1].get("cursor")
            if not cursor:
                break
    return out


def put_env_vars(key: str, service_id: str, pairs: list[dict[str, str]]) -> None:
    # Render PUT /env-vars replaces the whole set — we already merged upstream.
    with httpx.Client(timeout=60) as c:
        r = c.put(
            f"{RENDER}/services/{service_id}/env-vars",
            headers={**auth_headers(key), "Content-Type": "application/json"},
            json=pairs,
        )
        if r.status_code >= 400:
            raise SystemExit(f"env-vars PUT failed: {r.status_code} {r.text}")


def trigger_deploy(key: str, service_id: str) -> str:
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{RENDER}/services/{service_id}/deploys",
            headers={**auth_headers(key), "Content-Type": "application/json"},
            json={"clearCache": "do_not_clear"},
        )
        if r.status_code >= 400:
            raise SystemExit(f"deploy trigger failed: {r.status_code} {r.text}")
        return r.json()["id"]


def wait_for_deploy(key: str, service_id: str, deploy_id: str, timeout_s: int = 600) -> None:
    started = time.time()
    with httpx.Client(timeout=30) as c:
        while time.time() - started < timeout_s:
            r = c.get(
                f"{RENDER}/services/{service_id}/deploys/{deploy_id}",
                headers=auth_headers(key),
            )
            r.raise_for_status()
            status = r.json().get("status", "")
            elapsed = int(time.time() - started)
            print(f"  [{elapsed:3d}s] deploy status: {status}")
            if status in ("live", "succeeded"):
                return
            if status in ("build_failed", "update_failed", "canceled", "deactivated"):
                raise SystemExit(f"deploy ended with status '{status}'")
            time.sleep(8)
    raise SystemExit(f"deploy did not go live in {timeout_s}s")


def wait_for_route(url: str, timeout_s: int = 180) -> None:
    """Poll the new /owl/health/owl-studio-sales endpoint until it's reachable.
    Since no sites are registered yet, we use / (which exists) as the proxy."""
    started = time.time()
    with httpx.Client(timeout=15) as c:
        while time.time() - started < timeout_s:
            try:
                r = c.get(url)
                if r.status_code < 500:
                    return
            except Exception:
                pass
            time.sleep(4)


def register_site(owner_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{CALLMEIE_URL}/owl/sites",
            params={"token": owner_token},
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code == 409:
            print(f"  [skip] site '{payload['site_id']}' already registered")
            return {"already_exists": True}
        if r.status_code >= 400:
            raise SystemExit(f"site registration failed: {r.status_code} {r.text}")
        return r.json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="Render API key (rnd_...). Falls back to $RENDER_API_KEY")
    ap.add_argument("--owner-token", help="Reuse an existing OWL_OWNER_TOKEN. Default: generate a fresh one.")
    ap.add_argument("--dry-run", action="store_true", help="Print what would happen; change nothing")
    args = ap.parse_args()

    api_key = args.key or os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Missing Render API key. Set RENDER_API_KEY env var or pass --key rnd_xxx")
    if not api_key.startswith("rnd_"):
        print(f"[warn] key doesn't start with 'rnd_' — may not be a Render API key", file=sys.stderr)

    print("[1/6] finding Render service...")
    svc = find_service(api_key)
    service_id = svc["id"]
    print(f"      service: {svc['name']}  id: {service_id}")

    print("[2/6] fetching current env vars...")
    existing = get_env_vars(api_key, service_id)
    have_keys = {e["key"] for e in existing}
    print(f"      currently {len(existing)} env vars set: {sorted(have_keys)}")

    owner_token = args.owner_token
    if "OWL_OWNER_TOKEN" in have_keys:
        for e in existing:
            if e["key"] == "OWL_OWNER_TOKEN":
                owner_token = owner_token or e["value"]
                break
        print(f"      OWL_OWNER_TOKEN already set — reusing")
        need_redeploy = False
    else:
        owner_token = owner_token or secrets.token_urlsafe(32)
        existing.append({"key": "OWL_OWNER_TOKEN", "value": owner_token})
        print(f"      generating new OWL_OWNER_TOKEN ({len(owner_token)} chars)")
        need_redeploy = True

    if args.dry_run:
        print("\n[dry-run] would PUT", len(existing), "env vars + trigger deploy + register 2 sites.")
        print("[dry-run] owner_token =", owner_token)
        return

    if need_redeploy:
        print("[3/6] saving merged env vars...")
        put_env_vars(api_key, service_id, existing)
        print("      done.")
        print("[4/6] triggering deploy + waiting for live...")
        deploy_id = trigger_deploy(api_key, service_id)
        wait_for_deploy(api_key, service_id, deploy_id)
        print("      deploy live.")
    else:
        print("[3/6] env vars unchanged — skipping save + deploy")
        print("[4/6] skipped (no redeploy needed)")

    print("[5/6] waiting for /owl route to become reachable...")
    wait_for_route(CALLMEIE_URL + "/")
    print("      reachable.")

    print("[6/6] registering the first two test sites...")
    results = []
    for payload in [
        {
            "site_id": "owl-studio-sales",
            "display_name": "Owl Studio · Sales",
            "tier": "starter",
            "lead_email": "callmeie@proton.me",
            "lead_sms": "+353 85 786 3564",
            "edit_emails": ["callmeie@proton.me"],
            "live_url": "https://websites.owlzone.trade",
        },
        {
            "site_id": "rathborne-dental-demo",
            "display_name": "Rathborne Dental (demo)",
            "tier": "pro",
            "care_tier": "growth",
            "lead_email": "callmeie@proton.me",
            "lead_sms": "+353 85 786 3564",
            "edit_emails": ["callmeie@proton.me"],
            "live_url": "https://websites.owlzone.trade/samples/industries/01-dental-swiss.html",
        },
    ]:
        print(f"      -> {payload['site_id']}")
        res = register_site(owner_token, payload)
        results.append((payload["site_id"], res))

    print()
    print("=" * 72)
    print("DONE.")
    print("=" * 72)
    print(f"OWL_OWNER_TOKEN = {owner_token}")
    print(f"(save this in your Vaultwarden / 1Password vault — you'll need it to register future sites)")
    print()
    for site_id, res in results:
        if res.get("already_exists"):
            print(f"{site_id}  (already registered — admin URL unchanged)")
            continue
        print(f"{site_id}")
        print(f"  admin: {CALLMEIE_URL}{res['admin_url']}")
        print(f"  token: {res['admin_token']}")
        print(f"  snippet (paste once per page with a data-owl form):")
        print(f"    {res['embed_snippet']}")
        print()
    print("Next: paste the embed snippet into the contact form on each site, then submit a test message to verify.")


if __name__ == "__main__":
    main()
