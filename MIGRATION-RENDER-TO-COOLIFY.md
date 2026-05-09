# Migration · CallMeIE FastAPI · Render → Coolify Hetzner

**Status:** open
**Owner:** Adam (Coolify dashboard + DNS + Stripe + Vapi steps need human hands)
**Driver:** P0-9 — Render free-tier 55s cold-start measured by P4 chatbot stress test. Choice was paid tier vs keep-alive vs full migration; Adam picked migration so we land "no cold start" + single ops surface (Doc Ops portal already on the same Hetzner box per INFRA §2).

---

## 1 · What's moving

`callmeie-fix/scripts/server.py` (FastAPI · Python 3.11 · uvicorn) currently
running at `https://callmeie.onrender.com` (Render web service
`srv-d75f7luuk2gs73d8b79g`, Frankfurt EU).

Includes:
- AI Receptionist endpoints (Vapi tool callbacks, /admin, /submit-onboarding)
- Discovery quiz API (`/api/discovery`)
- Owl Studio webhook routes (`/owl/*`, Stripe)
- Predecessor Doc Ops sandbox (`/api/docops/extract` — being retired in
  favour of `portal.callmeie.ie/api/sandbox/extract` per P0-1)
- Vapi metered billing routes (`billing/` package — webhook + portal + admin)

Hetzner box: `ubuntu-4gb-nbg1-8` (4 GB RAM, Nuremberg, IPv4 178.104.205.255).
Coolify control plane at `https://coolify.owlzone.trade`, server uuid
`mihuu5scwb1y3gja1lik7tp9`, project uuid `m100nrzbdx92dn8kxzvrmhpy`.

---

## 2 · Stages — zero-downtime cutover

**Rule: Render keeps serving until Stage 4. Coolify runs in parallel
during 1-3.**

### Stage 1 — Stand up parallel Coolify deploy (safe)

No customer impact. Render keeps running.

#### 1.1 Create Postgres on Coolify

```bash
TOK="$COOLIFY_API_ROOT_TOKEN"
API="https://coolify.owlzone.trade/api/v1"

curl -X POST -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  $API/databases/postgresql \
  -d '{
    "name": "callmeie-db",
    "project_uuid": "m100nrzbdx92dn8kxzvrmhpy",
    "server_uuid": "mihuu5scwb1y3gja1lik7tp9",
    "environment_name": "production",
    "image": "postgres:16-alpine",
    "postgres_user": "callmeie",
    "postgres_db": "callmeie"
  }'
# → save returned uuid as $PG_UUID
# Coolify auto-generates the password; fetch via:
curl -H "Authorization: Bearer $TOK" $API/databases/$PG_UUID
# → POSTGRES_PASSWORD lives in the response under `postgres_password`
```

**Internal connection string** for the FastAPI service (same Coolify
network, no egress):

```
postgresql+psycopg://callmeie:<PG_PASS>@callmeie-db-<PG_UUID>:5432/callmeie
```

Verify Coolify generated the right env vars on the DB:

```bash
curl -H "Authorization: Bearer $TOK" $API/databases/$PG_UUID/envs
```

#### 1.2 Create Application (FastAPI) on Coolify

`scripts/Dockerfile` already exists (Python 3.11 slim, uvicorn). Use the
git-application flow modelled on `owltradezone` (uuid
`kvpvd10evtfhn074p0kgk525` per INFRA §2.2).

```bash
curl -X POST -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  $API/applications \
  -d '{
    "name": "callmeie-api",
    "project_uuid": "m100nrzbdx92dn8kxzvrmhpy",
    "server_uuid": "mihuu5scwb1y3gja1lik7tp9",
    "environment_name": "production",
    "git_repository": "https://github.com/scruge1/CallMeIE.git",
    "git_branch": "main",
    "build_pack": "dockerfile",
    "base_directory": "scripts",
    "dockerfile_location": "Dockerfile",
    "ports_exposes": "8080",
    "health_check_enabled": true,
    "health_check_path": "/health"
  }'
# → save returned uuid as $APP_UUID
```

**Note:** if Coolify rejects the volume / mount fields via API (it does
on v4.0.0-beta.473 per INFRA §14.1c), use the dashboard for the disk
mount step instead.

#### 1.3 Set env vars on the Application

29 env vars to copy from Render. Pull each from the Render dashboard or
`$RENDER_API_KEY` and POST to Coolify. Use this script template
(don't commit secrets — use vault):

```bash
# scripts/coolify-set-envs.sh — DO NOT COMMIT
TOK="$COOLIFY_API_ROOT_TOKEN"
API="https://coolify.owlzone.trade/api/v1"
APP="$APP_UUID"

# from Render via API
RENDER_TOK="$RENDER_API_KEY"
SRV="srv-d75f7luuk2gs73d8b79g"

# helper
set_env() {
  curl -s -X PATCH -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
    $API/applications/$APP/envs \
    -d "{\"key\":\"$1\",\"value\":\"$2\"}" >/dev/null
  echo "set $1"
}

# DATABASE_URL — point at the Coolify Postgres just created
set_env DATABASE_URL "postgresql+psycopg://callmeie:$PG_PASS@callmeie-db-$PG_UUID:5432/callmeie"
set_env CALLMEIE_TIMEZONE "Europe/Dublin"

# everything else — copy from Render
for K in ADMIN_TOKEN ANTHROPIC_API_KEY CALLMEIE_BACKUP_SHEET_ID \
  CALLMEIE_BACKUP_SHEET_TAB CALLMEIE_CALLBACK_CALENDAR_ID \
  CLIENTS_JSON DB_PATH GOOGLE_SA_EMAIL GOOGLE_SERVICE_ACCOUNT_JSON \
  METER_SYNC_TOKEN OWL_OWNER_TOKEN OWL_STRIPE_WEBHOOK_SECRET \
  OWNER_NOTIFICATION_NUMBER STRIPE_API STRIPE_API_KEY \
  STRIPE_METER_NAME STRIPE_SECRET_KEY TELEGRAM_BOT_TOKEN \
  TELEGRAM_CHAT_ID TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN \
  TWILIO_FROM_NUMBER VAPI_API_KEY VAPI_WEBHOOK_SECRET \
  XAI_API_KEY AGENCY_DB_PATH; do
  V=$(curl -s -H "Authorization: Bearer $RENDER_TOK" \
    "https://api.render.com/v1/services/$SRV/env-vars" | \
    jq -r ".[] | select(.envVar.key==\"$K\") | .envVar.value")
  [ -n "$V" ] && [ "$V" != "null" ] && set_env "$K" "$V"
done
```

Two adjustments from the Render shape:
- Drop `DB_PATH` (was the SQLite path on `/tmp` — Coolify Postgres
  replaces it; code already prefers `DATABASE_URL` when set)
- Add `CALLMEIE_TIMEZONE=Europe/Dublin` if not already on Render (used
  by discovery rate-limit reset computation)

#### 1.4 Deploy + verify

```bash
curl -H "Authorization: Bearer $TOK" "$API/deploy?uuid=$APP_UUID&force=true"
# wait ~3 min, watch logs:
curl -H "Authorization: Bearer $TOK" $API/applications/$APP_UUID/logs?lines=200
```

Coolify will auto-assign a sslip.io hostname like
`callmeie-api-$APP_UUID.178.104.205.255.sslip.io`. Test it directly:

```bash
SSLIP="https://callmeie-api-$APP_UUID.178.104.205.255.sslip.io"
curl -i $SSLIP/health
curl -i $SSLIP/api/discovery -X POST -H 'Content-Type: application/json' \
  -d '{"page_context":"hub","business":"dental","pain":"missed-calls","team_size":"solo","urgency":"this-month"}'
```

Both should return 200. Discovery should land a recommendation.

### Stage 2 — Add custom FQDN (still parallel)

Add a NEW DNS record so the Coolify deploy is reachable on a real
hostname, without touching the Render-bound `api.callmeie.ie`:

```
api-coolify.callmeie.ie  CNAME  178.104.205.255  (or appropriate Hetzner record)
```

Set on Coolify:

```bash
curl -X PATCH -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  $API/applications/$APP_UUID \
  -d '{"fqdn":"https://api-coolify.callmeie.ie"}'
# Apply the documented Coolify v4 fqdn-cascade workaround per INFRA §2:
ssh root@178.104.205.255
docker exec coolify-db psql -U coolify -d coolify -c \
  "UPDATE applications SET fqdn = 'https://api-coolify.callmeie.ie' WHERE uuid = '$APP_UUID';"
exit
curl -H "Authorization: Bearer $TOK" "$API/deploy?uuid=$APP_UUID&force=true"
# wait 90s for Let's Encrypt
curl -I https://api-coolify.callmeie.ie/health
```

Run a parallel-shadow test: send a test discovery quiz at the new FQDN
and confirm it lands in Coolify Postgres. Render still owns
`api.callmeie.ie` and is serving real traffic.

### Stage 3 — Cutover (Adam-driven, irreversible-ish)

This is where customer traffic moves. Each step is reversible by
flipping back to Render, but each step has a window where webhooks
in flight could be lost.

**Order matters. Do NOT skip steps.**

#### 3.1 Vapi assistant serverUrls (CRITICAL)

Every Vapi assistant has a `serverUrl` pointing at Render. They MUST
all flip to Coolify before DNS, otherwise calls land on Render after
DNS but their tool-callbacks point at Render too — fine — but post-DNS
new calls land on Coolify with serverUrl still pointing at Render —
broken.

Approach: cut Vapi serverUrl FIRST while the DNS is still Render. Vapi
calls will: ring on Twilio → Vapi runtime → tool callback hits the new
serverUrl (api-coolify.callmeie.ie). Receptionist works on Coolify
already. Render `/admin` etc still works through old DNS.

Use the `restore_tools.py` pattern from `receptionist/CLAUDE.md` to
preserve tools while patching serverUrl. List of assistants in
INFRA / SYSTEM.md; Demo Squad + Claire + 4 demos + every provisioned
client.

```python
# scripts/migrate-vapi-server-url.py — write before this stage
import os, requests
NEW = "https://api-coolify.callmeie.ie"
OLD = "https://callmeie.onrender.com"
KEY = os.environ["VAPI_API_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
for asst in requests.get("https://api.vapi.ai/assistant", headers=H).json():
    cur = asst.get("serverUrl") or ""
    if OLD not in cur:
        continue
    # read full assistant first to preserve model.tools (the documented
    # tool-stripping landmine — see receptionist/CLAUDE.md)
    full = requests.get(f"https://api.vapi.ai/assistant/{asst['id']}", headers=H).json()
    new_url = cur.replace(OLD, NEW)
    requests.patch(f"https://api.vapi.ai/assistant/{asst['id']}",
                   headers={**H, "Content-Type": "application/json"},
                   json={"serverUrl": new_url, "model": full["model"]})
    print(f"updated {asst['name']}: {cur} → {new_url}")
```

Run it. Check Vapi dashboard — every assistant's serverUrl is now
api-coolify.callmeie.ie.

#### 3.2 Stripe webhook endpoint

Stripe dashboard → Developers → Webhooks → existing endpoint
`https://callmeie.onrender.com/owl/stripe/webhook`:

- Add NEW endpoint: `https://api-coolify.callmeie.ie/owl/stripe/webhook`
- Same events list per INFRA §4
- Get the new signing secret, set `OWL_STRIPE_WEBHOOK_SECRET` on
  Coolify (overwriting the Render one — they're different secrets per
  endpoint)
- Disable (NOT delete) the old Render endpoint — keep for 24h
  rollback grace

In-flight webhook events that hit Render during this 30-second window
are idempotent thanks to `stripe_events` table dedupe — confirmed by
the security audit per INFRA. So no duplicate-charge risk.

#### 3.3 Onboarding form endpoint

`callmeie-hub/receptionist/onboard.html` line ~938 hardcodes Railway
URL: `const API='https://callmeie-webhook-production.up.railway.app';`.

Per V1 audit + DPA P2-10 commit: Railway is a webhook relay forwarding
to Render. Two paths:

- **Path A (faster):** point Railway directly at Coolify (Railway
  dashboard env var). One DNS-equivalent change at Railway.
- **Path B (cleaner):** drop Railway entirely. Edit onboard.html to
  POST direct to `https://api-coolify.callmeie.ie/submit-onboarding`.
  Update DPA to remove Railway from sub-processor list.

Recommend Path B — fewer hops, fewer sub-processors, faster legal.

```diff
- const API='https://callmeie-webhook-production.up.railway.app';
+ const API='https://api-coolify.callmeie.ie';
```

#### 3.4 DNS swap

Cloudflare DNS → `api.callmeie.ie` CNAME → currently Render's
`callmeie.onrender.com`. Change CNAME to point at Coolify FQDN
(`api-coolify.callmeie.ie`) OR direct Hetzner A-record.

Cloudflare TTL: typically 5 min on first switch. Longer-lived cached
clients re-resolve within the hour.

#### 3.5 Onboard form receptionist code references

Discovery widget endpoint in `_partials/cohesion-chatbot.html:336`:

```js
var DISCOVERY_ENDPOINT = 'https://callmeie.onrender.com/api/discovery';
```

Two paths:
- Wait for DNS to settle, change to `https://api.callmeie.ie/api/discovery`
- OR hardcode to `https://api-coolify.callmeie.ie/api/discovery` for
  belt-and-braces

Recommend the api.callmeie.ie alias — once DNS lands, this is the one
URL all the dashed sub-domains can roll under.

Same pattern for any other client-side hardcoded
`callmeie.onrender.com` — grep + replace.

### Stage 4 — Decommission

**Wait 7 days minimum** after Stage 3 success.

- Delete the disabled Stripe webhook endpoint (cleanup)
- Render dashboard → Suspend the service (don't delete — keep config
  as 30-day rollback insurance)
- Update INFRA.md §3 → mark Render decommissioned, point all flow
  diagrams at Coolify
- Update legal/privacy.html + legal/dpa.html → remove Render from
  sub-processor list (P0-6 work continues here)
- Update receptionist/SYSTEM.md operator reference

---

## 3 · Risks + mitigations

| Risk | Mitigation |
|---|---|
| Coolify Postgres password lost mid-migration | Pull via API immediately after creation, vault into `~/.claude/routes/.env` BEFORE running env-var script |
| Vapi tool stripping | Use the GET-merge-PATCH pattern (per receptionist/CLAUDE.md) — never bare-PATCH `model.messages` |
| Stripe webhook double-fire (in-flight) | `stripe_events` table dedupe handles it — confirmed up to alembic 0001 (security_hardening) per V3 |
| Customer Vapi calls during cutover | They route through Twilio → Vapi runtime → serverUrl. Stage 3.1 cuts serverUrl FIRST, so calls work continuously. The 30-sec window where serverUrl is updating: in-flight tool callbacks may 502; Vapi retries. Acceptable. |
| DNS propagation drag | Cloudflare ~5 min, but some recursive DNS caches longer. Render keeps running 7 days as the safety net. |
| Coolify Postgres data loss | Coolify auto-backups can be enabled; verify before Stage 3. Also: callmeie-fix tables are append-only event logs (call_events, owl_leads) — historic loss is acceptable, ongoing loss is not. |
| Telegram + Sheets backups still pointed at Render | Both are SENT FROM the FastAPI to external services — they don't care which host originated them. No change. |
| /tmp SQLite data loss | Already documented at INFRA §3 line 147 as acceptable ("source of truth is email/SMS notifications"). The migration is the right time to ship persistent Postgres anyway. |

---

## 4 · Adam-step checklist (the parts I can't do alone)

Stages 1-2 I can drive via Coolify API + git push. Stages 3-4 need
Adam at the controls because they touch live customer-facing
infrastructure:

- [ ] **Stage 3.1** Vapi serverUrl swap — review the script before run, confirm assistants list, watch for tool-stripping
- [ ] **Stage 3.2** Stripe Dashboard — add new webhook endpoint, vault new signing secret, disable old endpoint
- [ ] **Stage 3.3** Railway dashboard / `onboard.html` edit — pick Path A or Path B
- [ ] **Stage 3.4** Cloudflare DNS — `api.callmeie.ie` CNAME swap
- [ ] **Stage 3.5** discovery widget endpoint URL — push the `cohesion-chatbot.html` edit, re-inject, deploy
- [ ] **Stage 4** Render service Suspend (keep config) after 7 days clean

---

## 5 · Test plan (run after Stage 1, again after Stage 3)

```bash
BASE=https://api-coolify.callmeie.ie  # change to https://api.callmeie.ie post-3.4

# Health
curl -i $BASE/health

# Discovery quiz
curl -i $BASE/api/discovery -X POST -H 'Content-Type: application/json' \
  -d '{"page_context":"hub","business":"dental","pain":"missed-calls","team_size":"solo","urgency":"this-month"}'
# expect: 200, recommended_product:"receptionist"

# Discovery rate-limit (5 per IP per hour, P0-8 — should land headers)
for i in 1 2 3 4 5 6; do
  curl -i $BASE/api/discovery -X POST -H 'Content-Type: application/json' \
    -d '{"page_context":"hub","business":"x","pain":"y","team_size":"z","urgency":"a"}' \
    | head -8
done
# expect: first 5 are 200 with X-RateLimit-Remaining counting down; 6th is 429 with Retry-After

# Admin (Adam token)
curl -i "$BASE/admin?token=$ADMIN_TOKEN" | head -20
# expect: 200 + admin.html

# Vapi tool callback simulation
curl -i $BASE/check-availability -X POST -H 'Content-Type: application/json' \
  -d '{"call":{"id":"test"},"assistantId":"adee3d89-99d8-4f58-9dc3-78c38b9f2a7c"}'
# expect: 200 with availability JSON
```

---

## 6 · Files touched by this work

- `MIGRATION-RENDER-TO-COOLIFY.md` (this file)
- `scripts/migrate-vapi-server-url.py` (TODO Stage 3.1 — write before run)
- `scripts/coolify-set-envs.sh` (TODO Stage 1.3 — DO NOT COMMIT, vault only)
- `INFRA.md` (update §3 Render section to mark migration)
- `callmeie-hub/_partials/cohesion-chatbot.html` (Stage 3.5 endpoint URL)
- `callmeie-hub/receptionist/onboard.html` (Stage 3.3 form endpoint)

---

## 7 · Rollback

Each stage is reversible in this window:

- Stage 1-2: just delete the new Coolify app + DB. Render unchanged.
- Stage 3.1: re-run the Vapi script with NEW=Render URL.
- Stage 3.2: disable new Stripe endpoint, re-enable old one.
- Stage 3.3: revert the onboard.html commit.
- Stage 3.4: Cloudflare DNS swap back.
- Stage 4: Resume the suspended Render service from dashboard.

After 7 clean days, Stage 4 makes the Render config the rollback,
and Coolify is the new normal.
