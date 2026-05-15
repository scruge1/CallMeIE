# CallMeIE / Owl Studio — Pricing Single Source of Truth

**Status:** `canonical` · created 2026-05-15
**Authority:** This file is the canonical pricing reference for all CallMeIE / Owl Studio services. It is derived from the **live website** (`callmeie.ie`, source `callmeie-hub/`), which is what we actually advertise to customers. Stripe products, payment links, `~/.claude/routes/.env`, and `INFRA.md §15` MUST match this file. Where they differ, this file wins and Stripe is corrected to it.

> Supersedes for *advertised* pricing: `Next Revenue Ideas/document-ops/PRICING.md` (that file is a `draft` research/anchoring doc — internal value-based pricing logic, NOT what we advertise). PRICING.md numbers (Entry Pilot €750, Operations €750-1,500/mo) do **not** match the site and must not be quoted to customers.

Mirror rule (inherited from INFRA.md): on any pricing change, update this file FIRST, then mirror to `callmeie-fix/PRICING-SSOT.md`, then reconcile Stripe, then update `INFRA.md §15.3` and `~/.claude/routes/.env` in the same commit.

---

## 1. AI-First Websites

Source: `callmeie-hub/websites/index.html` §3 `#pricing` ("Pick a tier, not a subscription. … one all-in fee"). One-off project fee, not a subscription. Source code handed to client, no lock-in.

| Tier | Advertised price | Timeline | Key inclusions | On-site CTA link |
|---|---|---|---|---|
| **Starter** | **€695** all-in | 7 days | 1 long landing page, 150–300 words + 6 photos, mobile/tablet/desktop, contact form, 1yr Cloudflare Pages hosting + SSL + GDPR, 1 revision round | `cNicN40Y31imenO2HzaIM07` |
| **Pro** *(most popular)* | **€1,595** all-in | 14 days | 5-page site, bespoke design, Decap CMS, copywriting (home+services+about), GBP setup, basic SEO, 2yr hosting + backups, 2 revision rounds | `bJe28qeOTf9c5RidmdaIM08` |
| **Custom** | **from €2,950** quoted | 4–6 wks | E-commerce / SaaS / multi-language / bespoke integrations. Scoped via €99 audit first | `bJe6oGbCH7GK0wY4PHaIM00` (the €99 audit) |

**Add-ons / entry points (websites):**
- **Copywriting add-on (Starter):** **+€150** (Starter tier-fine print).
- **€99 style-match scoping audit:** one-off €99, **credited against the final Custom invoice if booked within 30 days**. This is the *Custom* tier entry point. **The €99 is an audit, NOT a website price — we do not sell websites "from €99".**

---

## 2. AI Phone Receptionist

Source: `callmeie-hub/receptionist/index.html` §pricing ("Month-to-month on Starter and Professional. Three-month minimum on Growth."). Vertical landing pages (`cafe.html`, `dental.html`, …) repeat the same numbers.

| Tier | Monthly | Setup (one-time) | Commitment | Included usage | Overage |
|---|---|---|---|---|---|
| **Starter** | **€149/mo** | **€297** | month-to-month | 300 min/mo, business-hours cover | €0.22/min |
| **Professional** *(featured)* | **€249/mo** | **€297** | month-to-month | 600 min/mo, 24/7 cover | €0.20/min |
| **Growth** | **€397/mo** | **€497** | 3-month minimum | 1,200 min/mo, 24/7 + integrations | €0.20/min |
| **Enterprise** | Quoted (`mail hello@callmeie.ie`) | Quoted | Per scope | Multi-location, PMS/CRM/ERP, SLA | — |

> **RESOLVED:** the historical €347-vs-€397 Growth conflict is settled — the **entire live site now shows €397** (main receptionist page + cafe + dental verticals + cost-comparison table). Canonical Growth monthly = **€397**. Any €347 reference anywhere is stale and must be corrected.

> **Minor site inconsistency to fix (copy, not price):** main receptionist page expresses included usage as **minutes** (300/600/1,200); vertical pages express it as **calls** (200/500/1,500). Pick one unit sitewide. Not a Stripe issue.

**Receptionist add-ons:**
- Extra minutes billed per-minute at the tier rate above (Stripe Billing Meter `vapi_minutes`, `mtr_61UbGf0xrOZ6vdadl41CEqG2AuI1zWoq`).
- Dental vertical: optional SMS recall reminder at 5-month mark (priced per engagement, not a public SKU).

Sales flow is `onboard.html` (form → per-client Vapi provisioning), so per-tier public payment links are optional; the **setup fee** and **monthly** still need correct Stripe prices for invoicing/portal.

---

## 3. Document Ops

Source: `callmeie-hub/docs/index.html` §`#pricing` (line 1618 — a full, explicit pricing section with price cards; this is firm advertised pricing, not illustrative).

| Tier | Advertised price | Includes | On-site CTA link |
|---|---|---|---|
| **Entry Pilot** | **€500** one-time | 25 docs, 1 doc type, CSV, 14-day | `dRm00i0Y3gdgbbCgypaIM09` |
| **Standard Pilot** *(featured)* | **€1,500** one-time | 100 docs, schema mapping, review queue, run report | `14A3cueOTbX04NeeqhaIM0a` |
| **Auto** | **€99/mo** | 200 docs/mo, 98%-auto-post, self-serve | `14A9AS9uzgdggvW95XaIM0f` |
| **Auto Plus** *(featured)* | **€249/mo** | 600 docs/mo, multi-tenant, 2 seats incl. | `eVq6oG8qvaSW0wYgypaIM0g` |
| **Rescue + Export** | **€499/mo** | 250 docs/mo, 10 human rescues, 3-biz-day SLA | `4gM14m2273qufrS3LDaIM0h` |
| **Bespoke** | **from €1,500/mo** | custom schema/export, 5-day SLA, ≤2 slots | `mailto:` ("Talk to us" — no Stripe link by design) |

**Doc Ops add-ons / notes:**
- Auto Plus: 2 tenant seats included, **+€40/mo per extra seat**.
- Fineprint: existing **Operations Monthly Starter €250/mo** subscribers are **grandfathered** — that legacy link/price stays active but unadvertised (do NOT archive it).
- The €95–275/mo figure on the docs page is **competitor pricing** (Klippa/AutoEntry "Cheap OCR"), not ours.
- The site's Doc Ops links already resolve to correctly-priced Stripe objects (€500/€1,500/€99/€249/€499). Drift here is duplicate product *generations* + product *names/caps* in Stripe, **not** wrong amounts.

---

## 4. Local SEO (advertised add-on; email-sold, no Stripe link)

Source: `callmeie-hub/local-seo/index.html`. Sold via `mailto:hello@callmeie.ie`, not Stripe checkout.

| Tier | Price |
|---|---|
| Growth | €245/mo (cancel anytime; 6-month no-progress refund on last month) |
| (higher tier) | €495/mo ("Everything in Growth" + monthly blog post) |
| GBP audit | €99 one-off (credited to first month if signs on) |

> Local SEO tier names/structure on the page need a clean 3-tier table (Essential / Growth / +) — currently only Growth €245 and a €495 tier are clearly labelled. Flagged for a website copy pass, not a Stripe action.

---

## 5. Canonical → Stripe binding (what each price ID/link MUST be)

| Canonical item | Correct amount | Stripe price (live) | Status vs canonical |
|---|---|---|---|
| Websites Starter | €695 one-off | `price_1TRsbHCEqG2AuI1zdlIK9J81` ("Starter site deposit (€348)") | ❌ **charges €348 (50% deposit), site says "€695 all-in"** |
| Websites Pro | €1,595 one-off | `price_1TRsbHCEqG2AuI1zvaFxw5iM` ("Pro site deposit (€798)") | ❌ **charges €798 (50% deposit), site says "€1,595 all-in"** |
| €99 style-match audit | €99 one-off | `price_1TObWJCEqG2AuI1zfTgTfGNe` | ✅ correct |
| Receptionist Professional | €249/mo | `price_1TVxwgCEqG2AuI1zPZzlP7q3` | ✅ amount correct — ⚠️ no active payment link |
| Receptionist Growth | €397/mo | `price_1TVxwgCEqG2AuI1zzsCH9YG1` | ✅ amount correct — ⚠️ no active payment link |
| Receptionist Starter | €149/mo | *(none — Path B never created Starter)* | ❌ missing |
| Receptionist Setup (Starter/Pro) | €297 one-off | `price_1TVxwgCEqG2AuI1zkxlRvISY` | ✅ correct |
| Receptionist Setup (Growth) | €497 one-off | *(none — only €297 setup exists)* | ❌ missing |
| Doc Ops Entry Pilot | €500 one-off | `price_1TSPnpCEqG2AuI1zwjKLxK9A` | ✅ correct |

### Orphans / duplicates active in Stripe (NOT on the live website)

- **Obsolete 4-tier receptionist ladder (AUD-021, 2026-04-30):** `CallMeIE · Starter €99/mo`, `Growth €299/mo`, `Pro €699/mo`, `Concierge €1,500/mo` (+ yearly + overage = 12 prices, products `prod_UQow…`). Superseded by current €149/€249/€397 model. **Candidate: archive.**
- **Doc Ops — triple-stacked ladders.** Three parallel generations all active: (a) Pilot Entry/Standard + Operations Monthly Starter €250/mo (2026-05-01); (b) "Document Ops — Self-serve Auto/Auto Plus/Rescue" (D22, 2026-05-04); (c) "Doc Ops Starter/Core/Rescue/Bespoke" (newest). ~10 active doc-ops links for 1 advertised product. **Candidate: keep one ladder, archive the rest.**
- **Owl Studio care plans** Essential €45/mo · Growth €95/mo · Concierge €195/mo (+ yearly) — 6 active links. Not advertised anywhere on `callmeie-hub`. **Status: confirm with Adam (legacy owlzone.trade? keep / archive).**
- **Document Ops Pilot - Standard €1,500 one-off** and **Operations Monthly Starter €250/mo** — in `INFRA.md §15.3` but not linked on the site. Reconcile.

---

## 6. Decisions — RESOLVED (Adam, 2026-05-15)

- **D1 (Websites): CHARGE FULL ALL-IN.** Stripe must charge Starter **€695** and Pro **€1,595** in full at checkout (one-off). The €348/€798 deposit prices are wrong. New prices + new payment links + website href update + deactivate old deposit links.
- **D2 (Doc Ops): canonical ladder = Entry €500 / Standard €1,500 / Auto €99 / Auto Plus €249 / Rescue+Export €499 / Bespoke €1,500.** All advertised + firm (full `#pricing` section on site). On-site links already correctly priced. Action: archive the **duplicate non-site generations** (`Document Ops — Self-serve Auto/Auto Plus/Rescue` IM0c/d/e). Keep Operations Monthly €250 (grandfathered). Optionally rename Stripe products to match site tier names/caps.
- **D3 (Receptionist): CREATE FULL LINK SET.** Live payment links for Starter €149/mo, Professional €249/mo, Growth €397/mo + setup €297 (Starter/Pro) + setup €497 (Growth). Create missing Starter €149/mo price + Growth €497 setup price.
- **D4 (Cleanup): ARCHIVE OBSOLETE ONLY.** Archive superseded 4-tier ladder (`CallMeIE · Starter/Growth/Pro/Concierge €99/299/699/1500`, products `prod_UQow…` + their prices) + duplicate Doc Ops links. **Keep** `Owl Studio · Essential/Growth/Concierge care plan` links untouched (legacy owlzone — confirm separately later).

---

## 7. Executed reconciliation (2026-05-15)

Per Adam's D1–D4. New canonical Stripe objects (live mode):

| Item | Price ID | Payment link |
|---|---|---|
| Websites Starter €695 all-in | `price_1TXOiTCEqG2AuI1zZzAGb44I` | `buy.stripe.com/cNi14mbCH2mq93ube5aIM0k` |
| Websites Pro €1,595 all-in | `price_1TXOiTCEqG2AuI1zlW1NC8ZK` | `buy.stripe.com/5kQ28q6in8KO7ZqgypaIM0l` |
| Receptionist Starter €149/mo | `price_1TXOiVCEqG2AuI1zGWDwhEJG` | `buy.stripe.com/3cI7sK36b4uy6Vm81TaIM0m` |
| Receptionist Professional €249/mo | `price_1TVxwgCEqG2AuI1zPZzlP7q3` | `buy.stripe.com/dRmaEW5ej8KO2F6dmdaIM0n` |
| Receptionist Growth €397/mo | `price_1TVxwgCEqG2AuI1zzsCH9YG1` | `buy.stripe.com/eVqbJ0dKP7GKcfGci9aIM0o` |
| Receptionist Setup €297 | `price_1TVxwgCEqG2AuI1zkxlRvISY` | `buy.stripe.com/14A9AS6in6CGenO0zraIM0j` |
| Receptionist Growth Setup €497 | `price_1TXOiVCEqG2AuI1zF7raG6Gr` | `buy.stripe.com/00waEWbCH5yCgvW6XPaIM0p` |

**Done:** new prices+links created · website HTML (`callmeie-hub/websites/index.html`) Starter+Pro hrefs updated · obsolete 4-tier ladder products archived (`prod_UQowfvaBTt487e/GVxn6bpMeg/BGv7jwRdpb/zW8jkmW9kX`) · 3 duplicate DocOps "Self-serve" links deactivated · `routes/.env` + `INFRA.md §15.3` updated.

**Pending Adam (gated — affects live callmeie.ie / live customers):**
1. Commit + push `callmeie-hub/` so callmeie.ie serves the new website links.
2. **After** deploy confirmed live: deactivate old deposit links `cNicN40Y31imenO2HzaIM07` (€348) + `bJe28qeOTf9c5RidmdaIM08` (€798). Not before — would dead-button the live site.
3. Optional: rename Stripe DocOps products to match site tier names/caps (Auto/Auto Plus/Rescue vs current "Doc Ops Starter (60)/Core (250)/Rescue (500)"); prices already correct.
4. Website/Local-SEO copy passes (receptionist minutes-vs-calls unit; Local SEO 3-tier table) — copy only, no Stripe.

## 8. Change log

- 2026-05-15 — file created; canonical extracted from live site; Stripe audited (21 prod/40 price/20 links). D1–D4 resolved by Adam. Reconciliation executed (websites full all-in, receptionist link set, obsolete ladder archived, duplicate DocOps links off). Old website deposit links left active pending site redeploy.
