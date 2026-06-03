# Cron re-home plan — morning-rollup + GDPR purge

**Date:** 2026-06-03 · **Status:** DRAFT — ready to paste, NOT executed. Needs Adam's go + one confirmation.
**Context:** GHA notification-spam audit (`New repos/handoffs/GHA-AUDIT-2026-06-03.md`). The GitHub Actions schedules for these two jobs were disabled on 2026-06-03 (commit `cbe7200`, `schedule:` commented, `workflow_dispatch` kept) because they failed every run on missing `DATABASE_URL`. This doc re-homes them next to the live DB instead of putting prod creds in a cloud runner.

## Confirmed deploy target = Hetzner / Coolify (NOT Render)

Verified from `INFRA.md §3` (live-probe verified 2026-05-30):

- **Canonical backend = `admin.callmeie.ie` / `api.callmeie.ie` on Coolify, Hetzner cax21 `178.104.205.255`.** Live Postgres, idmax 1587, real calls.
- `callmeie.onrender.com` is a **STALE secondary** — separate DB frozen at id≤45 / 2026-04-26. Pending decommission.
- ⚠ **Do NOT cron these on Render.** rollup would email stats off a frozen DB; **purge against the wrong/stale DB is actively dangerous.** Both must run where the live DB lives = the Hetzner box.

Both deploys still auto-build from `scruge1/CallMeIE` push, so the scripts (`scripts/morning_rollup.py`, `scripts/purge_old_data.py`) are present in the deployed code on the Hetzner box.

## Schedules (unchanged from the disabled workflows)

| Job | Script | Cron (UTC) | Default mode |
|-----|--------|-----------|--------------|
| Lead roll-up | `scripts/morning_rollup.py` | `30 6 * * *` (06:30) | live |
| GDPR purge | `scripts/purge_old_data.py` | `0 3 * * *` (03:00) | **`--dry-run`** until Adam eyeballs first run |

`DATABASE_URL` (and rollup's `RESEND_API_KEY` / `TELEGRAM_*` / `ROLLUP_*` envs) are **already on the box** in the app's Coolify env — reuse them, add **no new secret**.

---

## ONE confirmation needed (decides A vs B)

**Is the CallMeIE receptionist backend a Coolify-managed *Application*** (deployed via Coolify build pack / dockercompose, visible under a Coolify project with a container Coolify can `docker exec` into)?

- **Yes → use Variant A** (Coolify Scheduled Task — UI-managed, cleanest).
- **No / raw `docker run` / unsure → use Variant B** (host crontab on cax21 root — the *proven precedent* already in use on this exact box, INFRA §14.4 lines 839/842, and the documented fallback at line 845 when a service isn't a Coolify-managed Application).

Adam can answer from the Coolify dashboard: does the CallMeIE/receptionist service show up as an **Application** with a **Scheduled Tasks** tab?

---

## Variant A — Coolify Scheduled Task (if Coolify-managed Application)

In Coolify dashboard → the CallMeIE application → **Scheduled Tasks** → add two:

| Field | Roll-up | Purge |
|-------|---------|-------|
| Name | `lead-rollup-daily` | `gdpr-purge-daily-DRYRUN` |
| Command | `python scripts/morning_rollup.py` | `python scripts/purge_old_data.py --dry-run` |
| Frequency | `30 6 * * *` | `0 3 * * *` |
| Container | (the app container — default) | (the app container — default) |

Env: inherits the application's existing env (incl. `DATABASE_URL`) automatically — nothing to add.

**Flip-to-apply (purge), after Adam reads ≥1 dry-run log:** change the purge command to `python scripts/purge_old_data.py --apply` and rename to `gdpr-purge-daily`.

---

## Variant B — host crontab on cax21 root (proven precedent, INFRA §14.4)

Mirrors the existing `docops-sla-check` (line 839) + `dvc_push` (line 842) pattern: a wrapper `.sh` holds env + does `docker exec` into the running app container, invoked by root crontab.

**Step 1 — find the live container name** (Adam, on the box):
```bash
ssh root@178.104.205.255
docker ps --format '{{.Names}}\t{{.Image}}' | grep -iE 'callmeie|receptionist|admin'
# note the container name → use as $C below
```

**Step 2 — wrapper script** `/usr/local/bin/callmeie-cron.sh` (paste, then `chmod +x`):
```bash
#!/usr/bin/env bash
# CallMeIE daily jobs — re-homed from GitHub Actions 2026-06-03.
# DATABASE_URL etc. already live inside the app container env; we just exec there.
set -euo pipefail
C="callmeie-app"   # <-- replace with the container name from Step 1
LOG=/var/log/callmeie
mkdir -p "$LOG"
case "${1:-}" in
  rollup)
    docker exec "$C" python scripts/morning_rollup.py >> "$LOG/rollup.log" 2>&1 ;;
  purge-dryrun)
    docker exec "$C" python scripts/purge_old_data.py --dry-run >> "$LOG/purge.log" 2>&1 ;;
  purge-apply)
    docker exec "$C" python scripts/purge_old_data.py --apply  >> "$LOG/purge.log" 2>&1 ;;
  *) echo "usage: $0 {rollup|purge-dryrun|purge-apply}" >&2; exit 2 ;;
esac
```

**Step 3 — crontab lines** (`crontab -e` as root; matches the disabled GHA schedules, UTC):
```cron
30 6 * * *  /usr/local/bin/callmeie-cron.sh rollup
0  3 * * *  /usr/local/bin/callmeie-cron.sh purge-dryrun
```

**Flip-to-apply (purge), after Adam reads ≥1 dry-run log** (`/var/log/callmeie/purge.log`): change the 03:00 line to `purge-apply`.

---

## Human steps for Adam (summary)

1. Answer the A-vs-B confirmation (is CallMeIE a Coolify-managed Application?).
2. Apply the chosen variant on the box (SSH/dashboard — Claude does NOT touch the host).
3. Leave purge at **dry-run**; read `purge.log` once; only then flip to `--apply`.
4. (Optional later) decommission the stale Render service so it stops accepting writes (INFRA §3 BUG-06/07).

## Rollback

Re-homing is additive. To revert to Actions: uncomment the `schedule:` blocks in `ads-canary.yml` is unrelated; for these two, uncomment `schedule:` in `.github/workflows/morning-rollup.yml` + `purge-old-data.yml` and add `DATABASE_URL` as an Actions secret (not recommended — that's the prod-creds-in-runner anti-pattern this re-home avoids).
