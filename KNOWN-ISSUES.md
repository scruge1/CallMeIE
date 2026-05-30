# CallMeIE Receptionist — Known Issues

Filed from live demo-line transcript review. Evidence = Vapi call IDs (`api.vapi.ai/call/{id}`).
Demo line: **+353 61 788120** (Limerick squad, Twilio→Vapi `phoneNumberId 67cf10fd-c91c-40d6-a0b0-489c42487df2`).

## Open

### BUG-01 — Brand mispronounced in greeting/pitch (P2, sales-facing)
- Greeting says **"I'm Claire Claire"** (name duplicated).
- Pitch reads brand as **"Call Me IE. E?"** / "Call Me I E. E?" — raw `IE`/`.ie` hits TTS wrong.
- Rule already exists for ad voice (never feed raw `callmeie.ie`; spoken = "Call Me I E"). Not applied to the live Limerick **squad** assistants' first message + pitch script.
- Evidence: calls `019e7a93`, `019e7aa1` (2026-05-30).
- Fix: normalize brand token to "Call Me I E" in squad greeting + pitch prompts; remove duplicate "Claire".

### BUG-02 — Post-booking loop burns minutes (P1, cost)
- After handoff completes ("Talk soon. Bye"), assistant re-enters discovery ("Right. That makes sense for an undertaker…") instead of ending the call. Caller goes silent → ends only on `silence-timed-out`.
- Wasted **7 min** on call `019e7aa1` (413s, silence-timeout). Direct Vapi-minute cost + Stripe meter burn.
- Fix: terminate (end-call) after booking/handoff confirmation; do not return control to the discovery/pitch node.

### BUG-03 — Flat "€149 a month" stated (P2, pricing accuracy)
- Pitch says "Call Me IE is a €149 a month" — missing **"from"**. Canonical = `PRICING-SSOT.md` (€149 is entry/"from", not flat).
- Evidence: `019e7a9b`, `019e7aa1`.
- Fix: "from €149 a month" in pitch script.

### BUG-04 — RETRACTED 2026-05-30 (was: "lead capture not persisting")
- ORIGINAL (WRONG) CLAIM: `/admin/api/leads-unified`=0 and `/admin/api/events` frozen at 2026-04-26 ⇒ leads dropping.
- Adam corrected: leads DO work — SMS notification fires per call, admin dashboard updates with the recording (he found the prank by listening to the recording in the dashboard).
- REAL open question (renamed BUG-06): the backend I queried, `callmeie.onrender.com/admin/api/events`, returned only 45 rows ending 2026-04-26 — it is NOT the backend serving Adam's live dashboard. Either a stale/secondary deployment or a different DB. Pin down the real admin backend.

### BUG-06 — Backend identity mismatch → RESOLVED 2026-05-30
- REAL admin backend = **`https://admin.callmeie.ie`** → `178.104.205.255` (**Hetzner VPS / Coolify**), idmax 1587, live to 2026-05-30 21:23, serves the dashboard Adam uses (recordings, SMS, call-flag).
- `callmeie.onrender.com` is a **STALE/secondary deploy** (separate DB, frozen id≤45 / 2026-04-26). It still answers + accepts writes — my first spam-flags landed there uselessly.
- FIX APPLIED: re-flagged all unflagged pranks on `admin.callmeie.ie` — `019e7a93`, `019e7ac4`, `019e7ac5` (+ Adam already flagged `019e7a9b`, `019e7aa1` in the dashboard). All 5 troll calls now `spam` on the live backend.
- **INFRA.md is stale** — §3 documents `callmeie.onrender.com` as THE backend; the live admin is `admin.callmeie.ie` on Hetzner/Coolify. Two live backends with divergent DBs is a real risk (see BUG-07).
- LESSON: verify backend identity (DNS + a freshness probe) before diagnosing "data missing" — the Apr-26 freeze was the wrong deploy, not lost data.

### BUG-07 — Two live backends, divergent DBs (P2, ops hazard)
- `admin.callmeie.ie` (Hetzner/Coolify, current) and `callmeie.onrender.com` (frozen Apr-26) both respond and both accept admin writes with the same `ADMIN_TOKEN`.
- Risk: ops/scripts hitting onrender silently no-op against reality. Decide: retire onrender, or document it as a non-authoritative mirror. Update INFRA.md §3 to name `admin.callmeie.ie` as canonical.

### Block status note (BUG-05)
- Twilio repoint went live **21:26:11 UTC**; two troll calls at **21:23** predate it (not a block failure). Self-test (synthetic POST) passed: deny→`<Reject>`, allow→`<Redirect>`. **Not yet verified against a real inbound call** — watch the next attempt from `+353852345595`.

## Resolved

### BUG-05 — No inbound blocklist → RESOLVED 2026-05-30
- One caller (`+353852345595`) trolled the demo line 3× in 15 min (2026-05-30 20:29–20:44).
- Fix: Twilio Serverless Function screens inbound BEFORE Vapi. Denylisted `From` → `<Reject>`; all others → `<Redirect>` to `https://api.vapi.ai/twilio/inbound_call`.
- Deploy script (idempotent, editable denylist): `scripts/deploy_call_screen.py`.
- Function URL: `https://callmeie-call-screen-1962-scr.twil.io/screen` (service `callmeie-call-screen`, env `prod`).
- Number `+35361788120` VoiceUrl → Function; **VoiceFallbackUrl → Vapi** so a Function failure still routes to Vapi (demo line cannot break from this).
- To edit the denylist: change `DENYLIST` in the script, re-run `python deploy_call_screen.py`.
- Did NOT pick the Vapi assistant-request hook: it requires server-driven routing, which puts free-tier Render (30–90s cold start) inside Vapi's ~7.5s assistant-request window → would intermittently kill the demo line.

> **Vapi-side hub DB BUG-04 still OPEN** — no serverUrl set on number/squad/assistants, so end-of-call webhooks aren't landing (leads-unified=0, events frozen at 2026-04-26). Real customer leads silently lost. Next priority.
