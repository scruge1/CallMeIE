#!/usr/bin/env python3
"""Stage-1 helper for the P0-9 Render -> Coolify migration.

Pulls env vars from the Render service for `srv-d75f7luuk2gs73d8b79g`
(the live `CallMeIE` FastAPI), adjusts the two values that need
adjusting, and POSTs each to the new Coolify application via the
Coolify v4 API.

Adjustments:
  - DATABASE_URL -> repointed at the Coolify Postgres
    `callmeie-api-pg` (uuid `zpy3t4torksez48k8attrzbr`) so the new
    deploy has a clean, persistent Postgres of its own. The Render
    Postgres URL is left untouched on Render's side until Stage 4
    cutover.
  - DB_PATH dropped — was the Render free-tier `/tmp` SQLite path,
    irrelevant once DATABASE_URL is set (server.py:225 prefers
    Postgres when present).

Tokens pulled from `~/.claude/routes/.env`:
  - COOLIFY_API_ROOT_TOKEN  (write-capable)
  - RENDER_API_KEY

Designed to be idempotent — re-running just PATCHes the same keys.

Environment knobs (override at the shell prompt):
  - COOLIFY_APP_UUID — defaults to xml9wji6109b1kergfz05665
  - COOLIFY_PG_INTERNAL_URL — full postgres:// URL incl. password
  - RENDER_SERVICE_ID — defaults to srv-d75f7luuk2gs73d8b79g
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx


VAULT = Path.home() / ".claude" / "routes" / ".env"
COOLIFY_BASE = "https://coolify.owlzone.trade/api/v1"
COOLIFY_APP_UUID = os.environ.get("COOLIFY_APP_UUID", "xml9wji6109b1kergfz05665")
RENDER_BASE = "https://api.render.com/v1"
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "srv-d75f7luuk2gs73d8b79g")

# Internal hostname follows Coolify's network: each DB resolves under
# its own UUID inside the Docker network. Password baked in at create
# time. If you re-create the DB this URL changes — re-pull from the
# Coolify dashboard or `GET /databases/{uuid}`.
COOLIFY_PG_INTERNAL_URL = os.environ.get(
    "COOLIFY_PG_INTERNAL_URL",
    "postgres://callmeie:gjbkJbyxXRkvN1Ym1ZTr17FUPilH6z8m7L67X77MT2fZwiPHeKGzPIXJmfW9MlmJ@zpy3t4torksez48k8attrzbr:5432/callmeie",
)

# Keys we DO NOT push from Render -> Coolify.
SKIP_KEYS = {
    "DB_PATH",  # SQLite fallback path; irrelevant once DATABASE_URL is set
}

# Keys overridden with a Coolify-specific value (set or rewritten).
OVERRIDES = {
    "DATABASE_URL": COOLIFY_PG_INTERNAL_URL,
}


def _read_token(name: str) -> str:
    if not VAULT.exists():
        sys.exit(f"vault not found: {VAULT}")
    for line in VAULT.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{name} not found in {VAULT}")


def _redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 16:
        return value[:4] + "..." + value[-2:] if len(value) > 6 else "***"
    return value[:8] + "..." + value[-4:]


def main() -> int:
    coolify_token = _read_token("COOLIFY_API_ROOT_TOKEN")
    render_token = _read_token("RENDER_API_KEY")

    with httpx.Client(timeout=30.0) as client:
        r = client.get(
            f"{RENDER_BASE}/services/{RENDER_SERVICE_ID}/env-vars",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {render_token}"},
        )
        r.raise_for_status()
        items = r.json()
        print(f"pulled {len(items)} env vars from Render service {RENDER_SERVICE_ID}")

        # Render returns [{"envVar": {key, value}, "cursor": ...}, ...]
        env_pairs: dict[str, str] = {}
        for item in items:
            ev = item.get("envVar", item)
            k, v = ev.get("key"), ev.get("value")
            if not k or v is None:
                continue
            if k in SKIP_KEYS:
                print(f"  skip {k} (in SKIP_KEYS)")
                continue
            env_pairs[k] = v

        # Apply overrides on top
        for k, v in OVERRIDES.items():
            env_pairs[k] = v
            print(f"  override {k} -> {_redact(v)}")

        # Pull existing Coolify env vars to know which are PATCH vs POST
        r = client.get(
            f"{COOLIFY_BASE}/applications/{COOLIFY_APP_UUID}/envs",
            headers={"Authorization": f"Bearer {coolify_token}"},
        )
        r.raise_for_status()
        existing = {e["key"]: e for e in r.json()}
        print(f"coolify already has {len(existing)} env vars on app {COOLIFY_APP_UUID}")

        # Push each one. Coolify v4: POST creates, PATCH updates by key.
        wrote, updated, failed = 0, 0, 0
        for k, v in env_pairs.items():
            method, url = ("PATCH", f"{COOLIFY_BASE}/applications/{COOLIFY_APP_UUID}/envs") \
                if k in existing else ("POST", f"{COOLIFY_BASE}/applications/{COOLIFY_APP_UUID}/envs")
            payload = {"key": k, "value": v, "is_preview": False}
            r = client.request(
                method, url,
                headers={"Authorization": f"Bearer {coolify_token}", "Content-Type": "application/json"},
                json=payload,
            )
            if 200 <= r.status_code < 300:
                if method == "PATCH":
                    updated += 1
                    print(f"  ~ {k} = {_redact(v)}")
                else:
                    wrote += 1
                    print(f"  + {k} = {_redact(v)}")
            else:
                failed += 1
                print(f"  ! {k} {method} -> {r.status_code} {r.text[:200]}")

        print(f"\nsummary: wrote={wrote} updated={updated} failed={failed} skipped={len(SKIP_KEYS)}")
        if failed:
            return 1
        print(f"\nnext: deploy via\n  curl -H 'Authorization: Bearer $COOLIFY_API_ROOT_TOKEN' "
              f"'{COOLIFY_BASE.replace('/v1','')}/v1/deploy?uuid={COOLIFY_APP_UUID}&force=true'")
        return 0


if __name__ == "__main__":
    sys.exit(main())
