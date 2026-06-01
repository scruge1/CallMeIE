# Vapi ↔ Cal.com Voice-to-Calendar Integration

**Status:** BLOCKED on Cal.com API — see Finding below. Revised 2026-06-01.
**Goal:** A Vapi flow that results in a booking on the self-hosted Cal.com (`https://cal.callmeie.ie`).

---

## CRITICAL FINDING (2026-06-01 probe) — read first

The self-hosted Cal.com runs **only the Next.js web app** (`calcom/cal.com:latest`) + postgres. There is **NO REST API service deployed**:

| Endpoint | Result | Meaning |
|---|---|---|
| `https://cal.callmeie.ie/adam` | **200** | Public booking PAGE works (humans can book) |
| `/api/v2/*` (event-types, slots, me, bookings) | **500** | API v2 (separate `@calcom/api-v2` NestJS container) NOT deployed |
| `/api/trpc/*` | **500** | web tRPC API unhealthy on GET |
| `/api/book/event` | 405 GET / hangs on POST | internal booking route exists but not a stable public API |

**Consequence:** Programmatic booking from Vapi via Cal.com API is **not possible on this deploy as-is**. Adam's API key would NOT unblock it — the API *service* is absent, not just unauthorized. My first draft of this doc (assuming `/api/v2` works) was wrong; corrected here.

## Why Cal.com is still worth keeping (the real ROI)

Per ledger: white-label scheduling for clients who are **not on Google Calendar** (the receptionist's current booking backend uses a Google service account — onboarding friction for Outlook/Apple/no-calendar SMEs). Cal.com's booking PAGE already delivers that today as a shareable link. The API-driven version is a later upgrade.

## What works TODAY (zero-code, no API needed)

**Vapi reads/SMS the booking link.** The receptionist (or a dedicated sales/qualifier assistant) says *"I'll text you our booking link"* and fires an SMS with `https://cal.callmeie.ie/<user>`. Caller self-books on the Cal.com page. This:
- needs no Cal.com API, no API v2 deploy, no new server code
- works for any client regardless of their calendar provider (Cal.com handles the calendar)
- is the prototype-before-batch move: prove the funnel, then automate the booking call later

Implementation = add a Vapi tool / prompt branch that calls existing `send_sms` with the client's Cal.com URL. Reuses `server.py` SMS path already in prod.

## Path to FULL programmatic booking (future task, larger)

Requires deploying the Cal.com API. Two routes:

1. **Deploy `@calcom/api-v2` (NestJS) container** alongside the web app. Needs: the api-v2 image in compose, `NEXTAUTH_SECRET`/`CALENDSO_ENCRYPTION_KEY` shared, an OAuth client created, and the v2 base wired. Then `POST /api/v2/bookings` (Bearer API key + `cal-api-version: 2024-08-13`) becomes usable. This is the proper white-label-API path.
2. **Cal.com Platform/cloud** (api.cal.com) — abandons self-host; recurring cost + not white-label-owned. Rejected (defeats the self-host ROI).

When route 1 is done, the booking call shape is:
```
POST https://cal.callmeie.ie/api/v2/bookings
  Authorization: Bearer <CAL_API_KEY>
  cal-api-version: 2024-08-13
  {"eventTypeId": <int>, "start": "<ISO-UTC>",
   "attendee": {"name","email","timeZone":"Europe/Dublin"}}
```

## Decisions / DON'Ts
- **DON'T** ask Adam for a Cal.com API key yet — useless until the API v2 service is deployed.
- **DON'T** couple any new Vapi tool's `serverUrl` to `callmeie.onrender.com` (BUG-04, dying box) — route to Coolify prod.
- **DON'T** rip out the working Google-Calendar receptionist booking. Cal.com is an *additional option* for non-Google clients, not a replacement.
- **DO** ship the SMS-link funnel first (works today), measure, then decide if API v2 deploy is worth it.

## Next actions
1. (this session) Document done. SMS-link funnel = small, can prototype next.
2. (future, Adam-nod) Deploy `@calcom/api-v2` container on the rig → unlocks programmatic booking + the resellable white-label API.
