#!/usr/bin/env python3
"""Stage 3.1 Vapi serverUrl swap — Render → Coolify (P0-9, 2026-05-09).

Walks every Vapi assistant in the account, looks for `serverUrl`,
`server.url`, and any `model.tools[*].server.url` that points at
``https://callmeie.onrender.com``, and PATCHes it to point at
``https://api.callmeie.ie``.

CRITICAL — tool-stripping landmine:
  When you PATCH ``model.messages`` without including ``model.tools``,
  Vapi clears the assistant's tools. Documented in
  ``receptionist/CLAUDE.md`` "Vapi tool stripping" section.

  Mitigation: this script does GET → mutate → PATCH the FULL model
  object (preserving model.tools verbatim). Never PATCHes a partial
  ``model``. Never sends ``model.messages`` without
  ``model.tools``.

Usage:
  python migrate-vapi-server-url.py            # show what would change
  python migrate-vapi-server-url.py --apply    # actually mutate

Reads ``VAPI_API_KEY`` from ``~/.claude/routes/.env``.

Idempotent — running twice has no effect on the second run because
the OLD URL is no longer present.
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

import httpx

OLD = "https://callmeie.onrender.com"
NEW = "https://api.callmeie.ie"
VAPI_BASE = "https://api.vapi.ai"
VAULT = Path.home() / ".claude" / "routes" / ".env"


def _read_token(name: str) -> str:
    for line in VAULT.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{name} not found in {VAULT}")


def _swap_str(s: str | None) -> tuple[str | None, bool]:
    if not isinstance(s, str):
        return s, False
    new = s.replace(OLD, NEW)
    return new, new != s


def _walk_swap(obj):
    """Recursively swap any string field that contains the old URL.
    Returns (new_obj, count_changed)."""
    if isinstance(obj, dict):
        out = {}
        n = 0
        for k, v in obj.items():
            v2, c = _walk_swap(v)
            n += c
            out[k] = v2
        return out, n
    if isinstance(obj, list):
        out = []
        n = 0
        for v in obj:
            v2, c = _walk_swap(v)
            n += c
            out.append(v2)
        return out, n
    if isinstance(obj, str):
        new, changed = _swap_str(obj)
        return new, 1 if changed else 0
    return obj, 0


def _patchable_subset(asst_full: dict) -> dict:
    """Build the minimum PATCH body that mutates serverUrl + nested
    server.url + every model.tools[*].server.url WITHOUT stripping
    anything else.

    Strategy: send back the full ``model`` object verbatim (with the
    swapped tool URLs inside) PLUS the top-level serverUrl + server
    fields. Vapi PATCH semantics replace each field present in the
    body with the value sent — by sending the full unchanged-shape
    model, we preserve model.tools, model.messages, model.provider,
    model.model name, voice, transcriber, etc. — none of which we
    mutate."""
    body = {}

    # Top-level serverUrl
    if isinstance(asst_full.get("serverUrl"), str):
        new, changed = _swap_str(asst_full["serverUrl"])
        if changed:
            body["serverUrl"] = new

    # Top-level server.url (Vapi's "server" object — Claire has this)
    if isinstance(asst_full.get("server"), dict):
        server_new = deepcopy(asst_full["server"])
        if isinstance(server_new.get("url"), str):
            new, changed = _swap_str(server_new["url"])
            if changed:
                server_new["url"] = new
                body["server"] = server_new

    # model.tools[*].server.url
    if isinstance(asst_full.get("model"), dict):
        model_new = deepcopy(asst_full["model"])
        tools = model_new.get("tools")
        any_tool_changed = False
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict) and isinstance(t.get("server"), dict):
                    if isinstance(t["server"].get("url"), str):
                        new, changed = _swap_str(t["server"]["url"])
                        if changed:
                            t["server"]["url"] = new
                            any_tool_changed = True
        if any_tool_changed:
            body["model"] = model_new

    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually PATCH (default is dry-run)")
    args = ap.parse_args()

    token = _read_token("VAPI_API_KEY")
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=60.0) as client:
        r = client.get(f"{VAPI_BASE}/assistant", headers=headers)
        r.raise_for_status()
        assistants = r.json()
        print(f"pulled {len(assistants)} assistants")

        total_root = total_server = total_tool = 0
        for asst_summary in assistants:
            aid = asst_summary["id"]
            # Pull the FULL assistant — list endpoint may omit some fields
            r = client.get(f"{VAPI_BASE}/assistant/{aid}", headers=headers)
            r.raise_for_status()
            full = r.json()
            name = full.get("name") or aid

            patch = _patchable_subset(full)
            if not patch:
                print(f"  skip {name!r}: no callmeie.onrender.com hits")
                continue

            # Tally changes for the report
            n_root = 1 if "serverUrl" in patch else 0
            n_server = 1 if "server" in patch else 0
            n_tools = 0
            if "model" in patch:
                tools = patch["model"].get("tools") or []
                for t in tools:
                    if isinstance(t, dict):
                        s = t.get("server") or {}
                        u = s.get("url", "")
                        if NEW in str(u):
                            n_tools += 1
            total_root += n_root
            total_server += n_server
            total_tool += n_tools

            print(f"  {'PATCH' if args.apply else 'DRY-RUN'} {name!r} (id={aid}): "
                  f"root={n_root} server.url={n_server} tools.server.url={n_tools}")

            if args.apply:
                rp = client.patch(
                    f"{VAPI_BASE}/assistant/{aid}",
                    headers={**headers, "Content-Type": "application/json"},
                    json=patch,
                )
                if not (200 <= rp.status_code < 300):
                    print(f"    ! PATCH {aid} -> {rp.status_code} {rp.text[:300]}")
                    return 2
                rp_full = rp.json()
                # Verify post-PATCH
                if OLD in json.dumps(rp_full):
                    print(f"    ! WARN: {OLD} still present in response after PATCH")
                # Verify model.tools survived
                tools_after = (rp_full.get("model") or {}).get("tools") or []
                tools_before = (full.get("model") or {}).get("tools") or []
                if len(tools_after) != len(tools_before):
                    print(f"    ! WARN: tool count {len(tools_before)} -> {len(tools_after)} (TOOL STRIPPING DETECTED)")
                    return 3
                print(f"    ok — tools preserved ({len(tools_after)} tools), serverUrl swapped")

        print()
        print(f"summary: root.serverUrl={total_root} server.url={total_server} tools.server.url={total_tool}")
        if not args.apply:
            print("(dry-run — re-run with --apply to mutate)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
