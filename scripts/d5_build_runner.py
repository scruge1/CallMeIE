#!/usr/bin/env python3
"""D5 managed-website build runner (Gap A — PDR-CLIENT-FULFILLMENT.md §6).

Polls `d5_build_queue` every 60s. On each tick:
  1. SELECT oldest row WHERE status IN ('queued','failed') AND attempts < 3
  2. FOR UPDATE SKIP LOCKED so two daemons can't grab the same row
  3. Mark status='running', attempts += 1, started_at = now()
  4. Write the brief.json to a tmp dir
  5. Invoke /new-service-site skill via `claude` CLI in headless mode
     against the brief; capture stdout + stderr + exit code
  6. On success: status='succeeded', finished_at = now(),
     anonymise_at = now() + 90 days (PB3 GDPR retention)
  7. On failure: status='failed' (retries next tick up to attempts=3)
  8. On 3rd failure: status='blocked' + SMS Adam + notified_owner_at = now()

Runs in a `python:3.13-slim` container on the coolify network alongside
docops-rescue-daemon (precedent: INFRA.md §14.5 line 812). Single instance.
systemd-style restart unless-stopped.

PB-Fail-Closed (per constraints): if the `claude` CLI binary is not
present or `ANTHROPIC_API_KEY` is empty, the runner refuses to start.
A missing CLI cannot fake a build silently.

Env required (via Coolify env or routes/.env):
  DATABASE_URL           - postgres connection string (same as corrections-consumer)
  ANTHROPIC_API_KEY      - claude CLI auth
  TWILIO_ACCOUNT_SID     - SMS on final failure (optional — degrades to stdout log)
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER
  OWNER_NUMBER           - Adam's mobile
  CLAUDE_CLI_BIN         - path to `claude` binary (default: /usr/local/bin/claude)
  BUILD_OUTPUT_ROOT      - where /new-service-site writes the dist/
                           (default: /var/d5-builds)
  POLL_INTERVAL_SECONDS  - default 60
  BUILD_TIMEOUT_SEC      - default 1800 (30 min hard cap per job)
  D5_RETENTION_DAYS      - default 90 (PB3 GDPR — intake_payload anonymise window)
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests

# ─── Config ──────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "").strip()
CLAUDE_CLI_BIN = os.environ.get("CLAUDE_CLI_BIN", "/usr/local/bin/claude")
BUILD_OUTPUT_ROOT = Path(os.environ.get("BUILD_OUTPUT_ROOT", "/var/d5-builds"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
BUILD_TIMEOUT_SEC = int(os.environ.get("BUILD_TIMEOUT_SEC", "1800"))
RETENTION_DAYS = int(os.environ.get("D5_RETENTION_DAYS", "90"))


def _preflight() -> None:
    """Fail closed if dependencies are missing. Constraint:
    'If `claude` CLI is not available on the deploy container, FAIL CLOSED —
    return error, don't fake the build.'"""
    if not DATABASE_URL:
        print("[d5_runner] FATAL: DATABASE_URL not set", flush=True)
        sys.exit(2)
    if not ANTHROPIC_API_KEY:
        print("[d5_runner] FATAL: ANTHROPIC_API_KEY not set (fail-closed)",
              flush=True)
        sys.exit(2)
    if not shutil.which(CLAUDE_CLI_BIN) and not Path(CLAUDE_CLI_BIN).is_file():
        print(
            f"[d5_runner] FATAL: claude CLI not found at {CLAUDE_CLI_BIN} "
            "(fail-closed — refuse to fake builds)",
            flush=True,
        )
        sys.exit(2)


# ─── Graceful shutdown ───────────────────────────────────────────────
_RUNNING = True


def _stop(signum, frame):  # noqa: ARG001
    global _RUNNING
    print(
        f"[d5_runner] received signal {signum}, finishing current job then exiting",
        flush=True,
    )
    _RUNNING = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def _conn():
    return psycopg.connect(DATABASE_URL, autocommit=False)


def _claim_one_job() -> dict | None:
    """Returns dict row or None. Marks status='running', bumps attempts.
    SKIP LOCKED so a future second daemon can't grab the same row."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH next AS (
                SELECT id FROM d5_build_queue
                 WHERE status IN ('queued','failed')
                   AND attempts < %s
                 ORDER BY queued_at ASC
                 LIMIT 1
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE d5_build_queue q
               SET status = 'running',
                   attempts = q.attempts + 1,
                   started_at = now(),
                   last_error = NULL
              FROM next
             WHERE q.id = next.id
            RETURNING q.id, q.stripe_event_id, q.site_id, q.tier,
                      q.stripe_price_id, q.customer_email, q.customer_name,
                      q.customer_phone, q.intake_payload, q.attempts
            """,
            (MAX_ATTEMPTS,),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        conn.commit()
    cols = [
        "id", "stripe_event_id", "site_id", "tier", "stripe_price_id",
        "customer_email", "customer_name", "customer_phone",
        "intake_payload", "attempts",
    ]
    return dict(zip(cols, row))


def _mark_succeeded(job_id: int) -> None:
    """PB3 — set anonymise_at = finished_at + retention window."""
    anonymise_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE d5_build_queue
                  SET status='succeeded',
                      finished_at=now(),
                      last_error=NULL,
                      anonymise_at=%s
                WHERE id=%s""",
            (anonymise_at, job_id),
        )
        conn.commit()


def _mark_failed(job_id: int, attempts: int, error: str) -> bool:
    """Returns True if this is the FINAL failure (attempts >= MAX_ATTEMPTS)."""
    final = attempts >= MAX_ATTEMPTS
    new_status = "blocked" if final else "failed"
    with _conn() as conn, conn.cursor() as cur:
        if final:
            cur.execute(
                """UPDATE d5_build_queue
                      SET status=%s,
                          finished_at=now(),
                          last_error=%s,
                          notified_owner_at=now()
                    WHERE id=%s""",
                (new_status, error[:4000], job_id),
            )
        else:
            cur.execute(
                "UPDATE d5_build_queue SET status=%s, last_error=%s WHERE id=%s",
                (new_status, error[:4000], job_id),
            )
        conn.commit()
    return final


def _sms_owner(body: str) -> None:
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and OWNER_NUMBER):
        print(f"[d5_runner] Twilio not configured; would have SMS'd: {body}",
              flush=True)
        return
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"From": TWILIO_FROM, "To": OWNER_NUMBER, "Body": body[:1500]},
            timeout=10,
        )
        if r.status_code >= 300:
            print(f"[d5_runner] SMS failed {r.status_code}: {r.text[:500]}",
                  flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[d5_runner] SMS exception: {e}", flush=True)


def _build_brief(job: dict) -> dict:
    """Convert d5_build_queue row → /new-service-site brief.json shape.
    Schema reference: ~/.claude/skills/new-service-site/SKILL.md
    'Input Brief Format' section."""
    intake = job.get("intake_payload") or {}
    if isinstance(intake, str):
        # JSONB usually comes through as a Python dict via psycopg, but
        # the SQLite path stores it as TEXT — defensive parse.
        try:
            intake = json.loads(intake)
        except Exception:
            intake = {}
    business_name = (
        intake.get("business_name")
        or job.get("customer_name")
        or job.get("customer_email")
    )
    return {
        "clientSlug": job["site_id"],
        "businessName": business_name,
        "industry": intake.get("business_type") or "service",
        "market": "IE-SME",
        "tier": job["tier"],
        "phone": intake.get("contact_phone") or job.get("customer_phone") or "",
        "address": {"raw": intake.get("address") or ""},
        "tone": "premium-trust" if job["tier"] == "premium" else "approachable-pro",
        "services": intake.get("services") or "",
        "hours": intake.get("hours") or "",
        "contactEmail": job["customer_email"],
        "primaryHex": "#2d6a4f",  # token-author overrides from brand questionnaire
        "accentHex": "#b08d57",
        "_provenance": {
            "stripe_event_id": job["stripe_event_id"],
            "stripe_price_id": job["stripe_price_id"],
        },
    }


def _invoke_skill(brief_path: Path, build_dir: Path) -> tuple[int, str, str]:
    """Run `claude` CLI in headless mode to execute /new-service-site.
    Returns (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    prompt = (
        f"Run /new-service-site with brief at {brief_path}. "
        f"Output to {build_dir}. Do not prompt for confirmation — proceed end-to-end. "
        f"Stop only on hard QA failure."
    )
    proc = subprocess.run(
        [CLAUDE_CLI_BIN, "-p", prompt, "--output-format", "text"],
        env=env,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SEC,
        cwd=str(build_dir),
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _process_job(job: dict) -> None:
    job_id = job["id"]
    site_id = job["site_id"]
    BUILD_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    build_dir = BUILD_OUTPUT_ROOT / site_id
    build_dir.mkdir(parents=True, exist_ok=True)
    brief = _build_brief(job)
    brief_path = build_dir / "brief.json"
    brief_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    print(
        f"[d5_runner] starting job id={job_id} site={site_id} "
        f"tier={job['tier']} attempt={job['attempts']}",
        flush=True,
    )
    try:
        code, out, err = _invoke_skill(brief_path, build_dir)
    except subprocess.TimeoutExpired:
        msg = f"timeout after {BUILD_TIMEOUT_SEC}s"
        final = _mark_failed(job_id, job["attempts"], msg)
        if final:
            _sms_owner(f"CallMeIE * D5 BUILD BLOCKED * {site_id} * {msg}")
        return
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        final = _mark_failed(job_id, job["attempts"], tb)
        if final:
            _sms_owner(
                f"CallMeIE * D5 BUILD BLOCKED * {site_id} * see logs (exception)"
            )
        return

    if code == 0:
        # Sanity: dist/ should exist now (skill's QA stage produces it)
        dist_dir = build_dir / "dist"
        if not dist_dir.is_dir() or not any(dist_dir.iterdir()):
            err_msg = f"skill exited 0 but dist/ empty (build_dir={build_dir})"
            final = _mark_failed(job_id, job["attempts"], err_msg)
            if final:
                _sms_owner(
                    f"CallMeIE * D5 BUILD BLOCKED * {site_id} * "
                    f"empty dist after success exit"
                )
            return
        _mark_succeeded(job_id)
        _sms_owner(
            f"CallMeIE * D5 BUILT * {site_id} ({job['tier']}) * "
            f"{job['customer_email']} * review build_dir then publish"
        )
        print(f"[d5_runner] OK job id={job_id} site={site_id}", flush=True)
    else:
        err_msg = (err or out or f"exit {code}")[:3000]
        final = _mark_failed(job_id, job["attempts"], err_msg)
        if final:
            _sms_owner(
                f"CallMeIE * D5 BUILD BLOCKED * {site_id} * "
                f"3 attempts failed * see d5_build_queue.last_error"
            )


def main() -> None:
    _preflight()
    print(
        f"[d5_runner] starting, poll={POLL_INTERVAL}s, "
        f"max_attempts={MAX_ATTEMPTS}, claude_cli={CLAUDE_CLI_BIN}",
        flush=True,
    )
    while _RUNNING:
        try:
            job = _claim_one_job()
            if job:
                _process_job(job)
                continue  # check immediately for another (don't sleep if backlog)
        except Exception:  # noqa: BLE001
            print(
                f"[d5_runner] tick exception: {traceback.format_exc()}",
                flush=True,
            )
        # No job (or recoverable exception): sleep with shutdown checks
        for _ in range(POLL_INTERVAL):
            if not _RUNNING:
                break
            time.sleep(1)
    print("[d5_runner] shutting down clean", flush=True)


if __name__ == "__main__":
    main()
