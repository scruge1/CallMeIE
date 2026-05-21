#!/usr/bin/env python3
"""
Cancellation export -- produce a portable ZIP of one tenant's data.

Closes PDR-CLIENT-FULFILLMENT.md §6 Gap #6. D5 (managed websites) +
Care plans + Doc Ops tiers all promise "data export within 7 days of
cancellation". This script is the executable side of that promise.

Run:
    python export-customer-data.py --tenant-id <slug-or-email> --output-dir <dir>
    python export-customer-data.py --tenant-id <id> --output-dir <dir> \
        --products receptionist,docops,websites

Behavior:
  1. Resolve tenant: by site_id, by email, or by stripe customer_id.
  2. Determine which products tenant has (receptionist? doc-ops? website?).
  3. Per-product exporter writes files under build_dir/.
  4. Pack build_dir -> <tenant>-export-<utc_ts>.zip.
  5. Emit a README.md inside the zip (GDPR retention notes incl.).

Env (loaded via ~/.claude/routes/.env if running locally, else from Coolify env):
  DATABASE_URL        - postgres
  VAPI_API_KEY        - call logs + assistant config
  COOLIFY_API_TOKEN   - read-only token (INFRA.md §2) (or COOLIFY_API_ROOT_TOKEN)
  COOLIFY_BASE_URL    - https://coolify.callmeie.ie/api/v1
  PORKBUN_API_KEY     - DNS export (best-effort)
  PORKBUN_SECRET_API_KEY
  HETZNER_S3_ENDPOINT / HETZNER_S3_ACCESS_KEY / HETZNER_S3_SECRET / HETZNER_DOCOPS_BUCKET

Mirrors `coolify_list.py` _load_env pattern. Same shape as the Gap A
applied draft sibling `d5_build_runner.py`.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# psycopg + requests are imported lazily inside helpers so --help and --dry-run
# work even on hosts without those installed (smoke test mandate).


# ────────────────────────────────────────────────────────────────────
# Env load -- mirrors coolify_list.py pattern (routes/.env then process env).
# ────────────────────────────────────────────────────────────────────
def _load_env_vault() -> None:
    """Load ~/.claude/routes/.env into os.environ (without overriding existing).

    Same shape as coolify_list.py `load_env` -- KEY=VALUE lines, strip quotes,
    skip comments. Safe to call multiple times.
    """
    vault = Path.home() / ".claude" / "routes" / ".env"
    if not vault.is_file():
        return
    for line in vault.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_env_vault()
DATABASE_URL = os.environ.get("DATABASE_URL", "")
VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "").strip()
COOLIFY_API_TOKEN = (
    os.environ.get("COOLIFY_API_TOKEN", "").strip()
    or os.environ.get("COOLIFY_API_ROOT_TOKEN", "").strip()
)
COOLIFY_BASE = os.environ.get(
    "COOLIFY_BASE_URL", "https://coolify.callmeie.ie/api/v1"
).rstrip("/")
PORKBUN_API_KEY = os.environ.get("PORKBUN_API_KEY", "").strip()
PORKBUN_SECRET = os.environ.get("PORKBUN_SECRET_API_KEY", "").strip()


KNOWN_PRODUCTS = ("receptionist", "docops", "websites")


# ────────────────────────────────────────────────────────────────────
# Tenant resolver
# ────────────────────────────────────────────────────────────────────
def resolve_tenant(ident: str) -> dict:
    """Identifier can be site_id (slug-12345), email, or cus_XYZ.

    Returns dict with: site_id, display_name, lead_email, tier, care_tier,
    vapi_assistant_id (if linked).
    """
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        if ident.startswith("cus_"):
            cur.execute(
                """
                SELECT site_id, display_name, lead_email, tier, care_tier
                  FROM owl_sites
                 WHERE site_id IN (
                    SELECT site_id FROM owl_payments
                     WHERE customer_id = %s ORDER BY ts DESC LIMIT 1)
                """,
                (ident,),
            )
        elif "@" in ident:
            cur.execute(
                """
                SELECT site_id, display_name, lead_email, tier, care_tier
                  FROM owl_sites
                 WHERE lower(lead_email) = lower(%s)
                 ORDER BY id DESC LIMIT 1
                """,
                (ident,),
            )
        else:
            cur.execute(
                """
                SELECT site_id, display_name, lead_email, tier, care_tier
                  FROM owl_sites WHERE site_id = %s
                """,
                (ident,),
            )
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"tenant not found: {ident}")
        site_id, display_name, lead_email, tier, care_tier = row

        # Vapi assistant link (best-effort -- client_tokens may not exist on
        # every DB shape; tolerant of missing table).
        assistant_id = None
        try:
            cur.execute(
                """
                SELECT assistant_id FROM client_tokens
                 WHERE site_id = %s AND assistant_id IS NOT NULL
                 ORDER BY id DESC LIMIT 1
                """,
                (site_id,),
            )
            a = cur.fetchone()
            assistant_id = a[0] if a else None
        except Exception:
            pass

    return {
        "site_id": site_id,
        "display_name": display_name,
        "lead_email": lead_email,
        "tier": tier,
        "care_tier": care_tier,
        "vapi_assistant_id": assistant_id,
    }


# ────────────────────────────────────────────────────────────────────
# Receptionist exporter
# ────────────────────────────────────────────────────────────────────
def export_receptionist(tenant: dict, out: Path) -> dict:
    """Export call logs (CSV) + transcripts (TXT per call) + assistant config (JSON)."""
    if not tenant.get("vapi_assistant_id"):
        return {"skipped": "no Vapi assistant linked"}

    import psycopg
    import requests

    rec_dir = out / "receptionist"
    rec_dir.mkdir(parents=True, exist_ok=True)

    # Call log CSV from local call_events table.
    csv_path = rec_dir / "calls.csv"
    n_calls = 0
    rows: list = []
    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT call_id, assistant_id, ts, status, duration_seconds, caller, summary
                  FROM call_events
                 WHERE assistant_id = %s
                 ORDER BY ts DESC
                """,
                (tenant["vapi_assistant_id"],),
            )
            rows = cur.fetchall()
    except Exception as e:
        (rec_dir / "calls-error.txt").write_text(
            f"call_events table read failed: {e}", encoding="utf-8"
        )
        rows = []

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["call_id", "assistant_id", "ts", "status", "duration_seconds", "caller", "summary"]
        )
        for r in rows:
            w.writerow(r)
            n_calls += 1

    # Transcripts via Vapi REST per call (best-effort).
    transcripts_dir = rec_dir / "transcripts"
    transcripts_dir.mkdir(exist_ok=True)
    n_tx = 0
    if VAPI_API_KEY:
        headers = {"Authorization": f"Bearer {VAPI_API_KEY}"}
        for r in rows:
            call_id = r[0]
            if not call_id:
                continue
            try:
                resp = requests.get(
                    f"https://api.vapi.ai/call/{call_id}", headers=headers, timeout=20
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tx = data.get("transcript") or ""
                    (transcripts_dir / f"{call_id}.txt").write_text(tx, encoding="utf-8")
                    n_tx += 1
            except Exception:
                continue

        # Assistant config
        try:
            resp = requests.get(
                f"https://api.vapi.ai/assistant/{tenant['vapi_assistant_id']}",
                headers=headers,
                timeout=20,
            )
            if resp.status_code == 200:
                (rec_dir / "assistant-config.json").write_text(
                    json.dumps(resp.json(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception as e:
            (rec_dir / "assistant-config.error.txt").write_text(str(e), encoding="utf-8")
    else:
        (rec_dir / "transcripts-skipped.txt").write_text(
            "VAPI_API_KEY not set; transcripts and assistant config not exported.",
            encoding="utf-8",
        )

    return {"calls": n_calls, "transcripts": n_tx}


# ────────────────────────────────────────────────────────────────────
# Doc Ops exporter
# ────────────────────────────────────────────────────────────────────
def export_docops(tenant: dict, out: Path) -> dict:
    """Doc Ops storage = Hetzner S3 + Postgres extractions table.

    Exports: documents (downloaded from S3) + extractions CSV + audit_log CSV.
    Note: AX52 corrections-consumer is the canonical store. If this script
    is run from outside AX52 it can only export rows it can see.
    """
    import psycopg

    do_dir = out / "doc-ops"
    do_dir.mkdir(parents=True, exist_ok=True)
    counts: dict = {"documents": 0, "extractions": 0, "audit_rows": 0}

    rows: list = []
    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('extractions')")
            if cur.fetchone()[0] is None:
                (do_dir / "NOT-AVAILABLE.txt").write_text(
                    "Doc Ops storage lives on AX52; run this script there to "
                    "include doc-ops data.",
                    encoding="utf-8",
                )
                return {"skipped": "doc-ops tables not in this DB"}

            cur.execute(
                """
                SELECT id, doc_filename, doc_s3_key, status, extracted_fields_json, ts
                  FROM extractions WHERE tenant_slug = %s ORDER BY ts DESC
                """,
                (tenant["site_id"],),
            )
            rows = cur.fetchall()
    except Exception as e:
        (do_dir / "extractions-error.txt").write_text(str(e), encoding="utf-8")
        return {"skipped": f"doc-ops introspection failed: {e}"}

    with (do_dir / "extractions.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["id", "doc_filename", "doc_s3_key", "status", "extracted_fields_json", "ts"]
        )
        for r in rows:
            w.writerow(r)
            counts["extractions"] += 1

    # Documents: S3 download (best-effort; skip on missing creds / boto3).
    try:
        import boto3  # type: ignore

        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("HETZNER_S3_ENDPOINT", ""),
            aws_access_key_id=os.environ.get("HETZNER_S3_ACCESS_KEY", ""),
            aws_secret_access_key=os.environ.get("HETZNER_S3_SECRET", ""),
        )
        bucket = os.environ.get("HETZNER_DOCOPS_BUCKET", "callmeie-docops")
        docs_dir = do_dir / "documents"
        docs_dir.mkdir(exist_ok=True)
        for r in rows:
            key = r[2]
            if not key:
                continue
            try:
                fn = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(key).name) or f"doc_{r[0]}"
                s3.download_file(bucket, key, str(docs_dir / fn))
                counts["documents"] += 1
            except Exception:
                continue
    except Exception as e:
        (do_dir / "documents-skipped.txt").write_text(
            f"S3 download skipped: {e}", encoding="utf-8"
        )

    # Audit log (best-effort; the table may or may not be tenant-scoped).
    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ts, action, actor, detail_json
                  FROM audit_log WHERE tenant_slug = %s ORDER BY ts DESC
                """,
                (tenant["site_id"],),
            )
            arows = cur.fetchall()
            with (do_dir / "audit_log.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["id", "ts", "action", "actor", "detail_json"])
                for a in arows:
                    w.writerow(a)
                    counts["audit_rows"] += 1
    except Exception:
        pass

    return counts


# ────────────────────────────────────────────────────────────────────
# Managed Website exporter
# ────────────────────────────────────────────────────────────────────
def export_website(tenant: dict, out: Path) -> dict:
    """Source repo + DNS records (json) + Coolify app metadata.

    Source repo: clone via Coolify API -> application git URL (--depth=1).
    DNS records: Porkbun /domain/getRecord retrieve (best-effort).
    """
    import requests

    web_dir = out / "website"
    web_dir.mkdir(parents=True, exist_ok=True)
    info: dict = {}

    # Coolify: find the application for this site_id (convention: name == site_id).
    if COOLIFY_API_TOKEN:
        try:
            r = requests.get(
                f"{COOLIFY_BASE}/applications",
                headers={"Authorization": f"Bearer {COOLIFY_API_TOKEN}"},
                timeout=20,
            )
            if r.ok:
                payload = r.json()
                apps = payload if isinstance(payload, list) else payload.get("data", [])
                match = next(
                    (a for a in apps if (a.get("name") or "") == tenant["site_id"]),
                    None,
                )
                if match:
                    git_url = (
                        match.get("git_repository")
                        or match.get("git_full_url")
                        or ""
                    )
                    if git_url:
                        clone_dir = web_dir / "source"
                        proc = subprocess.run(
                            ["git", "clone", "--depth", "1", git_url, str(clone_dir)],
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        info["source_clone"] = (
                            "ok" if proc.returncode == 0 else proc.stderr[:500]
                        )
                    info["coolify_app"] = match.get("uuid") or match.get("id")
                    info["fqdn"] = match.get("fqdn", "")
                else:
                    info["coolify_app"] = "no matching application"
        except Exception as e:
            info["coolify_error"] = str(e)
    else:
        info["coolify_skipped"] = "COOLIFY_API_TOKEN not set"

    # DNS records via Porkbun /dns/retrieve/{domain} (best-effort).
    try:
        # Try fqdn first (more reliable than display_name).
        domain = info.get("fqdn") or (tenant.get("display_name") or "")
        domain = domain.lower().strip().lstrip("https://").lstrip("http://").rstrip("/")
        if PORKBUN_API_KEY and PORKBUN_SECRET and domain and "." in domain:
            r = requests.post(
                f"https://porkbun.com/api/json/v3/dns/retrieve/{domain}",
                json={"apikey": PORKBUN_API_KEY, "secretapikey": PORKBUN_SECRET},
                timeout=20,
            )
            if r.ok:
                (web_dir / "dns-records.json").write_text(
                    json.dumps(r.json(), indent=2), encoding="utf-8"
                )
                info["dns_records"] = "exported"
            else:
                info["dns_records"] = f"porkbun {r.status_code}"
        else:
            info["dns_records"] = "skipped (no PORKBUN creds or no resolvable domain)"
    except Exception as e:
        info["dns_error"] = str(e)

    (web_dir / "EXPORT-INFO.json").write_text(
        json.dumps(info, indent=2), encoding="utf-8"
    )
    return info


# ────────────────────────────────────────────────────────────────────
# README
# ────────────────────────────────────────────────────────────────────
def write_readme(tenant: dict, out: Path, summary: dict, products: list) -> None:
    md = f"""# Data Export -- {tenant['display_name']}

**Tenant:** `{tenant['site_id']}`
**Email:** {tenant['lead_email']}
**Tier:** {tenant['tier'] or '(none)'}
**Care:** {tenant['care_tier'] or '(none)'}
**Generated:** {datetime.now(timezone.utc).isoformat()}
**Products included:** {", ".join(products)}

## Contents

- `receptionist/` -- call logs (`calls.csv`), per-call transcripts (`transcripts/*.txt`), assistant config (`assistant-config.json`)
- `doc-ops/` -- extractions (`extractions.csv`), documents (`documents/`), audit log (`audit_log.csv`)
- `website/` -- source repo clone (`source/`), DNS records (`dns-records.json`), Coolify app info (`EXPORT-INFO.json`)

## Per-product summary

```json
{json.dumps(summary, indent=2)}
```

## Notes

- Files marked `NOT-AVAILABLE.txt` indicate the corresponding store could not be reached from the host this script ran on (typically: Doc Ops storage lives on AX52; run there to include).
- Transcripts older than Vapi's retention window will be missing.
- DNS records are best-effort; if the tenant's domain is not on Porkbun this section will be empty.

## GDPR retention notes

- This export fulfils the 7-day cancellation data-export commitment per
  `managed-plans-scope.md` (D5 tiers), the Care plan inclusions, and the
  Doc Ops tier promise. Closes PDR-CLIENT-FULFILLMENT.md §6 Gap #6.
- The ZIP contains personal data (caller identifiers, transcripts,
  contact details). Treat as restricted: encrypt in transit when
  emailing the customer (signed link via 1Password or similar), and
  delete the local copy after delivery + acknowledgement.
- Per GDPR Art. 5(1)(e) storage-limitation: once this export is
  delivered and acknowledged, the source rows in `owl_sites`,
  `call_events`, `extractions`, and `audit_log` for this tenant are
  retained only as long as the legal-basis register (legitimate interest
  for billing reconciliation: 6 years; legal claims: as long as
  applicable). The export ZIP is the customer's copy; ours is governed
  by the retention policy.
- Right of erasure (Art. 17): if the customer asks for deletion AFTER
  this export, run the (planned) `purge-tenant.py` companion -- not in
  this script.
"""
    (out / "README.md").write_text(md, encoding="utf-8")


# ────────────────────────────────────────────────────────────────────
# Pack
# ────────────────────────────────────────────────────────────────────
def pack(out: Path, dest_zip: Path) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(out))


# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────
def _parse_products(s: str) -> list:
    if not s or s.lower() == "all":
        return list(KNOWN_PRODUCTS)
    parts = [p.strip().lower() for p in s.split(",") if p.strip()]
    unknown = [p for p in parts if p not in KNOWN_PRODUCTS]
    if unknown:
        raise SystemExit(
            f"unknown --products values: {unknown!r}. "
            f"Allowed: {', '.join(KNOWN_PRODUCTS)} (or 'all')"
        )
    return parts


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="export-customer-data.py",
        description=(
            "Per-tenant data-export ZIP packager. Closes PDR-CLIENT-FULFILLMENT "
            "§6 Gap #6 (7-day cancellation export across D5 + Care + Doc Ops)."
        ),
    )
    ap.add_argument(
        "--tenant-id",
        required=True,
        help="site_id slug, lead email, or Stripe customer_id (cus_XYZ)",
    )
    ap.add_argument(
        "--output-dir",
        default=".",
        help="directory to write the resulting ZIP into (default: cwd)",
    )
    ap.add_argument(
        "--products",
        default="all",
        help=(
            "comma list of products to include: "
            f"{','.join(KNOWN_PRODUCTS)} (default: all)"
        ),
    )
    ap.add_argument(
        "--keep-staging",
        action="store_true",
        help="keep the unzipped staging directory after packing (debug)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the resolved plan (products, env-presence) and exit without "
            "touching the database or any external API. Used for the "
            "docker exec smoke test."
        ),
    )
    return ap


def main(argv: list | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    products = _parse_products(args.products)

    if args.dry_run:
        plan = {
            "ok": True,
            "dry_run": True,
            "tenant_id": args.tenant_id,
            "products": products,
            "output_dir": str(Path(args.output_dir).expanduser().resolve()),
            "env_present": {
                "DATABASE_URL": bool(DATABASE_URL),
                "VAPI_API_KEY": bool(VAPI_API_KEY),
                "COOLIFY_API_TOKEN": bool(COOLIFY_API_TOKEN),
                "PORKBUN_API_KEY": bool(PORKBUN_API_KEY),
            },
        }
        print(json.dumps(plan, indent=2))
        return 0

    if not DATABASE_URL:
        print(
            "DATABASE_URL not set; check ~/.claude/routes/.env or container env",
            file=sys.stderr,
        )
        return 2

    tenant = resolve_tenant(args.tenant_id)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    staging = Path(tempfile.mkdtemp(prefix=f"export-{tenant['site_id']}-"))

    summary: dict = {}
    if "receptionist" in products:
        summary["receptionist"] = export_receptionist(tenant, staging)
    if "docops" in products:
        summary["doc_ops"] = export_docops(tenant, staging)
    if "websites" in products:
        summary["website"] = export_website(tenant, staging)

    write_readme(tenant, staging, summary, products)

    out_dir = Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest_zip = out_dir / f"{tenant['site_id']}-export-{ts}.zip"
    pack(staging, dest_zip)
    print(
        json.dumps(
            {
                "ok": True,
                "tenant": tenant["site_id"],
                "zip": str(dest_zip),
                "products": products,
                "summary": summary,
            },
            indent=2,
        )
    )

    if not args.keep_staging:
        shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
