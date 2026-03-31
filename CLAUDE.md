# CallMeIE — Agent Context

AI phone receptionist agency for Irish SMBs. Stack: Vapi + Twilio + Google Calendar + FastAPI on Render.

**Full system reference:** `SYSTEM.md` — read this first for all IDs, credentials, pricing, client list.

---

## Architecture in 30 Seconds

```
Prospect rings +1 (661) 764-3212
  → Claire (Vapi squad qualifier)
      → collects name + phone → calls /capture-lead
      → transfers to dental / motor_factors / salon / solicitor (or catch-all)
  → Demo assistant plays the role of a real receptionist
      → calls /demo-complete when wrapping up
  → /vapi/call-ended fires → prospect follow-up SMS

Real client calls → their assistant → /check-availability / /book-appointment / /vapi/call-ended
```

**Server:** `scripts/server.py` — FastAPI, multi-tenant, SQLite at `/tmp/callmeie.db`
**Admin portal:** `callmeie.onrender.com/admin?token=ADMIN_TOKEN`
**Live call log:** Admin portal → Call Log tab (auto-refreshes every 5s)
**Deploy:** push to `main` → Render auto-deploys (3–5 min)

---

## Key IDs

| Thing | ID |
|-------|----|
| Demo Squad | `ff47df7a-41b8-4379-b6ab-8cad448acefd` |
| Claire | `adee3d89-99d8-4f58-9dc3-78c38b9f2a7c` |
| Dental demo | `0b37deb5-2fc2-4e7b-81b1-e61e97103506` |
| Motor demo | `8a533a56-2ca4-486f-b328-69183b59fa41` |
| Salon demo | `db4ab378-cd8a-40f5-b3f9-8fcaaba408b0` |
| Solicitor demo | `7774b535-95fe-4e75-b571-dde098e2f8fb` |
| Demo phone | +1 (661) 764-3212 |
| Owner SMS | +353 85 786 3564 |

Vapi API key + Twilio creds: see `SYSTEM.md` Section 2 and 5c.

---

## Server Endpoints

| Endpoint | Triggered by |
|----------|-------------|
| `POST /capture-lead` | Claire's captureLead Vapi tool |
| `POST /demo-complete` | Demo assistant's demoComplete Vapi tool |
| `POST /vapi/call-ended` | Vapi post-call webhook (all assistants) |
| `POST /check-availability` | Vapi calendar tool |
| `POST /book-appointment` | Vapi calendar tool |
| `POST /submit-onboarding` | onboard.html form |
| `GET  /admin` | Admin portal UI |
| `GET  /admin/api/events` | Live call log (call_events table) |
| `GET  /admin/api/submissions` | Onboarding queue |
| `POST /admin/api/provision/{id}` | One-click client provisioning |

---

## Common Tasks

### Add a new demo assistant (new business vertical)
1. Create assistant via `POST https://api.vapi.ai/assistant` with name = routing slug (e.g. `pharmacy`)
2. Add 5 tools: `google.calendar.availability.check`, `google.calendar.event.create`, `sms`, transferToOwner, `demoComplete`
3. Set `serverUrl: https://callmeie.onrender.com/vapi/call-ended`
4. Add to squad: `PATCH /squad/ff47df7a...` — include in members array with `assistantDestinations: []`
5. Update Claire's squad member destinations to include new assistant
6. Add `transferToX` tool on Claire pointing to new assistant name
7. Update `DEMO_ASSISTANT_IDS` dict in `server.py`
8. Update `SYSTEM.md`

### Provision a new client (after onboarding form submitted)
1. Go to admin portal → Queue tab
2. Click Provision — creates Vapi assistant + 4 tools automatically
3. SMS client: share Google Calendar with `callmeie-receptionist@callme-ie.iam.gserviceaccount.com`
4. Assign Twilio number to new assistant in Vapi dashboard
5. Add assistant ID to `CLIENTS_JSON` env var on Render

### Debug a failed call
1. Admin portal → Call Log tab
2. Find the call_id, check event sequence
3. Common issues:
   - `lead-captured` missing → Claire's captureLead tool not firing (check tool schema)
   - `demo-complete` missing → tool not in demo assistant's model.tools (re-run restore_tools.py)
   - `call-ended` missing → serverUrl not set on that assistant
   - Transfer silently failing → assistantName in transferCall doesn't match squad member name
4. Check Render logs for Python exceptions: `render.com → callmeie-receptionist → Logs`

### Fix Vapi tool stripping
**Critical gotcha:** When you PATCH `model.messages` without including `model.tools`, Vapi clears the tools.
Always GET the assistant first, merge tools, then PATCH the full model object.
See `/c/Users/a33_s/AppData/Local/Temp/restore_tools.py` for the pattern.

### Update a demo assistant prompt
```python
# Always do this — never PATCH messages alone
d = api("GET", f"/assistant/{assistant_id}")
current_tools = d["model"]["tools"]  # preserve
new_prompt = "..."
api("PATCH", f"/assistant/{assistant_id}", {
    "model": {
        "provider": "openai", "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": new_prompt}],
        "tools": current_tools,  # must include or they get wiped
    }
})
```

---

## Known Issues / Gotchas

- **Render free tier:** SQLite at `/tmp/callmeie.db` is wiped on redeploy. Submissions/leads are ephemeral. SMS fires immediately so leads aren't lost, just the DB record.
- **Twilio:** Currently US number (+16617643212). Irish +353 pending regulatory approval. SMS to Irish numbers may be blocked until upgraded.
- **HTTPS on callmeie.ie:** Blocked until IEDR grants DNS control (Irish citizenship verification pending).
- **Vapi `assistantName` routing:** Must match the `name` field on the Vapi assistant exactly. Demo assistants are named `dental`, `motor_factors`, `salon`, `solicitor` (simple slugs, not display names).
- **Squad transfers:** Use `assistantId` in squad member `assistantDestinations`, but `assistantName` in transferCall tool `destinations`.

---

## Peer Role

When running as the CallMeIE ops peer:

**Autonomous (no approval needed):**
- Read call logs and diagnose issues
- Research new business verticals for demo expansion
- Draft new system prompts for demo assistants
- Write scripts for client setup

**Needs owner approval:**
- PATCH any live Vapi assistant
- Push to GitHub main
- Send SMS via Twilio
- Provision a new client (involves creating Vapi assistants + billing)

**Escalate immediately via note to owner if:**
- A demo call shows an error in the call log (missing tools, failed transfer)
- A new submission arrives in the onboarding queue
- A booking fails

**Upgrade path:** Once first client revenue comes in, this peer gets its own API credits and can run scheduled monitoring (check call log hourly, flag anomalies, send weekly digest to owner).
