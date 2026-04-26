# CallMe.ie — Master System Document

> Single source of truth for the entire CallMe.ie AI Receptionist system.
> Update this file every time anything changes: new assistants, phone numbers, tools, features, pricing, clients, links.
> Last updated: 2026-04-01

---

## 1. WHAT CALLME.IE IS

An Irish AI phone receptionist agency. We build and operate AI voice assistants that answer calls 24/7 for Irish SMBs — booking appointments, handling FAQs, capturing leads, sending SMS confirmations, and transferring emergencies. Built on Vapi + Twilio + Google Calendar + our own webhook server hosted on Render.

**Target market:** Limerick / Munster initially, scaling nationally.
**Niche focus:** Dental, motor factors, salons, solicitors, and dynamic fallback discovery for every other business type.
**Pricing:** €149-347/mo (see Section 6).

---

## 2. ACCOUNTS & CREDENTIALS

| Service | Account | Login | Notes |
|---------|---------|-------|-------|
| Vapi | dashboard.vapi.ai | GitHub (scruge@pm.me) | PAYG, ~7.87 credits remaining |
| Twilio | console.twilio.com | — | Trial $15.50 credits. Needs upgrade + Ireland RC bundle for +353 |
| Google Cloud | console.cloud.google.com | — | Calendar API enabled, service account active |
| Render | render.com | — | Webhook server deployed |
| GitHub | github.com/scruge1 | — | CallMeIE repo |

**Render service URL:** `https://callmeie.onrender.com`
**GitHub repo:** `https://github.com/scruge1/CallMeIE`

---

## 3. VAPI ASSISTANTS

### 3a. Demo System (Sales / Prospect Experience)

| Assistant | ID | Phone Number | Purpose |
|-----------|----|-------------|---------|
| **Claire** (qualifier) | `adee3d89-99d8-4f58-9dc3-78c38b9f2a7c` | +1 (661) 764-3212 (main demo line) | Greets prospects, qualifies business type, warm-transfers to niche demo |
| **Bright Smile Dental** | `0b37deb5-2fc2-4e7b-81b1-e61e97103506` | (via squad) | Demo: Irish dental practice (booking, PRSI, medical card, emergency) |
| **Murphy's Motor Factors** | `8a533a56-2ca4-486f-b328-69183b59fa41` | (via squad) | Demo: Irish motor factors (stock queries, pricing, delivery, hours) |
| **City Salon (Aoife)** | `db4ab378-cd8a-40f5-b3f9-8fcaaba408b0` | (via squad) | Demo: Hair salon (booking, services, hours) |
| **O'Brien Solicitors (Ciara)** | `7774b535-95fe-4e75-b571-dde098e2f8fb` | (via squad) | Demo: Legal firm (conveyancing, family, wills, consultations) |
| **General Business Discovery** | `3e2f8e1c-e4eb-46ab-b8be-d7f97cbe6080` | (via squad) | Dynamic fallback demo for every business type outside the 4 specialist verticals |

**Demo Squad ID:** `ff47df7a-41b8-4379-b6ab-8cad448acefd` (Vapi Squad — no extra phone numbers needed)
**Demo number (hand to prospects):** +1 (661) 764-3212 ← Claire answers, squad routes
_(Swap to Irish +353 number once Twilio verified)_

### 3b. Client Assistants

| Business | Assistant ID | Phone Number | Calendar | Status |
|----------|-------------|-------------|----------|--------|
| _(first paying client — TBD)_ | — | — | — | Prospecting |

### 3c. Vapi API Keys

| Org | API Key | Used For |
|-----|---------|----------|
| Primary (API org) | stored in env only | Scripts, server |
| Browser org | Get from dashboard | Manual dashboard work |

---

## 4. VAPI TOOLS (Shared Tool IDs)

All tools now live in Vapi tool library. Attached inline (no toolId) to assistants. Re-use or clone for each new client.

| Tool | ID | Endpoint |
|------|----|----------|
| Check Availability (Google Calendar) | `cdbbcd96-b7d1-4646-bb10-9889dae214af` | `POST /check-availability` |
| Book Appointment (Google Calendar) | `a9b9aad6-fca7-4e4f-a418-a1f1d9809b74` | `POST /book-appointment` |
| Transfer Call (emergency) | `9b7299fe-68e2-40d9-a735-91b26b22b3e4` | Vapi native transferCall |
| SMS Confirmation | `31ef6572-41d0-456c-938e-c1c287476af4` | Vapi native sms |
| Product Query (knowledge base) | `2e65ba9d-3bb9-432e-8915-a8e4681ec1a9` | Vapi KB search |

**Transfer tool numbers:**
- Emergency on-call: +353 85 786 3564 (owner's number — update per client)

---

## 5. INFRASTRUCTURE

### 5a. Webhook Server

- **Live URL:** `https://callmeie.onrender.com`
- **Platform:** Render (free tier — spins down after 15min inactivity)
- **Keep-alive:** GitHub Actions workflow `.github/workflows/keep-alive.yml` pings `/health` every 5 min
- **Backup monitoring:** UptimeRobot (set up separately)

**Endpoints:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check — returns client count, Twilio status |
| `/vapi/call-ended` | POST | Vapi post-call hook — missed call text-back, owner alert |
| `/check-availability` | POST | Vapi tool — Google Calendar free slots |
| `/book-appointment` | POST | Vapi tool — Create calendar event + SMS |
| `/reminder` | POST | External scheduler — 24hr appointment reminder SMS |
| `/no-show` | POST | External scheduler — no-show follow-up SMS |
| `/sync-inventory` | POST | Sync Google Sheet → Vapi knowledge base |
| `/capture-lead` | POST | Vapi tool (Claire) — capture demo prospect name/phone, SMS owner |
| `/submit-onboarding` | POST | Client onboarding form — receive new client data, SMS owner |

### 5b. Google Calendar Integration

- **Service account:** `callmeie-receptionist@callme-ie.iam.gserviceaccount.com`
- **Credentials:** Service account JSON key (in Render env vars as `GOOGLE_SERVICE_ACCOUNT_JSON`)
- **Per client:** Share their Google Calendar with the service account email (Make changes to events)
- **Demo calendar:** `swarm.agent.2026@gmail.com` — shared with service account ✓

### 5c. Render Environment Variables

| Variable | Value | Notes |
|----------|-------|-------|
| `TWILIO_ACCOUNT_SID` | (set in Render — never commit) | Twilio SID |
| `TWILIO_AUTH_TOKEN` | (set in Render) | Twilio auth |
| `TWILIO_FROM_NUMBER` | +16617643212 | Current US number |
| `OWNER_NOTIFICATION_NUMBER` | +353857863564 | Owner SMS alerts |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | (JSON blob) | Calendar API auth |
| `CLIENTS_JSON` | (JSON blob — see below) | Per-assistant routing |
| `PORT` | 8080 | Auto-set by Render |

**Current CLIENTS_JSON note (demo assistants updated 2026-04-01):**
```json
{
  "adee3d89-99d8-4f58-9dc3-78c38b9f2a7c": {
    "name": "CallMe.ie Demo",
    "owner": "+353857863564",
    "from": "+16617643212",
    "calendar_id": "primary"
  },
  "0b37deb5-2fc2-4e7b-81b1-e61e97103506": {
    "name": "Bright Smile Dental",
    "owner": "+353857863564",
    "from": "+16617643212",
    "calendar_id": "primary"
  },
  "8a533a56-2ca4-486f-b328-69183b59fa41": {
    "name": "Murphy's Motor Factors",
    "owner": "+353857863564",
    "from": "+16617643212",
    "calendar_id": "primary"
  },
  "db4ab378-cd8a-40f5-b3f9-8fcaaba408b0": {
    "name": "City Salon",
    "owner": "+353857863564",
    "from": "+16617643212",
    "calendar_id": "primary"
  },
  "7774b535-95fe-4e75-b571-dde098e2f8fb": {
    "name": "O'Brien Solicitors",
    "owner": "+353857863564",
    "from": "+16617643212",
    "calendar_id": "primary"
  },
  "3e2f8e1c-e4eb-46ab-b8be-d7f97cbe6080": {
    "name": "General Business Discovery",
    "owner": "+353857863564",
    "from": "+16617643212",
    "calendar_id": "primary"
  }
}
```
_(Example shape only. Keep live assistant ids and routing in sync with the current Vapi squad.)_

### 5d. Twilio

- **Current number:** +1 (661) 764-3212 (US, trial)
- **Next step:** Upgrade to Pay-as-you-go → submit Ireland Regulatory Compliance bundle → buy +353 number
- **Estimated cost:** €1-2/mo per Irish number + €0.07/min calls

---

## 6. KNOWLEDGE BASE

| KB | File ID | Contents | Syncs From |
|----|---------|----------|-----------|
| Bright Smile Dental | `f90cf5a0-e723-480b-86f6-7e5fdf4ac3c8` | Services, pricing, hours, FAQs, insurance | Static (manual update) |
| Murphy's Motor Factors | `56bbdc26-c23d-42fb-909c-34e7f17ce1e6` | Parts categories, hours, delivery schedule, contact | Static (shared with dental KB — TODO: create separate KB) |

---

## 7. PRICING TIERS

| Plan | Monthly | Setup Fee | Minutes included | Overage | Margin at cap |
|------|---------|-----------|-----------------|---------|---------------|
| **Starter** | €149/mo | €297 | 300 min/mo | €0.22/min | ~€102 (68%) |
| **Professional** | €249/mo | €297 | 600 min/mo | €0.20/min | ~€157 (63%) |
| **Growth** | €347/mo | €497 | 1,200 min/mo | €0.18/min | ~€165 (47%) |
| **Enterprise** | Custom | Custom | Custom | Custom | — |

**Our cost per client (Vapi PAYG):**
- ~€0.12-0.17/min all-in (Vapi platform $0.05 + STT ~$0.004 + TTS ~$0.05 + LLM ~$0.03 + Twilio ~$0.01)
- Twilio Irish number: €2/mo fixed
- Margin at included-minute cap: €102–€165/mo (47–68%)
- Overage: €0.18–0.22/min charged vs ~€0.15/min cost = €0.03–0.07/min profit on every extra minute
- Breakeven: 1 Starter client covers Render + Twilio base costs

---

## 8. DEMO FLOW (The Sales Pitch)

**Prospect rings the CallMe.ie demo number.**

```
+1 (661) 764-3212  →  Claire (qualifier)
    ↓
"Hi, I'm Claire from CallMe.ie. What type of business do you run?"
    ↓
Dental          →  warm transfer →  Bright Smile Dental demo
Motor Factors   →  warm transfer →  Murphy's Motor Factors demo
Salon           →  warm transfer →  City Salon demo
Solicitor       →  warm transfer →  O'Brien Solicitors demo
Other           →  warm transfer →  General Business Discovery demo
    ↓
End of demo call:
"You've just experienced what your customers would hear.
 Can I take your name and number so our team can ring you back?"
    ↓
SMS lead alert to owner: "[CallMe.ie Lead] John Murphy, dental practice Cork —
                          wants pricing call. Rang demo 14:32."
```

**Why it works:** Prospect experiences the product firsthand. No video, no deck. The call IS the pitch.
**Competitor gap:** No Irish competitor (VoiceFleet, NeuralWave) offers a callable demo number.

---

## 9. DEMO ASSISTANTS — PROMPTS & PERSONAS

### Claire — Qualifier (Demo Gatekeeper)

**Voice:** Amy (ElevenLabs, warm Irish tone)
**First message:** "Hi there! Thanks for ringing CallMe.ie. I'm Claire, the virtual assistant here. Can I ask — what type of business do you run?"
**Goal:** Qualify → route to correct niche demo → capture lead at end
**Transfer targets:** Dental, Motor Factors, Salon, Solicitor, General Business Discovery

### Bright Smile Dental Demo

**Persona:** Friendly Irish dental receptionist
**Handles:** New patient booking, PRSI queries, medical card queries, emergency transfer, hours/location/services FAQs, payment plans
**Irish specifics:** PRSI Treatment Benefit (free exam + €15 scale & polish), medical card entitlements, insurance (VHI, Laya, Irish Life, Aviva)
**Voice:** Amy

### Murphy's Motor Factors Demo

**Persona:** Efficient trade counter assistant — terse, knowledgeable, no-nonsense
**Handles:** Stock availability, pricing, delivery runs, sourcing, opening hours, account queries
**Irish specifics:** Same-day delivery runs, trade vs retail callers, Irish parts catalogue
**Voice:** TBD (male voice for motor factors feel)

---

## 10. SCRIPTS & TOOLS

| Script | Location | Purpose |
|--------|----------|---------|
| `server.py` | `scripts/` | Main webhook server (FastAPI) |
| `calendar_api.py` | `scripts/` | Google Calendar integration |
| `setup-new-client.py` | `scripts/` | ONE command to spin up new client |
| `sync-inventory.py` | `scripts/` | Google Sheet → Vapi KB sync |
| `generate-report.py` | `scripts/` | Weekly/monthly client reports |
| `upload-knowledge-base.py` | `scripts/` | Push product catalogues to Vapi |
| `lead-scraper.py` | `scripts/` | Google Maps → leads CSV |

**Local dev:** `start-server.bat` (runs FastAPI + ngrok tunnel together)

---

## 11. LEADS PIPELINE

| File | Contents | Count |
|------|----------|-------|
| `demo/leads_dentist_Limerick Ireland_20260330_1408.csv` | Limerick dental practices | 8 |
| _(motor factors leads)_ | TBC — run lead-scraper.py | — |

**Cold outreach templates:** `templates/email-outreach/`

---

## 12. COMPETITORS (Irish Market)

| Competitor | URL | Price | Gap |
|-----------|-----|-------|-----|
| VoiceFleet | voicefleet.ai | €99-199/mo | No demo number, blog-only marketing |
| NeuralWave | ai-voice.ie | Unlisted | No demo number, sign-up form only |
| ViveoAI | viveoai.com | Unlisted | Dental/UK focused, newer |
| Upfirst (US) | upfirst.ai | ~$25/mo | Generic US product, Irish number only |

**Our edge:** Demo number you can ring NOW + Irish-specific niche packs (PRSI, medical card, motor trade) + done-for-you vs DIY.

---

## 13. KEY DECISIONS & RATIONALE

| Decision | Rationale |
|----------|-----------|
| Vapi over white-label platforms | Better margins (no €299/mo Trillet fee), full control, client-specific config |
| Dental + Motor factors as demo niches | Dental = highest ROI/missed call, Motor factors = uniquely Irish, zero AI competition |
| Claire as qualifier not IVR | Natural conversation feels premium vs "press 1 for dental" |
| Render for hosting | Free tier, auto-deploy from GitHub |
| Per-assistant CLIENTS_JSON routing | One server handles all clients — scales to 50+ without code changes |
| Irish +353 number as priority | Prospects won't ring a US number; builds local trust |

---

## 14. NEXT ACTIONS

- [x] Create Claire assistant in Vapi — `adee3d89-99d8-4f58-9dc3-78c38b9f2a7c`
- [x] Create Murphy's Motor Factors assistant — `8a533a56-2ca4-486f-b328-69183b59fa41`
- [x] Add PRSI / medical card handling to Bright Smile Dental
- [x] Create Demo Squad — `ff47df7a-41b8-4379-b6ab-8cad448acefd`
- [x] Assign squad to +1 (661) 764-3212
- [ ] Upgrade Twilio → buy +353 Irish number
- [ ] Move Claire to +353 number
- [ ] Run lead-scraper for motor factors in Limerick
- [ ] First client outreach (8 dental leads in CSV)
- [x] Build onboarding form — `callmeie.ie/onboard` (7-section HTML form, submits to `/submit-onboarding`)
- [x] Add lead capture to Claire — captures name/phone before demo transfer, SMS owner instantly

---

## 15. CHANGELOG

| Date | Change |
|------|--------|
| 2026-03-30 | Project initialised. 48 files built. Dental assistant live. Calendar integration working. |
| 2026-03-31 | CLIENTS_JSON updated with both Vapi org IDs. toolCallId bug fixed. Calendar shared with service account. |
| 2026-03-31 | SYSTEM.md created. Claire + Murphy's Motor Factors built. Dental updated with PRSI/medical card. Demo Squad created. +1 (661) 764-3212 now routes through squad — Claire answers, transfers to dental or motor demo. |
| 2026-03-31 | Lead capture added to Claire (captureLead tool → /capture-lead endpoint). Onboarding form built (onboard.html → /submit-onboarding). Two new server endpoints live on next Render deploy. |
| 2026-03-31 | Admin portal built (admin.html + SQLite queue + one-click provisioning). Scripts directory cleaned up (21→13 files). |
| 2026-03-31 | Two new demo assistants: City Salon (Aoife, db4ab378) + O'Brien Solicitors (Ciara, 7774b535). Both in squad. Claire now routes to all 4 demos. Demo assistants renamed to simple routing names (dental/motor_factors/salon/solicitor). |
| 2026-04-01 | General Business Discovery fallback assistant added to the demo squad. Claire now routes all non-core business types into a dynamic fallback demo instead of ending at a generic catch-all path. |
| 2026-04-01 | Live callback flow hardened: Render `/demo-complete` now creates internal sales callback events for actionable demo leads, and live assistants now expose richer callback fields for follow-up context. |
