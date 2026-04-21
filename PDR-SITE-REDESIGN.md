# PDR · CallMeIE site redesign

> Prototype-before-batch. This PDR covers the full homepage rebuild (`index.html`).
> Once the homepage lands, the same style extends to `onboard.html`,
> `demo.html`, `privacy.html`, `terms.html` in a second pass.

## Pre-Design Gate — tools declared

| Tool | Used for |
|---|---|
| `ui-ux-pro-max` Row 4 (Brutalism) — editorial variant | Base palette + type discipline |
| `frontend-design` skill | Anti-AI-slop bans (no gradients, no emoji, no system fonts) |
| `awesome-design-md` | Stripe + Linear editorial rhythm as reference-adapt target |
| Prior art `samples/industries/08-financial-brutalism-editorial.html` | Proven execution pattern for editorial brutalism on a services brand |

## Brand recap

| | |
|---|---|
| Product | AI phone receptionist for Irish SMBs |
| Stack | Vapi + Twilio + Google Calendar + FastAPI on Render |
| Demo line | +1 (661) 764-3212 |
| Pricing | Starter €149/mo +€297 setup · Professional €249/mo +€297 · Growth €347/mo +€497 · Enterprise quoted |
| Verticals | Dental · Motor factors · Hair salons · Solicitors · catch-all |
| Competitors | VoiceFleet €99-199/mo · Trillet €299/mo (white-label we avoid) |
| Contact | callmeie@proton.me · Limerick, Ireland |

## Style spec — Brutalism Editorial × Telephonic Mono

**Palette (locked):**

| Token | Value | Use |
|---|---|---|
| `--paper` | `#F5F1E8` | Primary background |
| `--ink` | `#0b0a08` | Body text + rules |
| `--burgundy` | `#5A1420` | Single accent — on kickers only |
| `--grey` | `#545454` | Secondary text |
| `--rule` | `#0b0a08` | 3-4px solid, between sections |

**Typography (locked):**

| Family | Role |
|---|---|
| **Archivo Black** | Display headlines (heavy), one-word emphasis |
| **Fraunces** | All body copy, long-form trust paragraphs |
| **JetBrains Mono** | Phone numbers, timestamps, call IDs, durations, prices, addresses |

**No gradients. No shadows. No rounded corners. No emoji. No icons.**
Rules between sections are 3-4px solid ink. Accent bar = 2px burgundy.

## Page sections (in order)

1. **Topbar** — small wordmark left; `call +1 (661) 764-3212` mono button right
2. **Hero** — the phone number IS the display treatment. Giant JetBrains Mono. Above it, Archivo Black headline. Beneath: 1-sentence Fraunces sub. Two CTAs: "Ring the demo" (mono, burgundy accent rule below) and "See it onboard a business" (secondary, text-only)
3. **Ring the demo live** — a dedicated second-hero strip. Instructions: "Pick a vertical. Claire qualifies the call. You're transferred to that vertical's receptionist. Wrap-up SMS follows." Link: `tel:+16617643212`. No icons, just measured text + mono number.
4. **Sample call excerpts** — 3 editorial pull-quotes styled like a call log:
   - `[14:32] Caller · "Looking for a filling, is there anything tomorrow?"`
   - `[14:33] Claire · "I'll put you through to Bright Smile Dental."`
   - `[14:36] Dental AI · "Booked you for Thu 2:15 PM. Text confirmation to +353..."`
   One per vertical.
5. **What it handles** — editorial 2-column list (not cards): Appointments · Missed-call text-back · Emergency transfer · Product/stock queries · Opening-hours queries · Weekly report. Each with a 1-sentence body.
6. **Four verticals, in depth** — 4 editorial blocks, one per vertical. Mono kicker: `DENTAL · MOTOR FACTORS · SALON · SOLICITOR`. Each gets a short paragraph on what Claire's vertical AI actually handles + one sample question pair.
7. **Pricing** — typographic 3-column list (not cards). Setup + monthly + inclusions with dot-leader pattern.
8. **What it costs to run vs. a receptionist** — a truthful comparison table: part-time receptionist €1,800-3,200/mo vs CallMeIE €149-347/mo. No hype, just arithmetic.
9. **Irish-specifics** — mono list: Medical card · PRSI Treatment Benefit · Irish insurance providers (VHI, Laya, Irish Life, Aviva) · Bank holidays · Irish accents tested
10. **Founder statement** — short paragraph signed by the owner, about why this exists and how it's operated
11. **FAQ** — 6 questions in `<details>` editorial list
12. **Final CTA** — large "Ring +1 (661) 764-3212" mono button + "Or submit the onboarding form →" secondary
13. **Footer** — Limerick, Ireland · email · privacy/terms · admin portal link (for owner) · company number once registered

## Anti-patterns (hard bans)

- NO navy → cyan gradient (current site)
- NO emoji anywhere (current site 📞📅💬🚨📦📊)
- NO system font stack — the Google Fonts triple is load-bearing
- NO stock photos, NO phone-call illustrations
- NO "seamless" / "AI-powered" / "transform your business" filler
- NO percentage stats without sources
- NO rounded-rectangle cards — everything is ruled editorial blocks
- NO "trusted by 100+ businesses" social-proof lies
- NO centred hero with a big round microphone graphic

## Acceptance criteria

- [ ] Phone number +1 (661) 764-3212 rendered at clamp(56px, 9vw, 128px) JetBrains Mono, weight 600+
- [ ] Pricing shown all-in (setup fee + monthly clear), 3 tiers + quoted Enterprise
- [ ] At least 3 sample call excerpts with mono timestamps and named speaker
- [ ] Four vertical blocks (Dental, Motor Factors, Salon, Solicitor) with real content
- [ ] Irish-specifics block mentions PRSI, medical card, bank holidays, Irish insurers
- [ ] Zero emoji, zero gradients, zero rounded cards
- [ ] Self-contained single HTML file, Google Fonts only external dep
- [ ] Safety-net responsive block at the end of `<style>` (same pattern as `samples/industries/*`)
- [ ] Renders cleanly at 375px mobile and 1440px desktop
- [ ] All CTAs resolve: `tel:+16617643212`, `mailto:callmeie@proton.me`, `/onboard.html`, `/admin?token=…` (owner-only)
- [ ] Footer includes privacy/terms links + Limerick address + email

## Out of scope (handled in pass 2, after homepage review)

- `onboard.html` restyle
- `demo.html` restyle
- `privacy.html` + `terms.html` restyle
- A dedicated `pricing.html` (only if pricing section in homepage is too dense)

## File path

`C:/Users/a33_s/Desktop/callmeie-fix/index.html` — overwrite the existing.
The current file is preserved in git history; no backup needed.

## After this PDR is approved

Build the homepage as a single strong prototype. Then review against the acceptance
criteria above. Only after sign-off do we propagate the style to the other pages.
