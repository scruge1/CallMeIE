# WhatsApp Cloud API setup runbook (P2-4 channel — 2026-05-10)

**Goal:** wire WhatsApp inbound → unified_leads via Meta Cloud API webhook.

**State today:** webhook endpoint LIVE on `https://api.callmeie.ie/webhooks/whatsapp` (hot-patched into running container, signature-verified, hub.challenge handshake confirmed working). Verify token already in Coolify env. Adam-keyboard remaining = Meta Developers console steps.

**Pre-filled values to paste into Meta console:**

| Field | Value |
|---|---|
| Webhook URL | `https://api.callmeie.ie/webhooks/whatsapp` |
| Verify token | `T5NemBrkjacYLrf7i0kpXDDnNKSrFq89E4IrBd5SDPU` |
| Subscribe to | `messages` (only) |

---

## Step 1 — Log in to Meta for Developers (~1 min)

1. Browser: https://developers.facebook.com
2. Top-right "Log in" → use your Facebook account (the one tied to your Meta Business Manager — same account you use for the GBP work)
3. After login: top-right "Get Started" or go directly to https://developers.facebook.com/apps

## Step 2 — Create or pick the CallMeIE app (~3 min)

If you already have a Meta app for CallMeIE → skip to Step 3.

Else:
1. https://developers.facebook.com/apps → **Create app**
2. Use case: **Other** (so it presents the full product list)
3. App type: **Business** (REQUIRED — WhatsApp product only attaches to Business apps)
4. Name: `CallMeIE`
5. Contact email: `hello@callmeie.ie`
6. Business Account: select your existing Meta Business Manager (the GBP one)
7. **Create app** → click through any "App secret" / 2FA prompts

## Step 3 — Add WhatsApp product (~2 min)

In the app dashboard:
1. Left sidebar **Add a Product** → find **WhatsApp** → click **Set up**
2. Pick the same Business Account when prompted
3. Meta gives you a **test phone number** automatically (something like `+1 555 ...`); the dashboard shows `Phone number ID` + temporary `Access token`
4. Note both — they're displayed on the WhatsApp → **API Setup** page:
   - **Phone number ID** (long numeric)
   - **Temporary access token** (24h validity; permanent System User token comes later in Step 6)

## Step 4 — Configure webhook (~3 min — THE LOAD-BEARING STEP)

In the app dashboard, left sidebar:

1. Click **WhatsApp → Configuration** (or **Webhooks** depending on UI version)
2. Under **Webhook**, click **Edit** or **Add Callback URL**
3. Paste:
   - **Callback URL:** `https://api.callmeie.ie/webhooks/whatsapp`
   - **Verify token:** `T5NemBrkjacYLrf7i0kpXDDnNKSrFq89E4IrBd5SDPU`
4. Click **Verify and save**

If Meta returns "The URL couldn't be validated" → ping me; the endpoint is verified working from a curl test, so it's likely a CORS or content-type quirk we can patch in 2 minutes.

5. Once verified, on the same page under **Webhook fields**, click **Manage** next to the WhatsApp Business Account row:
   - Tick **messages** (the only one we care about for inbound)
   - Save

## Step 5 — Find App Secret + give it to me (~30s)

In the app dashboard top-left:
1. **App settings → Basic**
2. Scroll to **App Secret** → click **Show** → copy the value
3. Paste it back to me. I'll add it to Coolify env as `WHATSAPP_APP_SECRET` so the webhook can verify Meta's HMAC signatures (without this, the endpoint rejects all real inbound as `bad_signature`).

## Step 6 — System User permanent token (~3 min, Phase 2b for replies)

Defer this until you actually need outbound replies (sending WA messages from the Coolify backend back to customers). Inbound-only flow (this PRD) only needs WHATSAPP_APP_SECRET.

When you're ready for outbound:
1. https://business.facebook.com → **Business settings → System Users**
2. Add System User → name `CallMeIE Backend` → role **Admin**
3. Generate token → scopes: `whatsapp_business_messaging`, `whatsapp_business_management`
4. Save the token; paste back to me as `WHATSAPP_ACCESS_TOKEN` (Coolify env)
5. Save the WhatsApp Business Account ID (different from Phone Number ID); paste as `WHATSAPP_BUSINESS_ACCOUNT_ID`

## Step 7 — Test inbound (after Steps 4 + 5) (~2 min)

1. From your personal WhatsApp on phone, message the test number Meta gave in Step 3
2. Send: `test hello from adam`
3. Within ~5 seconds:
   - Telegram bot should ping with the message (priority>=2 channel — WA gets priority because it's a real human typing)
   - Admin Leads tab at `https://api.callmeie.ie/admin` (when image rebuilds and admin.html ships) should show a new row with channel=`whatsapp`, contact_phone=your number, text=`test hello from adam`
   - psql check: `SELECT * FROM unified_leads WHERE primary_channel='whatsapp' ORDER BY created_at DESC LIMIT 1;`

If nothing arrives → check Meta dashboard **WhatsApp → Configuration → Webhook → Recent Deliveries** — Meta logs every POST + the response code we returned.

## Step 8 — Move from test number to your real Irish number (Phase 2c, post-Twilio +353 lands)

Test number is sandbox-only — it can only message 5 pre-registered numbers (yours, plus 4 you add manually). For production, you need:

1. **Business Verification** in Meta Business Manager (~1-3 days; needs IE company docs)
2. **Phone number registration** — your +353 once Twilio releases it
3. Display name approval (~24h after BV)

Defer until BV is in motion. Test number is enough to validate the inbound pipeline end-to-end.

---

## What's already done on my side

- Code: `GET /webhooks/whatsapp` (verify) + `POST /webhooks/whatsapp` (inbound) live in `server.py`. HMAC SHA-256 sig verify implemented; bad signature → 403. Inbound parser handles text / button / interactive payloads.
- Wired to LeadIngestor → unified_leads (`channel='whatsapp'`, priority=0 — bumped to 2 if we add it to `_PRIORITY_CHANNELS` later).
- Coolify env: `WHATSAPP_VERIFY_TOKEN` set + container restarted + tested.
- Live curl confirms: bad token → 403, good token → 200 + echoes challenge.

## What I still need from you

- **WHATSAPP_APP_SECRET** value (Step 5 above). Without it the webhook accepts the verify GET but rejects all real Meta POSTs as `bad_signature`. Critical.
- Step 6 + 7 deferred until first outbound reply needed.

## Rollback

If anything goes sideways:
1. Webhook URL in Meta dashboard → click **Delete**. Stops Meta sending.
2. Remove `WHATSAPP_VERIFY_TOKEN` + `WHATSAPP_APP_SECRET` from Coolify env. Endpoint then returns 403 on every request (verify_token mismatch, signature missing).
3. No data loss — unified_leads rows from WA channel can be cleaned with `DELETE FROM unified_leads WHERE primary_channel='whatsapp';` if needed.

---

**Stuck anywhere?** Quote the screen / error message and I'll patch the endpoint.
