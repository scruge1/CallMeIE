# INFRA · Owl Studio + CallMeIE — canonical reference

> Authoritative source of truth for every piece of Owl Studio + CallMeIE
> infrastructure. If anything is NOT in this file, it doesn't exist yet.
> Every service, every URL, every API key LOCATION (never values), every
> endpoint, every DB table.
>
> **Rule:** when anything is added / changed / removed, update this file
> in the SAME commit. If you're reading this in a future session and
> something's missing, the last session didn't write it down —
> immediately add it here before continuing.

---

## 1 · Hetzner VPS (shared home for everything self-hosted)

| Field | Value |
|---|---|
| Name | `ubuntu-4gb-nbg1-8` |
| Provider | Hetzner Cloud (Nuremberg) |
| Spec | 4 GB RAM · Ubuntu |
| IPv4 | `178.104.205.255` |
| IPv6 | `2a01:4f8:1c18:5cf5::/64` |
| SSH user | `root` |
| SSH password | `$HETZNER_ROOT_PASSWORD` in `~/.claude/routes/.env` |
| Hetzner API key | **not yet saved** — user has it; paste into `HCLOUD_TOKEN` in routes/.env when available |
| Hetzner Cloud Console login | **NOT IN VAULT** — `HETZNER_CLOUD_EMAIL` / `HETZNER_CLOUD_PASSWORD` placeholders added 2026-05-04. Currently logged-in Hetzner account at project 14229666 shows ZERO servers — AX52 (`ubuntu-4gb-nbg1-8`, 178.104.205.255 / `CLOUD-NBG1`) is owned by a DIFFERENT Hetzner account. Need correct account creds for Cloud Console + Object Storage bucket creation. |
| Coolify dashboard login | **IN VAULT (resolved 2026-05-08)** — `COOLIFY_DASHBOARD_EMAIL=scruge@pm.me` / `COOLIFY_DASHBOARD_PASSWORD` in `~/.claude/routes/.env`. Adam never set a password during install ("you set up Coolify completely on your own"); password generated + bcrypt-hashed + DB-UPDATEd this session — see §14.1d for runbook. Needed for: proxy restart, Terminal tab, Persistent Storage Directory Mount UI (API rejects volume-mount fields — see §14.1c), FQDN cascade fix #6281. API tokens (`COOLIFY_API_ROOT_TOKEN`) work for most automation but the above three need dashboard. |
| Claude Code SSH pubkey | `CLAUDE_CODE_PUBKEY` in routes/.env. Paste into AX52 `/root/.ssh/authorized_keys` via Hetzner Cloud Console "Server > Console" web tty to unlock paramiko key-auth from this machine. Avoids relying on root password (currently password auth fails — fail2ban or rotated). |

**What lives here (see each section below):**
- Coolify control plane (port 8000)
- owltradezone (owlzone.trade)
- **Owl Studio services:** Vaultwarden, Uptime Kuma, Umami

---

## 2 · Coolify (self-hosted PaaS on the Hetzner box)

> **AUD-022 (2026-04-30):** Dashboard now reachable at `https://coolify.owlzone.trade` with Let's Encrypt auto-renewal. Plain HTTP `:8000` retained as escape hatch for 7 days; firewall close decision documented in §13. API token transmitted only over HTTPS going forward.

| Field | Value |
|---|---|
| URL | `https://coolify.owlzone.trade` (primary, AUD-022) · `http://178.104.205.255:8000` (escape hatch, retained 7d) |
| Version | `v4.0.0-beta.473` |
| Deploy target | server uuid `mihuu5scwb1y3gja1lik7tp9` (localhost) |
| Project | "My first project" uuid `m100nrzbdx92dn8kxzvrmhpy` |
| Environment | `production` |
| API root token | `$COOLIFY_API_ROOT_TOKEN` in `~/.claude/routes/.env` — **write-capable** (created via direct DB insert with `root` ability in prior session) |
| API read token | `$COOLIFY_API_READ_TOKEN` in `~/.claude/routes/.env` — read-only |
| APP_KEY (Laravel) | `$COOLIFY_APP_KEY` in `~/.claude/routes/.env` — needed for decrypting env vars stored in Coolify's DB |

### 2.1 Coolify API reference

```
# list services
curl -s -H "Authorization: Bearer $COOLIFY_API_ROOT_TOKEN" \
  http://178.104.205.255:8000/api/v1/services

# create one-click service (type = vaultwarden | uptime-kuma | umami | ...)
curl -X POST -H "Authorization: Bearer $COOLIFY_API_ROOT_TOKEN" \
  -H "Content-Type: application/json" \
  http://178.104.205.255:8000/api/v1/services \
  -d '{"type":"vaultwarden","name":"foo","project_uuid":"m100nrzbdx92dn8kxzvrmhpy","server_uuid":"mihuu5scwb1y3gja1lik7tp9","environment_name":"production"}'

# start / stop / restart / deploy
curl -H "Authorization: Bearer $TOK" $API/services/{uuid}/start
curl -H "Authorization: Bearer $TOK" $API/services/{uuid}/stop
curl -H "Authorization: Bearer $TOK" $API/services/{uuid}/restart
curl -H "Authorization: Bearer $TOK" "$API/deploy?uuid={uuid}&force=true"

# list env vars (for setting SERVICE_FQDN_*, etc.)
curl -H "Authorization: Bearer $TOK" $API/services/{uuid}/envs
curl -X PATCH -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  $API/services/{uuid}/envs -d '{"key":"FOO","value":"bar"}'
```

### 2.2 Services running under Coolify

| Name | Type | UUID | Status | Public URL (current) | Target URL |
|---|---|---|---|---|---|
| owltradezone | application (git) | `kvpvd10evtfhn074p0kgk525` | running | `https://owlzone.trade` | same |
| owl-vaultwarden | service (vaultwarden) | `hx8st0ta4xecr0d0cm2b5l44` | running:healthy | `http://vaultwarden-hx8st0ta4xecr0d0cm2b5l44.178.104.205.255.sslip.io` | `https://vault.owlzone.trade` (DNS added, Traefik label update pending) |
| owl-uptime-kuma | service (uptime-kuma) | `t10jb009nm5e36oy1n8bki97` | running:healthy | `http://uptimekuma-t10jb009nm5e36oy1n8bki97.178.104.205.255.sslip.io` | `https://uptime.owlzone.trade` (same) |
| owl-umami | service (umami) | `txd1tt0zup0yckhlfojdf301` | running:healthy | `http://umami-txd1tt0zup0yckhlfojdf301.178.104.205.255.sslip.io` | `https://analytics.owlzone.trade` (same) |

**Solved workaround (2026-04-21) — documented Coolify v4 bug [coollabsio/coolify#6281](https://github.com/coollabsio/coolify/issues/6281):** the API's env-var endpoint updates `SERVICE_FQDN_*` but does NOT cascade to `service_applications.fqdn` in the DB, so Traefik keeps routing only the original sslip.io hostname. Running this recipe gets all custom domains live with Let's Encrypt SSL:

```bash
# 1. SSH to the box, update fqdn column directly in Coolify Postgres
ssh root@178.104.205.255  # password from ~/.claude/routes/.env
docker exec coolify-db psql -U coolify -d coolify -c   "UPDATE service_applications SET fqdn = 'https://vault.owlzone.trade' WHERE id = 1;"
# (repeat per app_id / target domain; use id from `SELECT id, name, fqdn FROM service_applications`)

# 2. Force redeploy — regenerates docker_compose + Traefik labels from new fqdn
TOK="$COOLIFY_API_ROOT_TOKEN"; API="http://178.104.205.255:8000/api/v1"
curl -H "Authorization: Bearer $TOK" "$API/deploy?uuid=<service_uuid>&force=true"

# 3. Wait ~90s for Let's Encrypt. Verify: curl -I https://<custom> → 200 / 302
```

The table is `service_applications` (NOT `services_applications`). `custom_labels` column does not exist on this table — Traefik labels live inside `services.docker_compose` and regenerate on force-deploy. See `scripts/fix-coolify-fqdn.py` for the automated version of this flow.

---

## 3 · Render (cloud runtime — to migrate to Coolify later)

| Field | Value |
|---|---|
| Service name | `CallMeIE` |
| Service UUID | `srv-d75f7luuk2gs73d8b79g` |
| URL | `https://callmeie.onrender.com` |
| Repo | `scruge1/CallMeIE` main branch |
| Runtime | Python 3 · FastAPI (`scripts/server.py`) · `uvicorn server:app --host 0.0.0.0 --port $PORT` |
| Dashboard | https://dashboard.render.com/web/srv-d75f7luuk2gs73d8b79g |
| API key | `$RENDER_API_KEY` in `~/.claude/routes/.env` |

**Env vars currently set on Render:** `ADMIN_TOKEN`, `ANTHROPIC_API_KEY`, `CALLMEIE_BACKUP_SHEET_ID`, `CALLMEIE_BACKUP_SHEET_TAB`, `CALLMEIE_CALLBACK_CALENDAR_ID`, `CALLMEIE_TIMEZONE`, `CLIENTS_JSON`, `DB_PATH`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `OWNER_NOTIFICATION_NUMBER`, `OWL_OWNER_TOKEN`, `OWL_STRIPE_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `VAPI_API_KEY`.

**Deploy model:** push to `main` on `scruge1/CallMeIE` → Render auto-deploys in 3-5 min.

### 3.1 FastAPI routes (live at callmeie.onrender.com)

**CallMeIE (original — AI phone receptionist):**
- `POST /capture-lead` — Vapi Claire qualifier
- `POST /demo-complete` — Vapi demo assistant wrap-up
- `POST /vapi/call-ended` — post-call webhook
- `POST /check-availability`, `POST /book-appointment` — Vapi calendar
- `POST /submit-onboarding` — onboard.html form
- `GET /admin?token=` — CallMeIE admin portal
- `GET /admin/api/events`, `GET /admin/api/diagnoses`, `GET /admin/api/submissions`
- `POST /admin/api/provision/{id}` — one-click client provisioning

**Owl Studio (added 2026-04-21):**
- `POST /owl/submit` — public form endpoint for every client site
- `POST /owl/care/ticket` — care-plan edit intake
- `GET /owl/admin?token=` — per-client dashboard (token-scoped to one site)
- `POST /owl/sites`, `GET /owl/sites` — owner-only site registration + fleet view
- `GET /owl/health/{site_id}` — UptimeRobot/Uptime-Kuma pollable
- `POST /owl/stripe/webhook` — Stripe events → auto-update `owl_sites.care_tier`

### 3.2 Tables in `/tmp/callmeie.db`

CallMeIE: `submissions`, `call_events`, `leads`, `call_diagnostics`
Owl Studio: `owl_sites`, `owl_leads`, `owl_tickets`, `owl_payments`

**⚠ Render free tier `/tmp` wipes on redeploy.** Migration to persistent Postgres is on the backlog — source of truth for leads is still the email/SMS notifications, DB is convenience-log only.

---

## 4 · Stripe (live mode — CallMeIE IE account)

| Field | Value |
|---|---|
| Account ID | `acct_1THUzrCEqG2AuI1z` |
| Country | IE · EUR |
| Secret key | `$STRIPE_API` in `~/.claude/routes/.env` · `sk_live_…` 107 chars |
| Webhook URL | `https://callmeie.onrender.com/owl/stripe/webhook` |
| Webhook signing secret | `$OWL_STRIPE_WEBHOOK_SECRET` (also set on Render env) |
| Webhook events | `checkout.session.completed`, `customer.subscription.*`, `invoice.paid`, `invoice.payment_failed` |

### 4.1 Products (all with metadata `owl_tag: owl-studio`)

| Owl key | Stripe product | Prices (lookup keys) | Amount |
|---|---|---|---|
| `audit` | Owl Studio · €99 style-match audit | `audit-once` | €99 one-off |
| `care-essential` | Owl Studio · Essential care plan | `care-essential-monthly`, `care-essential-yearly` | €45/mo · €450/yr |
| `care-growth` | Owl Studio · Growth care plan | `care-growth-monthly`, `care-growth-yearly` | €95/mo · €950/yr |
| `care-concierge` | Owl Studio · Concierge care plan | `care-concierge-monthly`, `care-concierge-yearly` | €195/mo · €1,950/yr |
| `site-starter-deposit` | Owl Studio · Starter site deposit (€348) | `site-starter-deposit` | €348 one-off (AUD-019) |
| `site-pro-deposit` | Owl Studio · Pro site deposit (€798) | `site-pro-deposit` | €798 one-off (AUD-019) |

### 4.2 Payment Links (saved in `~/.claude/routes/.env`)

| Key | URL env var |
|---|---|
| `audit-once` | `OWL_STRIPE_LINK_AUDIT` |
| `care-essential-monthly` | `OWL_STRIPE_LINK_CARE_ESSENTIAL_MONTHLY` |
| `care-essential-yearly` | `OWL_STRIPE_LINK_CARE_ESSENTIAL_YEARLY` |
| `care-growth-monthly` | `OWL_STRIPE_LINK_CARE_GROWTH_MONTHLY` |
| `care-growth-yearly` | `OWL_STRIPE_LINK_CARE_GROWTH_YEARLY` |
| `care-concierge-monthly` | `OWL_STRIPE_LINK_CARE_CONCIERGE_MONTHLY` |
| `care-concierge-yearly` | `OWL_STRIPE_LINK_CARE_CONCIERGE_YEARLY` |
| `site-starter-deposit` | `OWL_STRIPE_LINK_SITE_STARTER_DEPOSIT` (AUD-019 — pending provisioner run) |
| `site-pro-deposit` | `OWL_STRIPE_LINK_SITE_PRO_DEPOSIT` (AUD-019 — pending provisioner run) |

#### AUD-019 — site-build deposit Payment Links runbook

After running `python scripts/provision-stripe.py` to mint the two new
deposit Payment Links:

1. Vault: paste output URLs into `~/.claude/routes/.env`:
   ```
   OWL_STRIPE_LINK_SITE_STARTER_DEPOSIT=https://buy.stripe.com/...
   OWL_STRIPE_LINK_SITE_PRO_DEPOSIT=https://buy.stripe.com/...
   ```
2. Edit `interactive-gallery.html`:
   - Line ~1636 (Starter CTA): replace
     `href="mailto:callmeie@proton.me?subject=Starter%20website%20%E2%80%94%20%E2%82%AC695"`
     with the Starter Payment Link URL.
   - Line ~1655 (Pro CTA): replace
     `href="mailto:callmeie@proton.me?subject=Pro%20website%20%E2%80%94%20%E2%82%AC1595"`
     with the Pro Payment Link URL.
   - Line ~1671 (Custom CTA): replace `mailto:` with the existing
     `OWL_STRIPE_LINK_AUDIT` URL (€99 audit, credited against final Custom invoice).
3. Commit + push to `main` (GitHub Pages auto-deploys in ~30s).
4. Stripe webhook (`/owl/stripe/webhook`) already handles
   `checkout.session.completed` for these new prices — payment lands in
   `owl_payments` table with the deposit amount.

**Provisioner:** `C:/Users/a33_s/Desktop/callmeie-fix/scripts/provision-stripe.py` — idempotent, re-run safe.

### 4.3 Receptionist products (provisioned 2026-05-11, `owl_tag: callmeie`)

Path B per `RESUME-PRD-2026-05-11-VAPI-RECEPTIONIST-FUNNEL.md` — parallel surface to legacy `tier-*` 4-tier ladder (which has 0 subscribers and stays untouched, `owl_tag: ai-agency`). New products power the demo-line signup-on-call flow.

| Stripe product ID | Owl key | Price lookup_key | Price ID | Amount |
|---|---|---|---|---|
| `prod_UUxsSqZN7xj7Xz` | `receptionist-professional` | `receptionist-professional-monthly` | `price_1TVxwgCEqG2AuI1zPZzlP7q3` | €249/mo |
| `prod_UUxsiM0MV4y8v3` | `receptionist-growth` | `receptionist-growth-monthly` | `price_1TVxwgCEqG2AuI1zzsCH9YG1` | €397/mo |
| `prod_UUxswYBiVAW1rU` | `receptionist-setup` | `receptionist-setup-once` | `price_1TVxwgCEqG2AuI1zkxlRvISY` | €297 one-off |

**Static Payment Link (setup only):** `https://buy.stripe.com/14A9AS6in6CGenO0zraIM0j` — Adam can SMS this directly when bundling not needed.

**Vault keys** (in `~/.claude/routes/.env`):
```
STRIPE_RECEPTIONIST_PROFESSIONAL_MONTHLY=price_1TVxwgCEqG2AuI1zPZzlP7q3
STRIPE_RECEPTIONIST_GROWTH_MONTHLY=price_1TVxwgCEqG2AuI1zzsCH9YG1
STRIPE_RECEPTIONIST_SETUP_ONCE=price_1TVxwgCEqG2AuI1zkxlRvISY
STRIPE_RECEPTIONIST_LINK_SETUP_ONCE=https://buy.stripe.com/14A9AS6in6CGenO0zraIM0j
```

**Render env vars** (must mirror vault — endpoint returns 500 without):
- `STRIPE_RECEPTIONIST_PROFESSIONAL_MONTHLY`
- `STRIPE_RECEPTIONIST_GROWTH_MONTHLY`
- `STRIPE_RECEPTIONIST_SETUP_ONCE`

**Endpoint:** `POST /admin/api/send-setup-link?token=$OWL_OWNER_TOKEN` in `callmeie-fix/scripts/server.py` (commit `521bfb7`). Body `{phone, tier, include_setup}` → Stripe Checkout Session (mode=subscription, mixed line_items: recurring tier + one-off setup on first invoice) → SMS URL to caller via Twilio. Webhook `/owl/stripe/webhook` handles `checkout.session.completed` (no changes).

**Provisioner:** `callmeie-fix/scripts/provision-stripe-receptionist.py` — idempotent, re-run safe.

---

## 5 · Porkbun (DNS)

| Field | Value |
|---|---|
| Domain | `owlzone.trade` |
| API key (public) | `$PUBLIC_KEY` in `~/.claude/routes/.env` (under `# Porkbun`) |
| API secret | `$PRIVATE_KEY` in `~/.claude/routes/.env` (under `# Porkbun`) |

### 5.1 Current DNS records for `owlzone.trade`

| Subdomain | Type | Target | Purpose |
|---|---|---|---|
| `@` | A | 178.104.205.255 | owlzone.trade main (Hetzner) |
| `www` | CNAME | (check Porkbun) | (check) |
| `websites` | CNAME | `scruge1.github.io` | Owl Studio sales site (GitHub Pages) |
| `vault` | A | 178.104.205.255 | Vaultwarden (added 2026-04-21) |
| `uptime` | A | 178.104.205.255 | Uptime Kuma (added 2026-04-21) |
| `analytics` | A | 178.104.205.255 | Umami (added 2026-04-21) |
| `*.websites` | A | 178.104.205.255 | Lead sites wildcard — `{slug}.websites.owlzone.trade` → VPS nginx container (added 2026-04-25) |

### 5.2 Porkbun API reference

```
# add A record
curl -X POST https://api.porkbun.com/api/json/v3/dns/create/$DOMAIN \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PUBLIC_KEY\",\"secretapikey\":\"$PRIVATE_KEY\",\"type\":\"A\",\"name\":\"$SUB\",\"content\":\"$IP\",\"ttl\":\"300\"}"

# list all
curl -X POST https://api.porkbun.com/api/json/v3/dns/retrieve/$DOMAIN \
  -H "Content-Type: application/json" \
  -d "{\"apikey\":\"$PUBLIC_KEY\",\"secretapikey\":\"$PRIVATE_KEY\"}"
```

---

## 6 · GitHub Pages (client + sales sites)

> **SUPERSEDED 2026-05-03:** Owl Studio brand retired, web design merged into Callmeie Technologies as the AI-First Websites product (per BRAND-DOMAIN-CONSOLIDATION-PRD §0.3, executed 2026-05-03). New canonical sales surface: `https://callmeie.ie/websites/` (folded into scruge1/callmeie-hub). Old `https://websites.owlzone.trade` repo serves redirect stubs at `/` and `/interactive-gallery.html` to new URL. 7 demo dirs + 12 industry sample HTMLs copied into callmeie-hub on commit aa3d819 with brand sweep. Section kept for historical reference.

| Site | Repo | URL | Notes |
|---|---|---|---|
| AI-First Websites (sales) | `scruge1/callmeie-hub` | `https://callmeie.ie/websites/` | path-based monorepo (active 2026-05-03) |
| ~~Owl Studio sales~~ | `scruge1/owl-studio-website-directions` | ~~https://websites.owlzone.trade~~ → redirect to callmeie.ie/websites/ | RETIRED 2026-05-03; repo carries redirect stubs only |
| CallMeIE | `scruge1/CallMeIE` | ~~https://callmeie.ie~~ → folded into `scruge1/callmeie-hub` at `/receptionist/` | RETIRED 2026-05-03 (path-based monorepo cutover) |

**PAT:** `$GITHUB_TOKEN` in `~/.claude/routes/.env` (verify name — may be `GITHUB_PAT` or similar).

---

## 7 · Owl Studio registered sites (client fleet)

> **SUPERSEDED 2026-05-03:** site_id values KEPT for backend continuity per BRAND-DOMAIN-CONSOLIDATION-PRD §0.6 ("existing Owl Studio Stripe products keep their owl_tag: owl-studio metadata for billing continuity; future products tag callmeie. No retagging churn"). Display names + URLs updated to new path-based scheme. Admin tokens unchanged.

Seed data — each has its own admin token + dashboard.

| site_id | display_name | tier | care_tier | live_url | admin token env var |
|---|---|---|---|---|---|
| `owl-studio-sales` | AI-First Websites · Sales (was Owl Studio · Sales) | starter | — | https://callmeie.ie/websites/ (was websites.owlzone.trade) | `OWL_ADMIN_TOKEN_OWL_STUDIO_SALES` |
| `rathborne-dental-demo` | Rathborne Dental (demo) | pro | growth | https://callmeie.ie/websites/samples/industries/01-dental-swiss.html | `OWL_ADMIN_TOKEN_RATHBORNE_DENTAL_DEMO` |
| `vetcare-limerick-preview` | Limerick Vet Clinic (preview) | — | — | https://callmeie.ie/websites/demos/vetcare-limerick/ | — (not yet registered as client) |

**Owner fleet dashboard:** `https://callmeie.onrender.com/owl/sites?token=$OWL_OWNER_TOKEN`

**Owner admin URL template:** `https://callmeie.onrender.com/owl/admin?token=<site-specific-token>`

---

## 8 · Credential vault — `~/.claude/routes/.env`

This file is the ONE place every secret lives. Not committed to git. Structure:

```
# Owl Studio backend (provisioned 2026-04-21)
OWL_OWNER_TOKEN=...
OWL_ADMIN_TOKEN_OWL_STUDIO_SALES=...
OWL_ADMIN_TOKEN_RATHBORNE_DENTAL_DEMO=...

# Stripe Payment Links + webhook secret
OWL_STRIPE_WEBHOOK_SECRET=whsec_...
OWL_STRIPE_LINK_AUDIT=https://buy.stripe.com/...
... (6 more care links)

# Hetzner + Coolify
HETZNER_SERVER_IPV4=178.104.205.255
HETZNER_ROOT_PASSWORD=...
COOLIFY_API_ROOT_TOKEN=3|...
COOLIFY_API_READ_TOKEN=1|...
COOLIFY_APP_KEY=base64:...

# Provider APIs
STRIPE_API=sk_live_...      (CallMeIE IE account, live mode)
RENDER_API_KEY=rnd_...
PUBLIC_KEY=...              (Porkbun API key - NOT CallMeIE account)
PRIVATE_KEY=...             (Porkbun API secret)
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=...
VAPI_API_KEY=...

# ... plus whatever else lives here (Clerk, Anthropic, etc.)
```

**When adding a new secret:** append to this file AND reference it by name here in the infra doc.

---

## 9 · Operational runbooks

### 9.1 Onboard a new Owl Studio client

```bash
# Register site
curl -X POST "https://callmeie.onrender.com/owl/sites?token=$OWL_OWNER_TOKEN" \
  -H "Content-Type: application/json" -d '{
    "site_id": "<slug>",
    "display_name": "...",
    "tier": "starter|pro|custom",
    "care_tier": "essential|growth|concierge",
    "lead_email": "...",
    "lead_sms": "+353...",
    "edit_emails": ["..."],
    "live_url": "https://..."
  }'
# Save the returned admin_url + admin_token to Vaultwarden under
# "OwlStudio · <ClientName>" vault folder.

# Paste the embed snippet (returned by /owl/sites) onto each page with a
# <form data-owl>. Form will POST to /owl/submit with site_id prefilled.

# Add DNS:
curl -X POST "https://api.porkbun.com/api/json/v3/dns/create/owlzone.trade" \
  -H "Content-Type: application/json" -d "{\"apikey\":\"$PUBLIC_KEY\",\"secretapikey\":\"$PRIVATE_KEY\",\"type\":\"A\",\"name\":\"<sub>\",\"content\":\"178.104.205.255\",\"ttl\":\"300\"}"

# Add to Uptime Kuma — log in at http://178.104.205.255:3001, new monitor
# type HTTP(s), URL = https://<client-site>, interval 5 min, alert on 2
# consecutive failures. (Once uptime.owlzone.trade is live, swap URL.)
```

### 9.2 Provision a Stripe product + link (already done once, re-runnable)

```bash
source ~/.claude/routes/.env
export STRIPE_API   # ensure it's exported
python C:/Users/a33_s/Desktop/callmeie-fix/scripts/provision-stripe.py
# Idempotent — only creates what's missing.
```

### 9.3 Deploy a new Coolify service (one-click template)

```bash
TOK=$COOLIFY_API_ROOT_TOKEN
API="http://178.104.205.255:8000/api/v1"
SERVER="mihuu5scwb1y3gja1lik7tp9"
PROJECT="m100nrzbdx92dn8kxzvrmhpy"

# 1. Create (replace <type> with: vaultwarden | uptime-kuma | umami | plausible | cal-com | etc.)
curl -X POST -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  "$API/services" \
  -d "{\"type\":\"<type>\",\"name\":\"owl-<name>\",\"project_uuid\":\"$PROJECT\",\"server_uuid\":\"$SERVER\",\"environment_name\":\"production\"}"

# 2. Start (returned UUID from step 1)
curl -H "Authorization: Bearer $TOK" "$API/services/<uuid>/start"

# 3. (Optional) set custom domain via SERVICE_FQDN_<NAME> env var
curl -X PATCH -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  "$API/services/<uuid>/envs" \
  -d "{\"key\":\"SERVICE_FQDN_<NAME>\",\"value\":\"<sub>.owlzone.trade\"}"

# 4. Deploy (regenerates compose + Traefik labels)
curl -H "Authorization: Bearer $TOK" "$API/deploy?uuid=<uuid>&force=true"

# 5. Add DNS A record via Porkbun (see §5.2)
```

### 9.4 Redeploy CallMeIE (Render)

```bash
# Push any commit to scruge1/CallMeIE main — Render auto-deploys.
# Or force-redeploy current main via API:
python C:/Users/a33_s/Desktop/callmeie-fix/scripts/provision-owl-backend.py
# (That script also provisions env vars + seed sites — idempotent.)
```

### 9.5 When a Stripe subscription lands

- Webhook fires → `/owl/stripe/webhook` → `owl_sites.care_tier` auto-updated
- Owner gets SMS: `OwlStudio Stripe * paid * <product_key> * <amount> <currency>`
- Check `GET /owl/sites?token=$OWL_OWNER_TOKEN` to confirm

---

## 9.6 Client-site generation from Business Profile JSON

Schema + renderer live.

```bash
python scripts/render-site.py --profile rathborne-dental
# profiles/rathborne-dental.json + templates/01-dental-swiss.template.html
# -> client-builds/rathborne-dental/index.html
```

Supports: `{{key}}`, `{{nested.path}}`, `{{{html-safe}}}`, `{{#loop list}}...{{/loop}}`.
Stage-1 (6 fields, signup) vs Stage-2 (remaining, post-commit discovery).
`completeness{}` per section so partial profiles still render.
Full spec: `BUSINESS-PROFILE-SCHEMA.md`.

---

## 10 · Backend backlog (what's built vs what's left)

### Shipped

- [x] `/owl/submit` endpoint (Day 1)
- [x] `/owl/admin?token=` dashboard (Day 2)
- [x] `/owl/care/ticket` intake
- [x] `/owl/sites` + `/owl/health/:site_id`
- [x] `scripts/provision-owl-backend.py` — Render API one-shot
- [x] Stripe products + prices + payment links + webhook (Day 3)
- [x] `/owl/stripe/webhook` handler + `owl_payments` table
- [x] Quote modal on sales page, 24 CTAs wired
- [x] Live contact form on sales page → `/owl/submit`
- [x] Vaultwarden / Uptime Kuma / Umami deployed on Coolify

### Remaining

- [ ] **Trim `onboard.html` to Stage 1 (mother's feedback 2026-04-21)** — signup form currently asks ~15 fields + 3 FAQ repeaters before commit. Trim to 6: business_name, contact_name, contact_phone, contact_email, business_type (dropdown), plan tier. Move address/hours/services/insurance/FAQ to a post-payment welcome-call capture. Preserve full form as `onboard-details.html` for stage-2 use. Backend unchanged — `/submit-onboarding` already defaults missing fields to empty. Full rationale in `owl-studio-website-directions/BUSINESS-PROFILE-SCHEMA.md` §2.5.

- [ ] **Cascade SERVICE_FQDN_* to app sub-resource** — manual Coolify dashboard step OR SSH DB update for vault / uptime / analytics domains to go live via HTTPS
- [ ] **Weekly/monthly PDF report cron** (Day 4) — leads + uptime + tickets per site
- [ ] **Full onboarding CLI** (Day 5) — `python onboard.py --site X …` chains site registration + UptimeRobot/Uptime-Kuma monitor creation + Stripe customer + Vaultwarden vault folder + admin-URL email
- [ ] **Wire the 10 industry sample contact forms** to `/owl/submit` — demonstrate "your leads would land here"
- [x] **DONE 2026-04-21 - migrated to Postgres.** Coolify-hosted `owl-studio-postgres` (UUID jcgjvf0o55exgw69akj0uk5d, Postgres 16, public port 5433 on 178.104.205.255). server.py now auto-switches backends via `DATABASE_URL` env var: psycopg if set, sqlite3 fallback for local dev. DDL translated in-flight via `_ddl_fix()`. 11 sites re-registered durably on Postgres. Next Render redeploy won't wipe.
- [ ] **Payload CMS** — deploy on Coolify when first Pro client signs
- [ ] **Migrate callmeie FastAPI off Render onto Coolify** — when Render free tier gets tight

---

## 11 · What NOT to do (hard-won gotchas)

- **Do not run `git push --force` on `main` of either repo.** Use `--force-with-lease` only when reviewer asks, never on a shared branch.
- **Do not reset OWL_OWNER_TOKEN without updating `~/.claude/routes/.env`** — all registered sites stay, but the owner fleet view locks out.
- **Do not edit `samples/*.html` (legacy)** — those are the bespoke-direction menu referenced in Pro tier. The live sales floor is `samples/industries/*.html`.
- **Do not add `max-width: 100%` to iframes** in `interactive-gallery.html` — `.demo-showcase iframe` is fixed 1440px with `transform: scale()` and must keep that width; its parent has `overflow: hidden` to clip visually.
- **Do not touch owltradezone from Coolify API** — keep Owl Studio services isolated under their own names.
- **Do not use `object-fit: cover` on `.card-stage .card-preview`** — card aspect-ratio is 1440/900 matching the PNG, so any object-fit works, but `cover` is the tested value.
- **Do not repoint DNS with short TTL expecting instant effect** — Porkbun propagates in ~3-5 min, not instant.

---

## 12 · Sources of truth for each project

| Project | Root repo | CLAUDE.md | This file references |
|---|---|---|---|
| Owl Studio sales | `Desktop/claude MCPs/New repos/owl-studio-website-directions` | `owl-studio-website-directions/CLAUDE.md` | §6 Pages site, §7 site registry |
| CallMeIE receptionist | `Desktop/callmeie-fix` | `callmeie-fix/CLAUDE.md` | §3 FastAPI, §4 Stripe, §10 backlog |
| owltradezone | `Desktop/claude MCPs/New repos/owltradezone` | (separate — not managed here) | only §2 Coolify cohabitation |

---

## 13 · Last updated

**2026-04-21 -- autonomous session log (FINAL, end of run):**

Shipped: custom domain cascade, sample forms, Business Profile schema, Day 4 reports, Day 5 CLI, **Postgres migration (P1 DONE)**, **Stage-1 onboard trim (P2 DONE)**, **monthly digest cron via Twilio (P3 DONE)**. Data is now durable across Render redeploys. Mother's feedback closed. Care-plan digest scheduled.

Bugs fixed: PRAGMA (SQLite-only) + row[0] (dict_row) both wrapped in dialect guards.

**2026-04-21 -- trades swap (session tail):**

User said *"I know a lot of people in rubbish removal, man-with-a-van, powerwashing, plumbers, carpenters, electricians etc."* -- swapped two samples to cover the trades/sole-trader audience:

| # | Before | After | Aesthetic |
|---|---|---|---|
| 06 | `06-audiologist-aurora` · Hear Clear Audiology · Aurora Calm | `06-trade-pro-dark-oled` · Murphy Plumbing & Heating · Dark OLED · Industrial | amber #FFB547 warmth on black, RGI/OFTEC creds |
| 10 | `10-aesthetic-clinic-aurora-luxe` · Molyneux Aesthetics · Aurora Luxe | `10-local-services-brutalism` · Clear-Out Limerick · Mondrian Brutalism | primary colours, Archivo Black, WCP-2021-00472 |

Executed by two parallel agents (skill dispatch per `feedback_parallel-skill-dispatch.md`) -- not one template × recolour. Atomic migration via `scripts/swap-trade-samples.py` updated `_owl-nav.js`, `_owl-form.js`, `provision-sample-sites.py`, and `interactive-gallery.html` in one pass.

New admin tokens in vault — values redacted, see `~/.claude/routes/.env`:
- `OWL_ADMIN_TOKEN_MURPHY_PLUMBING=${OWL_ADMIN_TOKEN_MURPHY_PLUMBING}`
- `OWL_ADMIN_TOKEN_CLEAR_OUT_LIMERICK=${OWL_ADMIN_TOKEN_CLEAR_OUT_LIMERICK}`

> **Token rotation history (AUD-001, 2026-04-29):** original values were committed
> in plaintext here and exposed via the public GitHub mirror. Rotated via
> `POST /owl/sites/{site_id}/rotate-admin-token` on 2026-04-29 21:55 UTC; old
> values now return 401. Going forward, NEVER commit literal tokens — env-var
> placeholder syntax only. See `AUDIT-2026-04-29.md` AUD-001 for the full
> remediation runbook.

Old tokens suffixed `_RETIRED` in vault; `hear-clear-demo` + `molyneux-aesthetics-demo` rows set `status='off'` in Postgres (paper trail kept, admin URLs 401 cleanly). PNGs re-rendered at 2x retina (2880×1800) via Playwright.

Deferred backlog:
- P4: template-ise 8 more industry samples (was 9; 2 now live as trades) -- on-demand per vertical at signup time (not upfront).
- P5: Payload CMS on Coolify -- when first Pro client signs.
- One-time manual: add OWL_OWNER_TOKEN to GitHub scruge1/CallMeIE Actions secrets so monthly-owl-digest.yml can fire.

All 14 provisioning/patching scripts idempotent, documented in .9 runbooks.

Next session loads this file via CLAUDE.md chain routing -- no user instruction required.

## 14 · Document Ops Portal (Coolify on Hetzner — LIVE)

| Field | Value |
|---|---|
| Repo | https://github.com/scruge1/document-ops-portal (private) |
| Local repo | C:\Users\a33_s\Desktop\claude MCPs\New repos\document-ops-portal |
| Public URL (primary) | https://portal.callmeie.ie (active 2026-05-02 — M1 migration) |
| Public URL (legacy) | https://portal.owlzone.trade (30-day overlap, retire ~2026-06-02) |
| DNS — callmeie.ie | A record `portal` -> 178.104.205.255 TTL 600 (created 2026-05-02 via GoDaddy API) |
| DNS — owlzone.trade | A record `portal` -> 178.104.205.255 TTL 600 (created 2026-05-01 Porkbun, id 543450612) |
| Coolify app UUID | rs0jyp5cj24hutaxijacye6r |
| Coolify fqdn (multi) | `https://portal.owlzone.trade,https://portal.callmeie.ie` |
| Postgres DB | provisioned and live |
| Magic-link sender | callmeie@proton.me (same SMTP as CallMeIE) |
| LE certs | both domains, auto-renewed by Traefik |

### 14.0 GoDaddy DNS for callmeie.ie

callmeie.ie nameservers point at GoDaddy (`ns45.domaincontrol.com` / `ns46.domaincontrol.com`), NOT Porkbun. DNS edits for callmeie.ie subdomains use the GoDaddy API.

| Field | Value |
|---|---|
| API key var | `GODADDY_KEY` in `~/.claude/routes/.env` |
| API secret var | `GODADDY_SECRET` in `~/.claude/routes/.env` (also stored as legacy `GODADDY_APP`) |
| Auth header | `Authorization: sso-key KEY:SECRET` |
| Base URL | `https://api.godaddy.com/v1/domains/callmeie.ie` |

```powershell
# Add A record on callmeie.ie
$h = @{ Authorization = "sso-key $env:GODADDY_KEY:$env:GODADDY_SECRET"; "Content-Type" = "application/json" }
$body = ConvertTo-Json @(@{ data = "178.104.205.255"; ttl = 600 })
Invoke-RestMethod -Uri "https://api.godaddy.com/v1/domains/callmeie.ie/records/A/<sub>" -Headers $h -Method PUT -Body $body
```

**Future plan:** consolidate callmeie.ie DNS to Porkbun for one-API control. Tracked in `BRAND-DOMAIN-CONSOLIDATION-PRD.md` §3.3.

### 14.1 Coolify multi-FQDN gotcha (resolved 2026-05-02)

Coolify v4 PATCH `/api/v1/applications/{uuid}` rejects field `fqdn` with 422 ("This field is not allowed.") but accepts field `domains` instead — internally maps to the same `applications.fqdn` column. Use comma-separated values to attach multiple domains to one app.

```powershell
$body = '{"domains":"https://portal.owlzone.trade,https://portal.callmeie.ie,https://books.callmeie.ie"}'
Invoke-WebRequest -Uri "$api/applications/$uuid" -Headers $h -Method PATCH -Body $body
# Then force-deploy to regenerate Traefik labels + LE certs
Invoke-RestMethod -Uri "$api/deploy?uuid=$uuid&force=true" -Headers $h
```

LE certs issue automatically after force-deploy (~60s in M1 test).

### 14.1a Coolify env-sync gotcha (resolved 2026-05-08 — `LS_WEBHOOK_SECRET` incident)

**Pattern:** code adds a new required Settings field; vault has the secret; Coolify env DOESN'T → container crashloops on next restart, all hostnames return 503 silently.

**2026-05-08 timeline:**
- 2026-05-07: security commit `805d7a6` made `LS_WEBHOOK_SECRET` REQUIRED in `app/config.py`
- 2026-05-08 ~12:37 UTC: portal.callmeie.ie status `restarting:unknown` — container has been crashlooping for ~24h, undetected
- 2026-05-08 ~13:30 UTC: Phase 5 books.callmeie.ie FQDN PATCH triggered redeploy, exposed the bug
- 2026-05-08 ~14:55 UTC: pushed `LS_WEBHOOK_SECRET` from vault to Coolify env, all 3 hostnames came healthy

**Permanent fix:** `document-ops-portal/scripts/coolify_env_check.py`. Run BEFORE every redeploy. Introspects `app.config.Settings` for required `Field(alias=...)` keys, diffs against Coolify env via `GET /applications/{uuid}/envs`, blocks deploy if any required field is missing. `--sync` mode interactively pushes vault values to Coolify.

```bash
cd document-ops-portal
export DOPS_COOLIFY_APP_UUID="rs0jyp5cj24hutaxijacye6r"
export COOLIFY_API_ROOT_TOKEN="<vault>"
python3 scripts/coolify_env_check.py        # exits 1 if any required missing
python3 scripts/coolify_env_check.py --sync # interactive push
```

Add this to the deploy runbook in `document-ops-portal/PHASE-5-BOOKS-DEPLOYMENT.md` §0.

**Coolify env API quirks:**
- `POST /api/v1/applications/{uuid}/envs` body: `{"key", "value", "is_preview", "is_literal"}` — note: `is_build_time` field rejected with 422; use only the 4 fields listed
- `GET /api/v1/applications/{uuid}/envs` returns a list of `{uuid, key, value, ...}` rows
- Updates take effect on next deploy (Coolify does NOT auto-restart on env-only change — must `POST /api/v1/deploy?uuid=...&force=true`)

### 14.1c Coolify Persistent Storage — UI-only, three mount types (resolved 2026-05-08 — `/opt/callmeie/customers` bind-mount incident)

**Pattern:** code writes to host path (e.g. `/opt/callmeie/customers/`); container has no bind to that path; writes silently land inside container layer; lost on every redeploy. Audit caught at CRIT severity.

**Coolify v4 PATCH `/api/v1/applications/{uuid}` rejects ALL volume-mount field names** tested in 2026-05-08 sweep: `volume_mount`, `bind_mount`, `persistent_storage`, `volumes`, `mounts`, `storage`. `custom_docker_run_options` is accepted but **silently ignored at deploy time** for compose-deploy build packs (Coolify uses `docker compose up`, not `docker run`).

**Workaround = dashboard UI only.** Coolify Persistent Storage offers three distinct mount types under `+ Add`:

| Type | What it does | Use when |
|---|---|---|
| **Volume Mount** | Docker named volume (`/var/lib/docker/volumes/<name>`) | Container-internal data, doesn't need host visibility |
| **File Mount** | Single host file → container file | One config file (e.g. `nginx.conf`) |
| **Directory Mount** | Host directory → container directory (bind mount) | **Customer artifacts, training corpora, anything `ssh root@host` needs to inspect** |

**Adam-keyboard runbook (Chrome MCP automatable):**
1. Coolify dashboard → Project → Application → **Configuration** tab → **Persistent Storage** sidebar
2. **+ Add** dropdown → **Directory Mount**
3. Source Directory = host path (e.g. `/opt/callmeie/customers`); Destination Directory = container path (same path conventional)
4. **Add** → **Restart** (top right) — recreates compose with new mount
5. Verify: `ssh root@178.104.205.255 "docker inspect <container> --format '{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}}{{println}}{{end}}'"` — expect `bind /opt/callmeie/customers -> /opt/callmeie/customers`

**Coolify Restart vs Redeploy semantics (verified 2026-05-08):**
- **Restart** = recreate container with current Coolify config (picks up new mounts, env changes, FQDN changes); does NOT pull git
- **Redeploy** = pull latest git + rebuild image + recreate container; what GitHub Actions triggers via `/api/v1/deploy?uuid=...`
- For mount-only or env-only changes → **Restart** is correct + faster

**Pre-existing host directory:** before clicking Add in UI, ensure host path exists with right perms — `ssh root@host "mkdir -p /opt/callmeie/customers && ls -ld /opt/callmeie/customers"`. Bind mount won't auto-create.

### 14.1d Coolify dashboard password reset (resolved 2026-05-08)

**Pattern:** API automation needs to fall back to dashboard for actions API can't do (Persistent Storage, proxy restart, FQDN cascade fix #6281, Terminal tab). Adam set no password during install. `COOLIFY_DASHBOARD_PASSWORD` was placeholder in vault.

**Reset path 1 (interactive — failed in our case, kept here for reference):**
```bash
ssh -i ~/.ssh/owl_deploy_ed25519 root@178.104.205.255
docker exec -it coolify php artisan root:reset-password
# Prompts for password interactively. -i flag required for stdin.
```

**Reset path 2 (non-interactive bcrypt + DB UPDATE — what worked):**
```bash
# Generate password
NEWPW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)"

# bcrypt hash via Laravel's password_hash inside the coolify container
HASH="$(ssh -i ~/.ssh/owl_deploy_ed25519 root@178.104.205.255 \
  "docker exec coolify php -r 'echo password_hash(\"$NEWPW\", PASSWORD_BCRYPT);'")"

# Update users table directly
ssh -i ~/.ssh/owl_deploy_ed25519 root@178.104.205.255 \
  "docker exec coolify-db psql -U coolify -c \"UPDATE users SET password='$HASH' WHERE email='scruge@pm.me';\""

# Save NEWPW to ~/.claude/routes/.env as COOLIFY_DASHBOARD_PASSWORD
```

**Bash heredoc gotcha:** bcrypt hashes start with `$2y$12$...`. Inside double-quoted strings bash interprets `$2`, `$1`, `$y` as positional/variable refs and mangles the hash. Two fixes:
1. Run the entire HASH-generate + UPDATE chain as a SINGLE ssh command using bash variables on the remote shell — `$HASH` expands on the VPS, never on local Windows
2. Single-quote the SQL string locally, double-quote inside on remote — escape with `\"` boundaries as shown above

**Verification:** browse `https://coolify.owlzone.trade`, log in with `scruge@pm.me` + new password.

### 14.1 Stripe live (provisioned 2026-05-01)

| SKU | Price | Stripe ID | Payment Link |
|---|---:|---|---|
| Pilot Entry | EUR 500 one-time | prod_URIOoFD2CKSupL / price_1TSPnpCEqG2AuI1zwjKLxK9A | https://buy.stripe.com/dRm00i0Y3gdgbbCgypaIM09 |
| Pilot Standard | EUR 1500 one-time | prod_URIOiA6cPqsMLv / price_1TSPnqCEqG2AuI1zZAcghb91 | https://buy.stripe.com/14A3cueOTbX04NeeqhaIM0a |
| ~~Operations Monthly Starter~~ | ~~EUR 250/mo~~ | ~~prod_URIOilIdo12zXe / price_1TSPnsCEqG2AuI1zS5rFSMNJ~~ | ~~https://buy.stripe.com/14AfZg4afaSW5Rici9aIM0b~~ |

> **RETIRED 2026-05-04** — Operations Monthly Starter superseded by Self-serve Auto / Auto Plus / Rescue+Export ladder (§14.1b below). Product KEPT active in Stripe; existing subscribers grandfathered indefinitely. Removed from /docs/ public pricing block. Audit trail only.

Webhook: we_1TSPoBCEqG2AuI1zWijrXFhI -> https://portal.owlzone.trade/webhooks/stripe
Events: checkout.session.completed, customer.subscription.created, customer.subscription.deleted, invoice.payment_failed
Secret stored in outes/.env as DOPS_STRIPE_WEBHOOK_SECRET. All Document Ops Stripe IDs saved under DOPS_STRIPE_* keys.

### 14.1b Document Ops Re-spec — D22 LOCKED (provisioned 2026-05-04 via Stripe API)

Per `document-ops-portal/STRIPE-MIGRATION-SPEC.md` §2 + locked D11-D14. Created via `document-ops-portal/scripts/stripe-d22-execute.ps1` (idempotent, re-runnable). Source-of-truth result file: `document-ops-portal/scripts/stripe-d22-results.json`.

| Tier | Price | Product ID | Price ID | Payment Link |
|---|---:|---|---|---|
| Self-serve Auto | EUR 99/mo | prod_US352mGEO8iaHW | price_1TT8zmCEqG2AuI1zfNupnSjP | https://buy.stripe.com/28EfZggX16CG5Ri3LDaIM0c |
| Self-serve Auto Plus | EUR 249/mo | prod_US36KZjPHRR1Mw | price_1TT8zoCEqG2AuI1zqpYEvsR9 | https://buy.stripe.com/4gMeVccGL7GKdjK95XaIM0d |
| Rescue + Export | EUR 499/mo | prod_US36vqRR6YUvbg | price_1TT8zqCEqG2AuI1zTNhJTbGh | https://buy.stripe.com/28EcN4ayD5yC0wY2HzaIM0e |
| Managed Bespoke (low) | EUR 1500/mo | prod_US36MsoGM8YTwn | price_1TT8zsCEqG2AuI1zFuDj6Bks | (no public link — per-customer Checkout) |
| Managed Bespoke (high) | EUR 2500/mo | prod_US36MsoGM8YTwn | price_1TT8zsCEqG2AuI1z84nYWT8W | (no public link — per-customer Checkout) |

All subscription Payment Links: `tax_behavior=exclusive` (IE VAT applied at checkout), `billing_address_collection=required`, `tax_id_collection.enabled=true`. **`after_completion.hosted_confirmation.custom_message` NOT yet populated via API in 2026-05-04 run** — Adam can edit each Payment Link in Dashboard to paste D14-locked copy (see STRIPE-MIGRATION-SPEC.md §8 D14).

Webhook handler additions for new `tier_slug` metadata routing — see `document-ops-portal/STRIPE-MIGRATION-SPEC.md` §4. Bespoke flow: Adam scopes via email → per-customer Stripe Checkout session against the appropriate Price ID. Slot allocation tracked in this INFRA.md.

**Bespoke slot allocation** (cap=2 active engagements, waitlist when full):
- Slot 1-of-2: OPEN
- Slot 2-of-2: OPEN

Update each slot when an engagement signs.

### 14.2 Migration history

- **2026-05-01:** Initial Coolify provisioning + LE issuance for `portal.owlzone.trade`. Token mint via SSH+tinker (Sanctum personal access). DB + app deployed. AUD-001 token rotation handled.
- **2026-05-04 (D22):** Document Ops re-spec — 4 new products + 5 prices + 3 Payment Links via Stripe API (idempotent script). Operations Monthly Starter retired (grandfather only). New tier ladder: Auto €99/mo, Auto Plus €249/mo, Rescue+Export €499/mo, Bespoke €1500/€2500/mo. /docs/ wiring + `after_completion` copy still pending.
- **2026-05-02 (M1):** Brand-domain consolidation — added `portal.callmeie.ie` as primary URL via GoDaddy DNS API + Coolify multi-FQDN PATCH. Both URLs live, Traefik routing both. 30-day overlap until ~2026-06-02 then strip `portal` from `owlzone.trade` Porkbun zone.
- **2026-05-04 (v0.4.1 ship):** Document AI extraction pipeline — Stages 1+5+7 shipped (pypdfium2/pdfplumber ingest + provenance, instructor+Pydantic+Ollama JSON-mode, HaluGate cross-field hard-bounce). Stage 4b line-item tables via rapid-table (Windows-friendly PaddleOCR-PP-Structure ONNX bridge — paddlepaddle 3.3 PIR onednn bug confirmed real on Windows). Stage 5a skip-LLM-on-native (saves ~78s on Vega 8 native PDFs). Stage 8 Label Studio CE infra scaffolded (alembic 0002_corrections, infra/label-studio/, /api/corrections webhook route, corrections_consumer.py). Stage 9 DVC + Hetzner Object Storage scaffolded (.dvc/config, dvc.yaml, build_train_shard.py, dvc_push.sh). All Adam-keyboard remaining: see §14.4 + §14.5.

### 14.3 Phase 2 backlog (Document Ops Portal)

- Document upload UI (drag-drop multi-file)
- OCR pipeline trigger (existing `proof-fixtures/scripts/extract.py` v0.4.1 — drop-in replacement for `document-ops/scripts/ocr_file_to_artifact.py`)
- Review queue UI (row-by-row approve/correct/reject) — superseded by Label Studio CE per §14.4
- Clean export download (CSV per tenant)
- Run-report download (Markdown)
- Stripe Customer Portal embed

### 14.4 Label Studio CE (review interface — LIVE 2026-05-04)

Per `document-ops-portal/CALLMEIE-DOCAI-V0.4-PRD.md` D-V0.4-06. Apache-2.0 review surface for the corrections flywheel. NOT a sub-processor under DPA v0.4 §7 — Customer-side review interface running on Processor infrastructure (Hetzner DE).

| Field | Value |
|---|---|
| Image | `heartexlabs/label-studio:latest` (Apache-2.0; pinned 1.23.0 at first deploy) |
| Public URL | `https://review.callmeie.ie` (LIVE; HTTP 200; LE cert wired via Traefik) |
| DNS | A record `review` → 178.104.205.255 (Cloudflare zone callmeie.ie, DNS-only, TTL 300) — added 2026-05-04 |
| Coolify service uuid | `npfgaznhn2n79kow1r5euzdi` (project: my-first-project, server: localhost) |
| Internal Postgres | container `labelstudio-pg-{uuid}`, volume `npfgaznhn2n79kow1r5euzdi_pg-data` (distinct from portal Postgres `m19o67kvb3d2ugvobm9zs9z4`) |
| Network membership | `coolify` (Traefik reachability) + `npfgaznhn2n79kow1r5euzdi` (postgres reachability) |
| Webhook target | `https://portal.callmeie.ie/api/corrections` |
| Webhook secret | `LS_WEBHOOK_SECRET` in `~/.claude/routes/.env` |
| Admin auth | `LS_ADMIN_EMAIL` + `LS_ADMIN_PASSWORD` in vault. SMTP NOT configured so /forgot-password disabled — reset path: `docker exec coolify sh -c 'php artisan root:reset-password'` via SSH owl_deploy_ed25519. |
| Compose source | `/data/coolify/services/npfgaznhn2n79kow1r5euzdi/docker-compose.yml` (rewritten 2026-05-04 to match portal Traefik HTTPS pattern; Coolify-template was incompatible with custom FQDN — manual edits only going forward) |
| Task config | `document-ops-portal/infra/label-studio/label-config.xml` (vendor / total / vat / date / line_items / reason) — pending paste into Label Studio project UI |

**Adam-keyboard remaining:**
1. Login `https://review.callmeie.ie` with `LS_ADMIN_EMAIL` + `LS_ADMIN_PASSWORD` from vault. Confirm Django admin loads.
2. Create Label Studio project; paste `infra/label-studio/label-config.xml` into Labeling Setup; save.
3. Settings → Webhooks → Add webhook target `https://portal.callmeie.ie/api/corrections` with header `X-LS-Webhook-Secret: <LS_WEBHOOK_SECRET>` triggered on Annotation Created/Updated.

**SSH key for AX52** — `~/.ssh/owl_deploy_ed25519` (referenced in vault as `OWL_DEPLOY_KEY`). Password auth FAILS (root password rotated and/or fail2ban-banned). Always prefer key auth via paramiko `Ed25519Key.from_private_key_file()`.

**Compose-rewrite root cause (Coolify v4 known workaround)** — Coolify generates Traefik labels at service-create time using `SERVICE_FQDN_*` envs. Setting them AFTER create does NOT regenerate compose. Coolify dashboard "Save" calls `regenerate_compose_file()` but that path has no public API. Workaround: manual compose edit + `docker compose down/up -d`. New entry alongside #6281 cascade bug.

**Done by Claude 2026-05-04:**
- ✓ Label Studio service created via Coolify API (type=labelstudio template, uuid `npfgaznhn2n79kow1r5euzdi`)
- ✓ Env vars patched (LS_HOST, admin user/password, webhook secret)
- ✓ Compose rewritten with portal-pattern Traefik HTTPS labels + LE cert + dual-network membership
- ✓ Postgres rename `postgres` → `labelstudio-pg-{uuid}` (avoids DNS collision with portal Postgres alias on `coolify` network)
- ✓ alembic 0002_corrections applied to portal Postgres (`extractions` + `corrections` tables + `corrections_notify_trigger` confirmed via `pg_trigger`)

### 14.5 DVC + Hetzner Object Storage corpus (training-data store)

Per `document-ops-portal/CALLMEIE-DOCAI-V0.4-PRD.md` D-V0.4-07. Replaces HuggingFace private datasets (HF stores AWS US — Schrems II / SCCs Module 3 blocker that D26-PIVOT explicitly removed).

| Field | Value |
|---|---|
| DVC version | 3.67.1 (Apache-2.0) |
| Remote name | `hetzner` |
| Bucket | `s3://callmeie-corpus` (CREATED 2026-05-04 in project 14229666; eu-central / Nuremberg) |
| Endpoint | `https://nbg1.your-objectstorage.com` (Nuremberg — DPA v0.4 §8.1 residency. Earlier scaffold pointed at fsn1; corrected at bucket-create time) |
| Access keys | `HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID` + `HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY` in vault (also written to `proof-fixtures/.dvc/config.local`, gitignored). Description tag: `callmeie-corpus-dvc-2026-05-04` |
| Live test | `dvc push` 2 files OK (test artifact then `dvc gc -c -w` cleaned remote) |
| Hetzner Cloud Console UI quirk | Servers/Buckets list filter shows 0 entries even when project has them — direct URL path works (`/servers/127171852` for AX52, `/buckets` for callmeie-corpus). Documented for future debug. |
| Sub-processor status | Hetzner already DPA v0.4 §7.1 sub-processor for compute; Object Storage same vendor — no new entry needed |
| Pipeline | `proof-fixtures/dvc.yaml` (stages: `shard_train` + `bench_holdout`) |
| Push helper | `proof-fixtures/scripts/dvc_push.sh` (cron-friendly idempotent) |

**Tracked artifacts** (NOT in git — DVC pointer only):
- `corpus/corrections.jsonl` — append-only correction stream from Label Studio (consumer drains)
- `corpus/extractions.jsonl` — gate-pass extraction record (v0.4.2: `extract.py --emit-corpus`)
- `corpus/train_shard.jsonl` — `shard_train` stage output (deduped, latest-correction-wins, joined with extraction context)
- `corpus/holdout/` — D-V0.4-10 frozen v0 100-doc holdout (Adam-curated, pending)
- `corpus/metrics.json` — per-field + per-tenant correction counts feeding PRD §8 Open Question 2 (~100 corrections/customer/month thesis verification)

**Done by Claude 2026-05-04:**
- ✓ Bucket `callmeie-corpus` created via Hetzner Cloud Console (Claude in Chrome MCP), Nuremberg, project 14229666
- ✓ S3 credentials generated + saved to vault + `proof-fixtures/.dvc/config.local`
- ✓ DVC config endpoint corrected: fsn1 → nbg1
- ✓ Live `dvc push` verified end-to-end (test file pushed + `dvc gc -c -w` cleanup confirmed bucket reachable + credentials valid)
- ✓ AX52 server identity confirmed via Hetzner invoice: server-id `127171852`, primary-ip `126966475`, project `Default` 14229666 — same account as currently logged-in scruge@pm.me / K0405312326

**Done by Claude 2026-05-04 (continuation):**
- ✓ Label Studio API token (JWT refresh) generated + saved to vault as `LS_REFRESH_TOKEN` (user_id=1, exp +256y)
- ✓ Label Studio project id=1 "Document Ops IE invoices" created via API with `infra/label-studio/label-config.xml` pasted (vendor / total / vat / date / line_items / reason)
- ✓ Label Studio webhook id=1 created via API: target `https://portal.callmeie.ie/api/corrections`, header `X-LS-Webhook-Secret: <LS_WEBHOOK_SECRET>`, triggered on ANNOTATION_CREATED + ANNOTATION_UPDATED
- ✓ `corrections-consumer` Docker container deployed on AX52 (coolify network, restart=unless-stopped, env DATABASE_URL = portal Postgres DSN, CORPUS_PATH = /app/corpus/corrections.jsonl). LISTEN/NOTIFY active.
- ✓ `docops-rescue-daemon` Docker container deployed on AX52 2026-05-07 (coolify network, restart=unless-stopped, image `python:3.13-slim`, mounts `/opt/callmeie/proof-fixtures` + `/opt/callmeie/document-ops-portal`, env `DATABASE_URL` (postgresql+psycopg://) + `LABEL_STUDIO_URL=https://review.callmeie.ie` + `LS_REFRESH_TOKEN` + `SESSION_SIGNING_KEY` + `LS_WEBHOOK_SECRET` + SMTP/Resend trio). Cmd: `pip install psycopg[binary] requests sqlmodel sqlalchemy pydantic pydantic-settings python-dotenv itsdangerous stripe email-validator && python /app/proof-fixtures/scripts/push_rescues_to_label_studio.py --watch`. Polls every 60s, pushes `gate_passed=FALSE` extractions for tenants with `plan='rescue_export'` to their `docops-{slug}` Label Studio project. Idempotency via `AuditLog.action='rescue_pushed_to_label_studio'`.
- ✓ Alembic migration `0003_security_hardening` applied 2026-05-07: `stripe_events` idempotency table (UNIQUE on event_id) + `tenants.suppressed_at` timestamp (backfilled now() for already-suppressed rows). Live `alembic current` reports `0003_security (head)`.
- ✓ `document-ops-portal/{app,alembic,alembic.ini}` rsynced (tar+scp) to `/opt/callmeie/document-ops-portal` on AX52 (private repo, GitHub PAT clone refused; rsync is the deploy mechanism for now).
- ✓ Cron entry on AX52 root: `0 9 * * * /usr/local/bin/docops-sla-check.sh >> /var/log/docops/sla-check.log 2>&1`. Wrapper script holds the env vars for the SLA-check Docker run; emails `adam@callmeie.ie` if any rescue >3 business days old. Silent on green days.
- ✓ Vault additions 2026-05-07: `LABEL_STUDIO_URL=https://review.callmeie.ie` + `LABEL_STUDIO_TOKEN=ac2e65c5...` (legacy 40-hex; legacy auth returns 401 on this LS deploy — prefer `LS_REFRESH_TOKEN` Bearer JWT flow per `proof-fixtures/scripts/push_rescues_to_label_studio.py:_label_studio_token`).
- ✓ DVC 3.67.1 installed in `/opt/callmeie/dvc-venv` on AX52 host (apt python3-venv installed alongside). Live `dvc push` from host: 1 file pushed + `dvc gc -c -w` cleanup confirmed remote reachable.
- ✓ Crontab line installed on AX52 root: nightly 02:30 UTC `dvc_push.sh` execution. Logs to `/var/log/dvc-push.log`.
- ✓ `HCLOUD_TOKEN` generated via Hetzner Cloud Console (Read & Write scope, name `claude-automation-2026-05-04`). Saved to vault. Live API verified — returns server 127171852 in nbg1.

**Coolify scheduled-task deviation (documented):** User requested Coolify scheduled task for dvc_push (#3). Coolify scheduled tasks require a Coolify-managed Application/Service container with `docker exec` semantics. The corrections-consumer is a raw `docker run` (not Coolify-managed) and portal app's container has no dvc binary. Used host crontab on AX52 root instead — same lifecycle outcome (nightly 02:30 UTC), Coolify-native scheduled task deferred to v0.4.2 architectural cleanup (would require corrections-consumer + dvc-pusher rebuilt as Coolify Application via dockercompose build pack with custom Dockerfile baking dvc).

**Adam-keyboard remaining: NONE for v0.4.1 corrections flywheel** — all four user-requested items (Label Studio config, consumer daemon, scheduled push, HCLOUD_TOKEN) shipped. First correction submitted via Label Studio UI will end-to-end verify webhook → INSERT → NOTIFY → consumer → JSONL → cron → bucket.

## 15 · docs.callmeie.ie (Document Ops sales site — GitHub Pages)

> **SUPERSEDED 2026-05-03:** subdomain `docs.callmeie.ie` retired. Sales surface folded into the path-based monorepo at `https://callmeie.ie/docs/` (scruge1/callmeie-hub). Cloudflare CNAME deleted, GH Pages custom-domain config cleared on `scruge1/docs-callmeie` (now serves only at `https://scruge1.github.io/docs-callmeie/`). Reason: LE cert never provisioned (`https_enforced: false`) — broken HTTPS state during transition window flagged by Codex peer review as P0 blocker. Faster to drop than fix-then-drop. Old GSC URL-prefix property submission should be removed by Adam at Search Console. Section kept for historical reference only.

| Field | Value |
|---|---|
| Repo | https://github.com/scruge1/docs-callmeie (public) |
| Local repo | C:\Users\a33_s\Desktop\claude MCPs\New repos\docs-callmeie |
| Public URL | ~~https://docs.callmeie.ie~~ → DROPPED 2026-05-03 (was: built 2026-05-02, cert never provisioned) |
| Hosting | GitHub Pages, `main` branch, `/` root, custom CNAME |
| DNS | callmeie.ie GoDaddy zone — CNAME `docs` → `scruge1.github.io` TTL 600 (created 2026-05-02 via GoDaddy API) |
| Cert | Let's Encrypt (auto-issued by GitHub Pages once DNS verified, ~15-60 min) |

### 15.1 Build / deploy

No build step. Push to `main`, GitHub Pages rebuilds in ~30s.

```
docs-callmeie/
├── CNAME              docs.callmeie.ie
├── index.html         single-page sales site (Klippa wedge)
├── site.css           Owl Studio brand tokens (Fraunces + Inter + paper)
├── README.md
└── .gitignore
```

### 15.2 Source of truth for copy

- Wedge: `New repos/Next Revenue Ideas/document-ops/POSITIONING.md` (canonical)
- Pricing: `New repos/Next Revenue Ideas/document-ops/PRICING.md` (research-anchored)
- If copy changes, update those FIRST, then mirror to `index.html`.

### 15.3 Stripe Payment Links wired

**Canonical pricing = `PRICING-SSOT.md` (this dir).** Reconciled 2026-05-15 to match the live `callmeie-hub` site. Stripe is **live mode**.

**Websites (one-off, full all-in — D1 reconciliation 2026-05-15):**

| CTA | Price | Link |
|---|---|---|
| Start a Starter | €695 `price_1TXOiTCEqG2AuI1zZzAGb44I` | `https://buy.stripe.com/cNi14mbCH2mq93ube5aIM0k` |
| Start a Pro | €1,595 `price_1TXOiTCEqG2AuI1zlW1NC8ZK` | `https://buy.stripe.com/5kQ28q6in8KO7ZqgypaIM0l` |
| Book €99 scoping audit (Custom entry) | €99 | `https://buy.stripe.com/bJe6oGbCH7GK0wY4PHaIM00` |

> ⚠️ OLD 50%-deposit links `cNicN40Y31imenO2HzaIM07` (€348) + `bJe28qeOTf9c5RidmdaIM08` (€798) are **still active pending site redeploy** — deactivate them ONCE callmeie.ie is redeployed with the new links above (do not deactivate before deploy or live buy buttons break).

**Receptionist (monthly + setup — D3, created 2026-05-15):**

| CTA | Price | Link |
|---|---|---|
| Starter €149/mo | `price_1TXOiVCEqG2AuI1zGWDwhEJG` (prod `prod_UWRbvcwglnsgON`) | `https://buy.stripe.com/3cI7sK36b4uy6Vm81TaIM0m` |
| Professional €249/mo | `price_1TVxwgCEqG2AuI1zPZzlP7q3` | `https://buy.stripe.com/dRmaEW5ej8KO2F6dmdaIM0n` |
| Growth €397/mo | `price_1TVxwgCEqG2AuI1zzsCH9YG1` | `https://buy.stripe.com/eVqbJ0dKP7GKcfGci9aIM0o` |
| Setup €297 (Starter/Pro) | `price_1TVxwgCEqG2AuI1zkxlRvISY` | `https://buy.stripe.com/14A9AS6in6CGenO0zraIM0j` |
| Setup €497 (Growth) | `price_1TXOiVCEqG2AuI1zF7raG6Gr` | `https://buy.stripe.com/00waEWbCH5yCgvW6XPaIM0p` |

**Document Ops (canonical — links already correctly priced):**

| CTA | Price | Link |
|---|---|---|
| Entry Pilot | €500 one-off | `https://buy.stripe.com/dRm00i0Y3gdgbbCgypaIM09` |
| Standard Pilot | €1,500 one-off | `https://buy.stripe.com/14A3cueOTbX04NeeqhaIM0a` |
| Auto | €99/mo | `https://buy.stripe.com/14A9AS9uzgdggvW95XaIM0f` |
| Auto Plus | €249/mo | `https://buy.stripe.com/eVq6oG8qvaSW0wYgypaIM0g` |
| Rescue + Export | €499/mo | `https://buy.stripe.com/4gM14m2273qufrS3LDaIM0h` |
| Bespoke | from €1,500/mo | `mailto:` (no link by design) |
| Operations Monthly Starter €250/mo | *(grandfathered — keep, unadvertised)* | `https://buy.stripe.com/14AfZg4afaSW5Rici9aIM0b` |

> Duplicate "Document Ops — Self-serve" links (`…IM0c/d/e`) DEACTIVATED 2026-05-15. Obsolete 4-tier ladder products (`prod_UQowfvaBTt487e/GVxn6bpMeg/BGv7jwRdpb/zW8jkmW9kX`, CallMeIE Starter/Growth/Pro/Concierge €99/299/699/1500) ARCHIVED 2026-05-15. Owl Studio Essential/Growth/Concierge care plans left untouched (legacy — confirm separately).

Same Stripe webhook handler at `https://portal.callmeie.ie/webhooks/stripe` covers checkouts.

### 15.4 Cert recovery (if stalls > 24h)

```bash
# Drop CNAME via gh API to re-kick LE
gh api /repos/scruge1/docs-callmeie/pages -X PUT --input - <<<'{"cname": null}'
sleep 30
gh api /repos/scruge1/docs-callmeie/pages -X PUT --input - <<<'{"cname": "docs.callmeie.ie"}'
# Once approved, enforce
gh api /repos/scruge1/docs-callmeie/pages -X PUT --input - <<<'{"https_enforced": true}'
```

## 16 · callmeie.ie parent-brand hub (GitHub Pages — LIVE)

| Field | Value |
|---|---|
| Repo | https://github.com/scruge1/callmeie-hub (public) |
| Local repo | C:\Users\a33_s\Desktop\claude MCPs\New repos\callmeie-hub |
| Public URL | https://callmeie.ie (LIVE 2026-05-03 — M2 cutover) |
| Hosting | GitHub Pages, `main` branch, `/` root, custom CNAME |
| DNS provider | Cloudflare (NS swap from GoDaddy 2026-05-03) |
| Cert | Let's Encrypt, expires 2026-07-02 (auto-renewed by GH Pages; reused from prior CallMeIE Pages owner — no re-issuance lag) |
| Build | 22 files at v1 ship — see `CALLMEIE-PARENT-HUB-CODEBASE-PLAN.md` §1 |

### 16.1 Companion product surfaces (4-product family)

| Surface | Domain / path | Hosting | Repo |
|---|---|---|---|
| Parent hub | callmeie.ie/ | GH Pages | scruge1/callmeie-hub |
| AI Receptionist | callmeie.ie/receptionist/ (was receptionist.callmeie.ie, retired 2026-05-03) | GH Pages | scruge1/callmeie-hub (folded from scruge1/CallMeIE) |
| Document Ops sales | callmeie.ie/docs/ (was docs.callmeie.ie, retired 2026-05-03) | GH Pages | scruge1/callmeie-hub (folded from scruge1/docs-callmeie) |
| Document Ops portal | portal.callmeie.ie | Coolify Hetzner | scruge1/document-ops-portal (private) |
| AI-First Websites | callmeie.ie/websites/ (full gallery + 7 demos + 12 industry samples self-contained, post-2026-05-03 brand merge) | GH Pages | scruge1/callmeie-hub (folded from scruge1/owl-studio-website-directions) |

All five surfaces share the same brand contract — paper #f8f5f0 + ink #1c1f24 + indigo #1d3557 + amber #c08a3f + Fraunces + Inter + JetBrains Mono. Tokens lifted verbatim from `document-ops-portal/app/static/portal.css` lines 1–58.

### 16.2 Cloudflare configuration (callmeie.ie zone)

| Field | Value |
|---|---|
| Zone ID | `0ed441de9cda4746aa4bbc3c46532c81` |
| Account | `Scruge@pm.me's Account` (account ID `7cc2ab3455c4547401123e9c97baf077`) |
| Plan | Free |
| API token (zone-scoped) | `CLOUDFLARE_ZONE_CALLMEIE_TOKEN` in `~/.claude/routes/.env` (DNS:Edit + Zone Settings:Edit + Bulk URL Redirects:Edit). Distinct from `CloudFlare_API` which is the AI Gateway token. |
| Nameservers | `arely.ns.cloudflare.com`, `bjorn.ns.cloudflare.com` (NS swap at GoDaddy registrar) |
| SSL mode | Full (strict) |
| Always Use HTTPS | on |
| Proxy state | DNS-only on all GH Pages + Coolify origin records (proxying breaks origin SSL); proxy-on for any future records that don't host their own cert |

### 16.3 DNS records on Cloudflare

```
A     callmeie.ie  185.199.108-111.153   (GH Pages anycast — DNS-only)
A     portal       178.104.205.255         (Coolify Hetzner — DNS-only)
CAA   callmeie.ie  letsencrypt.org         (cert issuance restriction)
CNAME docs         scruge1.github.io       (GH Pages — DNS-only)
CNAME www          scruge1.github.io       (GH Pages www variant — DNS-only)
CNAME receptionist scruge1.github.io       (GH Pages — DNS-only) [added 2026-05-03]
CNAME _domainconnect _domainconnect.gd.domaincontrol.com  (orphan from GoDaddy era; safe to delete)
TXT   _dmarc       v=DMARC1; p=quarantine; ...
TXT   callmeie.ie  google-site-verification=YdiX8OOpq1...
```

### 16.4 Cutover history

- **2026-05-02:** PRDs drafted, design recipe + research-deep + codebase plan + BUILD-SPEC produced via 5 parallel Opus agents.
- **2026-05-03 morning:** Adam confirmed §5 decisions (8 questions, mostly defaults). Cloudflare zone added by Adam via dashboard (Claude in Chrome drove). Token minted, NS swapped, ~30min .ie TLD propagation. Phase 1 build (22 files) shipped to scruge1/callmeie-hub. Phase 2 impeccable audit applied (Pass 1 CRITICAL + 2 HIGH + Pass 3 selected MEDIUM).
- **2026-05-03 cutover window:** scruge1/CallMeIE pushed direct to main with CNAME swap (callmeie.ie → receptionist.callmeie.ie) + 50+ URL rewrites. Cloudflare CNAME `receptionist` added. Pages enabled on scruge1/callmeie-hub. callmeie.ie cert REUSED from previous Pages owner (zero re-issuance time). Both surfaces LIVE within ~5 minutes of cutover trigger.

### 16.5 Outstanding follow-ups

- **Cloudflare Bulk Redirects** — 9 legacy URLs redirect via meta-refresh stubs in callmeie-hub for now (Google honors as redirect, treats as 302). For 301 SEO transfer, configure Bulk Redirects via dashboard (token scope needs Account Rules Lists:Edit which current `CLOUDFLARE_ZONE_CALLMEIE_TOKEN` lacks). After Bulk Redirects active, delete the 9 meta-refresh stubs from callmeie-hub.
- ~~**GSC Change-of-Address**~~ — OBSOLETE. Subdomains retired 2026-05-03; path-based callmeie.ie/* handled by domain-property GSC entry. Old `receptionist.callmeie.ie` URL-prefix property should be DELETED by Adam at Search Console (data-only, no behavior change).
- ~~**receptionist.callmeie.ie HTTPS**~~ — RESOLVED via subdomain drop 2026-05-03 (cert never provisioned; faster to drop than fix-then-drop).
- **Lighthouse + Core Web Vitals scan** at 375 / 768 / 1180 / 1440 — run via gstack browse Playwright. Targets per BUILD-SPEC: LCP ≤1.2s, CLS <0.05, TBT <50ms.
- **Adam visual review on real device** — touch gate.
- **websites.callmeie.ie migration (H4)** — defer until web-design product is ready for Callmeie-branded shipping.

### 16.6 Email Routing (Cloudflare → Proton, 2026-05-03)

| Surface | Address shown | Routes to |
|---|---|---|
| Parent hub mailto CTAs (15 refs in `index.html` + `about.html`) | `hello@callmeie.ie` | Cloudflare Email Routing → `callmeie@proton.me` |
| Document Ops portal templates (`base.html`, `login.html`, `magic_link_sent.html`) | `hello@callmeie.ie` | same routing |
| Receptionist niche pages (35+ refs, GDPR/contact/footer) | `callmeie@proton.me` (direct, no CF hop) | Proton inbox |
| Receptionist privacy/terms (4 refs) | `hello@callmeie.ie` | same routing |
| Owl Studio sales (`/websites/index.html`, 14 mailto CTAs) | `callmeie@proton.me` (direct) | Proton inbox |

**DNS records added at Cloudflare zone callmeie.ie (2026-05-03):**
- `MX` route1.mx.cloudflare.net (priority 81)
- `MX` route2.mx.cloudflare.net (priority 12)
- `MX` route3.mx.cloudflare.net (priority 23)
- `TXT` callmeie.ie — `v=spf1 include:_spf.mx.cloudflare.net ~all`
- `TXT` cf2024-1._domainkey.callmeie.ie — DKIM RSA-SHA256 public key

**Routing rule:** custom address `hello@callmeie.ie` → action `Send to email` → destination `callmeie@proton.me` (verified 2026-05-03). Status: Active.

**Proton plan limitation:** Free plan — incoming forward only. Adam REPLIES from `callmeie@proton.me`, not `hello@callmeie.ie`. For reply-from-brand, upgrade to Proton Mail Plus + add callmeie.ie as Proton custom domain. Acceptable for current revenue stage.

**Token scope used:** `CLOUDFLARE_ZONE_CALLMEIE_TOKEN` (Zone DNS:Edit + Zone Settings:Edit) handled DNS records + zone-level routing enable. Account-scope endpoints (Account:Email Routing Addresses + Zone:Email Routing Rules) needed dashboard-driven setup — Claude in Chrome automated.

**Verification ritual:** Cloudflare sends one-click link to destination address; recipient must click within ~24h for routing rule save to succeed. Re-trying save before verify yields "Verification email has been sent too recently" banner.

**Recovery recipe (if mail stops landing):** check zone status `GET /client/v4/zones/<zone>/email/routing` returns `enabled=true status=ready`; verify destination not deleted from `/accounts/<acct>/email/routing/addresses`; verify routing rule still active in `/zones/<zone>/email/routing/rules`. If MX records missing, re-create from §16.6 list.