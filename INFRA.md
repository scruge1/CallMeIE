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

**What lives here (see each section below):**
- Coolify control plane (port 8000)
- owltradezone (owlzone.trade)
- **Owl Studio services:** Vaultwarden, Uptime Kuma, Umami

---

## 2 · Coolify (self-hosted PaaS on the Hetzner box)

| Field | Value |
|---|---|
| URL | `http://178.104.205.255:8000` |
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

**Provisioner:** `C:/Users/a33_s/Desktop/callmeie-fix/scripts/provision-stripe.py` — idempotent, re-run safe.

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

| Site | Repo | URL | Notes |
|---|---|---|---|
| Owl Studio sales | `scruge1/owl-studio-website-directions` | `https://websites.owlzone.trade` | main → Pages, custom domain CNAME |
| CallMeIE | `scruge1/CallMeIE` | `https://callmeie.ie` | HTTPS pending IEDR DNS control |

**PAT:** `$GITHUB_TOKEN` in `~/.claude/routes/.env` (verify name — may be `GITHUB_PAT` or similar).

---

## 7 · Owl Studio registered sites (client fleet)

Seed data — each has its own admin token + dashboard.

| site_id | display_name | tier | care_tier | live_url | admin token env var |
|---|---|---|---|---|---|
| `owl-studio-sales` | Owl Studio · Sales | starter | — | https://websites.owlzone.trade | `OWL_ADMIN_TOKEN_OWL_STUDIO_SALES` |
| `rathborne-dental-demo` | Rathborne Dental (demo) | pro | growth | https://websites.owlzone.trade/samples/industries/01-dental-swiss.html | `OWL_ADMIN_TOKEN_RATHBORNE_DENTAL_DEMO` |

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

- [ ] **Cascade SERVICE_FQDN_* to app sub-resource** — manual Coolify dashboard step OR SSH DB update for vault / uptime / analytics domains to go live via HTTPS
- [ ] **Weekly/monthly PDF report cron** (Day 4) — leads + uptime + tickets per site
- [ ] **Full onboarding CLI** (Day 5) — `python onboard.py --site X …` chains site registration + UptimeRobot/Uptime-Kuma monitor creation + Stripe customer + Vaultwarden vault folder + admin-URL email
- [ ] **Wire the 10 industry sample contact forms** to `/owl/submit` — demonstrate "your leads would land here"
- [ ] **Migrate `/tmp/callmeie.db` → persistent Postgres** on Coolify box (Render wipes on redeploy)
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

**2026-04-21** — Stripe shipped (commit `21cbf8e` on CallMeIE). Vaultwarden / Uptime Kuma / Umami deployed on Coolify (service UUIDs in §2.2). Porkbun DNS for vault/uptime/analytics added. Custom-domain cascade pending (see §2.2 known issue).
