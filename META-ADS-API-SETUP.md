# Meta Marketing API setup — CallMeIE Ads (P-ADS Phase A)

**Goal:** wire Meta Ads API into callmeie-fix so Adam can run + monitor ads from his own admin (Phase B-C). Today = read-only foundation. NO money spent until Phase C.

**Hard spend gates (in code):**
- `META_AD_DAILY_BUDGET_MAX_USD=2.00` (default per-campaign daily cap)
- `META_AD_DAILY_BUDGET_HARDCAP_USD=5.00` (absolute ceiling — server.py refuses any campaign request above this regardless of UI input)
- All campaigns created via API land as `status=PAUSED` — going live is a separate explicit action
- Telegram alert per campaign-state-change

**State today (2026-05-10):**
- Meta app `CallMeIE` (id `727093413800358`) has Marketing API use case attached
- Business Portfolio "CallMeIE Technologies" exists
- Phase A code stubs ready in callmeie-fix (TBD next commit)
- Adam-keyboard remaining = create System User + Ad Account + payment method

---

## Step 1 — Open Meta Business Manager (~1 min)

URL: `https://business.facebook.com/settings/system-users?business_id=1299418328946367`

Click **Add** → name `CallMeIE Backend` → role `Admin` → **Create**.

## Step 2 — Generate System User access token (~3 min)

Click the new `CallMeIE Backend` system user → **Generate new token**.

- App: select `CallMeIE` (id 727093413800358)
- Token expiration: **Never** (long-lived; safer than user tokens that expire)
- Scopes (tick these):
  - `ads_management` (REQUIRED — read + write campaigns)
  - `ads_read` (REQUIRED — pull insights / spend)
  - `business_management` (read account info)
  - `whatsapp_business_messaging` (REQUIRED if you want this token to also handle WA outbound replies later)
  - `whatsapp_business_management` (manage WA assets)

**Copy the token** — Meta only shows it once. Paste it back to me, I'll save as `META_MARKETING_TOKEN` in vault + push to Coolify env.

## Step 3 — Add or confirm Ad Account (~5 min)

Business Settings → **Accounts → Ad accounts**.

If no ad account exists:
1. Click **Add → Create a new ad account**
2. Name: `CallMeIE Ads`
3. Time zone: **Europe/Dublin**
4. Currency: **EUR** (or **USD** — pick one, can't change later, your daily-spend caps are stored in this currency)
5. Payment method: add card OR PayPal
6. Pages assigned to this ad account: any FB Pages you have (skip if none)

If ad account exists, just confirm it's assigned to the `CallMeIE Backend` system user with **Manage campaigns** + **Manage performance** + **View performance** permissions.

**Copy Ad Account ID** (format `act_<numbers>`). Paste back to me as `META_AD_ACCOUNT_ID`.

## Step 4 — Verify token works

I'll run a smoke test from server-side once you paste the token + account_id:

```sh
curl -s "https://graph.facebook.com/v25.0/act_<id>?fields=name,currency,timezone_name,balance,amount_spent&access_token=<token>"
```

If it returns the account JSON, token is good. If 400/401, token is missing scopes — back to Step 2.

## Step 5 — Phase A code lands (after Step 4)

I'll ship in callmeie-fix:
- `scripts/meta_ads.py` — Graph API client w/ token + account_id from env
- Read-only endpoints:
  - `GET /admin/api/ads/account` — basic info (currency, balance, amount_spent)
  - `GET /admin/api/ads/campaigns` — list w/ status + objective + lifetime spend + last-7d insights
  - `GET /admin/api/ads/campaigns/{id}/insights?days=7` — daily breakdown
- Admin tab in `/admin` to view live ad performance

## Phase B (next session) — draft campaigns

- `POST /admin/api/ads/draft` — creates a campaign+adset+ad triple via API w/ `status=PAUSED` always. Cap-enforced: rejects any `daily_budget` > `META_AD_DAILY_BUDGET_HARDCAP_USD`
- Templates per service: receptionist / doc_ops / websites / audit (pre-filled targeting + copy + creative reference)
- Draft preview UI in admin tab

## Phase C (after first draft reviewed) — go-live

- `POST /admin/api/ads/{campaign_id}/activate` — flips PAUSED → ACTIVE. **Each call = real spend starts.** Telegram alert fires immediately.
- `POST /admin/api/ads/{campaign_id}/pause` — emergency stop, can fire from phone
- Daily-spend canary: cron job every hour reads `amount_spent` for all active campaigns; if any campaign > 80% of `META_AD_DAILY_BUDGET_MAX_USD`, auto-pause + Telegram

---

## What you control vs what I control

| Action | Who |
|---|---|
| Create System User + token + Ad Account + payment method | You (Adam-keyboard, Steps 1-3) |
| Build/maintain API client + read-only dashboards | Me (Phase A) |
| Design draft campaign templates per service | Me (Phase B), you approve copy/targeting |
| Write a draft campaign | Me (Phase B) — code creates PAUSED |
| Activate (= start spending) | You (Phase C) — explicit click in admin tab; I never auto-fire |
| Pause / kill | Either (emergency stop available from any side) |
| Increase daily-cap above €5 hardcap | You only (changes `META_AD_DAILY_BUDGET_HARDCAP_USD` in vault + container restart) |

---

## Spending boundary recap

```
META_AD_DAILY_BUDGET_MAX_USD=2.00     ← default per new campaign
META_AD_DAILY_BUDGET_HARDCAP_USD=5.00  ← absolute ceiling, server refuses above
```

These live in callmeie-fix Coolify env. Code never proposes a campaign above hardcap. UI input is ignored if above. Adam confirms each go-live.
