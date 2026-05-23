# Twilio Improvement Plan — 2026-05-23

Source: `TWILIO-AUDIT-2026-05-22.md` (REST API sweep across 18 endpoints) + supplementary regulatory-bundles + addresses + full geo-permissions query.

Account: `AC***REDACTED-SEE-VAULT***` ("My first Twilio account") · status=active · type=Full (paid) · balance **$9.535 USD**.

Numbers (3):
- `+35361788120` — Limerick demo line, wired to Vapi (`PNfa6047...`).
- `+35361788870` — pilot pool DID bought 2026-05-22, **NO webhooks** (`PN4ecfd7...`).
- `+16624397271` — US Mississippi, voice + SMS + MMS, **NO webhooks** (`PN53d94d...`).

Approved regulatory bundle: `BUe3a9d5fdaa25fa8065f7dc1607b5551a` Ireland Local-Business (`twilio-approved` 2026-04-03), with address `ADa6af451f3c25f1e5ff44f5b587c62fe7` (40 Gouldavoher Estate, Limerick).

Geo dialing permissions: 10 enabled (AU / BR / DE / FR / GB / IE / IL / IN / JP / US) / 209 blocked. Tight posture — good fraud defence.

---

## P0 — Do today (blocks live pilot work)

### P0-1. Top up balance — $9.535 → ≥$50

**Why:** 3 numbers @ ~€1/mo passive = ~$3/mo before traffic. One pilot's expected usage (~400 min Vapi-out via Twilio at ~$0.013/min = $5.20). At current balance you have ~3 days of headroom + can't sustain even one pilot through 30 days.

**How:** Console only (not API). Console → Billing → Add Funds. Recommend auto-recharge at $20 threshold → +$50.

**Cost:** $50.

### P0-2. Wire `+35361788870` webhooks to Vapi

**Why:** Just bought, sitting cold. Will not take inbound calls until VoiceURL set. Once first pilot is assigned this number, this becomes urgent.

**How:** Two paths:
- **Vapi console:** Phone Numbers → Import from Twilio → select +35361788870 → assign to the per-pilot Vapi assistant. Vapi sets all 3 webhooks automatically.
- **Or REST:**
  ```bash
  curl -X POST https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/PN4ecfd7074b2cb1641292956e8738ca23.json \
    -u $TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN \
    -d "VoiceUrl=https://api.vapi.ai/twilio/inbound_call" \
    -d "VoiceMethod=POST" \
    -d "StatusCallback=https://api.vapi.ai/twilio/status" \
    -d "StatusCallbackMethod=POST"
  ```

**Timing:** Defer until pilot #1 is assigned a Vapi assistant — wiring earlier = number rings to nothing.

---

## P1 — This week (blocks pilot conversion + SMS reliability)

### P1-1. Re-submit TrustHub Customer Profile + Trust Product

**Why:** Both `BU58d684c9...` (Customer Profile) and `BU1a8950fb...` (Trust Product) are `twilio-rejected`. These are DIFFERENT from the approved IE Local bundle — they unlock US A2P 10DLC SMS, branded calling, secondary CNAM, and full Trust Hub features. Until approved: US SMS deliverability is hit-and-miss and US→IE customer notifications can fail silently.

**How:** Console only (rejection reason not in API). Console → Trust Hub → Customer Profiles → click rejected entry → review rejection reason (usually missing doc, wrong business type, or address mismatch). Fix + resubmit.

**Likely cause:** Trust Hub Customer Profile was set up before the Ltd was incorporated (2026-05-21 per claude-mem). Re-submit with CallMeIE Technologies Ltd CRO 816273.

### P1-2. Register Messaging Service `MG5773dd...` for US A2P 10DLC

**Why:** Service "CallMeIE Ireland Outbound (Alpha + IE PN)" has `us_app_to_person_registered=False` and `use_case=undeclared`. Recent alerts log shows **36× error 21659** ("from number not enabled to send SMS") — meaning current SMS sends are failing because the routing doesn't have a valid A2P-registered sender.

**How:** Console → Messaging → Services → CallMeIE Ireland Outbound → Compliance → "Register for US A2P 10DLC". Requires the Customer Profile (P1-1) to be approved first.

**Timing:** Blocks on P1-1 approval. Can submit in parallel; activation gates on approval.

### P1-3. Fix outbound SMS "from" number in server.py

**Why:** Recent alerts show SMS sends FROM `+16617643212` failing with 21659 ("not SMS-enabled"). The actual SMS-capable US number is `+16624397271`. The TWILIO_FROM env var either has the wrong number or the demo line setter overrides it incorrectly somewhere.

**How:** Verify Render env: `TWILIO_FROM_NUMBER=+16624397271` (not 212). `grep "+16617643212" callmeie-fix/scripts/*.py` to find any hardcoded reference. Sample errors in audit at 2026-05-11 20:01:06 and adjacent timestamps.

**Cost:** Free — config + grep fix.

---

## P2 — This month (hygiene + security)

### P2-1. Create scoped API Keys per service

**Why:** API Keys = 0. Only AUTH_TOKEN is in use, which is the account-master credential. Revoking it = rotate every script + service that uses it (Vapi binding, server.py, buy-pilot-did.py, build-dunne-demo.py). Per-service API Keys = scoped revocation.

**How:** Console → Account → API Keys → Create. One key per service:
- `vapi-twilio-bind` — Vapi only
- `server-py-admin` — callmeie-fix server (server.py)
- `pilot-scripts` — buy-pilot-did.py, twilio-audit.py, build-dunne-demo.py
- `ops-readonly` — read-only audit access (this script could use this)

Then rotate consuming services one by one.

**Cost:** Free.

### P2-2. Rename account friendly_name

**Why:** "My first Twilio account" is the default from signup. Now the system is incorporated as CallMeIE Technologies Ltd CRO 816273.

**How:** Console → Account → General Settings → friendly name → "CallMeIE Technologies Ltd". Or REST:
```bash
curl -X POST https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID.json \
  -u $TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN \
  -d "FriendlyName=CallMeIE Technologies Ltd"
```

### P2-3. Delete stale address `AD165094d7a5a89d29fc6922489bf32d2a` ("e kyc platform")

**Why:** Old test/signup-era address, unused by any current bundle. The active bundle uses `ADa6af451f3c25f1e5ff44f5b587c62fe7`. Stale address = confusion + potential bundle-binding mistake.

**How:** Console → Phone Numbers → Regulatory Compliance → Addresses → delete. Or REST DELETE on the address SID.

### P2-4. Recordings retention review

**Why:** Audit shows 0 recordings on page 1 (could be paginated). PILOT-PROGRAM.md §"Compliance + GDPR" specifies 90-day retention. Twilio default is unlimited; storage costs $0.0025/min/mo per recording = cumulative.

**How:** Console → Voice → Recordings → list count. Cron `purge_old_data.py` already referenced in server.py for retention machinery; verify it covers Twilio recordings (likely does NOT — only local DB). Add `scripts/purge-twilio-recordings.py` that lists `Recordings.json?DateCreatedBefore=<90d ago>` → DELETE each.

---

## P3 — Scale considerations (revisit at 5+ paying customers)

### P3-1. Sub-accounts per customer

**Why:** B2B2C platform pattern. Twilio's official recommendation for "selling Twilio as part of your service": one sub-account per customer = per-customer billing isolation, per-customer auth credentials, per-customer usage attribution. Currently all 3 numbers + all calls live in main account, mixing demo + pilot + production traffic.

**How:** `POST /Accounts.json` with `FriendlyName=<customer slug>`. Each sub-account gets its own SID + AUTH_TOKEN. Cost is the same; numbers can be transferred via `POST /IncomingPhoneNumbers/PNxxx.json` with `AccountSid=<sub-account-sid>`.

**Cost:** Free (Twilio doesn't charge per sub-account).

**Defer reason:** Premature for pilot stage. Re-evaluate at 5+ paying customers OR when usage attribution / per-customer billing becomes operationally painful.

### P3-2. Twilio Verify for admin 2FA

**Why:** ADMIN_TOKEN currently single-factor. Add Twilio Verify for admin portal login → SMS/voice OTP delivered via existing US number.

**How:** Verify Service created via console, integrated via Twilio Verify REST API in server.py admin auth flow.

**Defer reason:** Single-operator system today (Adam). Add when team grows or when admin portal exposes more powerful actions (e.g. PATCH live Vapi assistants without per-action confirmation).

### P3-3. Backup TwiML App for Vapi-fallback

**Why:** All inbound voice routing goes via Vapi (`https://api.vapi.ai/twilio/inbound_call`). If Vapi has an outage (real risk per recent alert 11200/15003 with HTTP 502 from api.vapi.ai on 2026-05-21), calls fail silently with no human-audible fallback.

**How:** Create TwiML App with a minimal `<Response><Say voice="alice">We're temporarily unable to take your call. Please try again in a moment or text us at...</Say></Response>` flow. On per-number basis, set `VoiceFallbackUrl` to the TwiML App.

**Defer reason:** Adds complexity; Vapi uptime has been adequate. Build if Vapi 502s become a pattern.

---

## What's NOT broken (good as-is)

- Approved IE Local bundle `BUe3a9d5fdaa25fa8065f7dc1607b5551a` — kept, never touch
- Approved address `ADa6af451f3c25f1e5ff44f5b587c62fe7` — kept
- Geo dialing permissions (10 enabled / 209 blocked) — strong fraud posture
- Recent call data — 25 inbound calls last 14 days, mostly Adam dogfooding + a handful of real prospects (e.g. `+353871697876`, `+353892577153`)
- No sub-accounts → simpler audit perimeter
- No Verify / Notify / SIP Trunks / Studio / Serverless / Conversations → smaller attack surface

---

## Recommended sequence

1. **Adam-keyboard tonight:** P0-1 top-up (5 min, console).
2. **Adam-keyboard tonight:** P1-3 verify TWILIO_FROM env var (1 min, Render dashboard).
3. **Claude tomorrow:** P0-2 wire +35361788870 webhooks (after pilot #1 assignment OR pre-wire to a generic Vapi assistant).
4. **Adam-keyboard this week:** P1-1 + P1-2 TrustHub re-submission (~30 min, console — needs Ltd CRO + new business docs).
5. **Claude this week:** P2-1 scoped API keys (~30 min — create via console, rotate consumers).
6. **Claude this month:** P2-2/3/4 cosmetic + retention cleanup.
7. **Re-audit:** Run `twilio-audit.py` monthly + diff against this baseline.

---

## Console-login session (if Adam wants to walk through P0-1 + P1-1 together)

Per `signup-assistant` pattern: Claude opens Chrome to twilio.com login → Adam types email/password → Claude waits → if MFA required, Claude polls swarm Gmail via Gmail MCP for verification code → Adam clicks Submit → Claude drives console to the right screen → Adam reviews + clicks.

Adam-keyboard moments: email/password entry, MFA verification, "Add Funds" amount selection + Stripe-style payment, TrustHub document upload, final Submit buttons. Claude-driven moments: navigation, screen capture, reading rejection text, writing findings.

If Adam wants this session: just say "open chrome to twilio" and Claude will launch + hand off at login.
