# CallMeIE / Owl Studio — Pricing Single Source of Truth

**Status:** `canonical` · created 2026-05-15
**Authority:** This file is the canonical pricing reference for all CallMeIE / Owl Studio services. It is derived from the **live website** (`callmeie.ie`, source `callmeie-hub/`), which is what we actually advertise to customers. Stripe products, payment links, `~/.claude/routes/.env`, and `INFRA.md §15` MUST match this file. Where they differ, this file wins and Stripe is corrected to it.

> Supersedes for *advertised* pricing: `Next Revenue Ideas/document-ops/PRICING.md` (that file is a `draft` research/anchoring doc — internal value-based pricing logic, NOT what we advertise). PRICING.md numbers (Entry Pilot €750, Operations €750-1,500/mo) do **not** match the site and must not be quoted to customers.

Mirror rule (inherited from INFRA.md): on any pricing change, update this file FIRST, then mirror to `callmeie-fix/PRICING-SSOT.md`, then reconcile Stripe, then update `INFRA.md §15.3` and `~/.claude/routes/.env` in the same commit.

---

## 1. AI-First Websites

**LEAD offer (primary, pitched first-and-foremost): managed website — build + maintain + host, recurring, €0 onboarding.** Source code NOT handed over (managed service). Per-tier minimum term, then month-to-month, cancel anytime. Source: `callmeie-hub/websites/index.html` §3 `#pricing` — to be repositioned so this ladder is the first/primary card (see §7 D5 pending).

| Tier | Recurring | Min term | Min-term value | Key inclusions | On-site CTA link |
|---|---|---|---|---|---|
| **Launch** | **€69/mo** | 12-mo | €828 | 1-page build, mobile/tablet/desktop, contact form, hosting + SSL + GDPR, **30 min/mo edits, 2-biz-day SLA** | `buy.stripe.com/dRm00i6in7GK5Ri6XPaIM0s` (`price_1TYuVGCEqG2AuI1zqvAc1w2B`) |
| **Business** *(most popular)* | **€100/mo** | 12-mo | €1,200 | up-to-5-page build, bespoke design, CMS, copywriting, GBP, basic SEO, hosting + backups, **2 hr/mo edits + 1 new page/quarter, 1-biz-day SLA** | `buy.stripe.com/00w7sK4af2mq5Ribe5aIM0t` (`price_1TYuVHCEqG2AuI1zDn31aEM1`) |
| **Premium** | **€149/mo** | 18-mo | €2,682 | bespoke/multi-page build, integrations, hosting + monitoring, **fair-use edits (6hr/mo rolling 90-day avg, 8hr ceiling) + quarterly refresh, same-day SLA** | `buy.stripe.com/9B68wOayD3quenO1DvaIM0u` (`price_1TYuVHCEqG2AuI1zSmCBhi0f`) |

- **Onboarding fee €0 — all tiers.** Adam decision 2026-05-19 (D5): model default, lowest barrier / easiest first sale.
- After minimum term → month-to-month, cancel anytime. Premium 18-mo: larger fronted build + €0 onboarding ∴ contract term = build-cost recovery.
- **Scope/boundary canonical = `callmeie-hub/websites/managed-plans-scope.md`; contract = `_internal/order-forms/order-form-template.md` §B1b.** Out-of-scope work quoted €60/hr (€55 Premium), never billed silently — same proven rule as Care plans (`care-plans-scope.md`). "unlimited" is a defined fair-use soft-cap, NOT literally unlimited. Site `#pricing` + ad copy derive FROM the scope doc (single-source; D5 clauses locked 2026-05-19: managed = source retained, want-to-own → one-off offer; early exit before min term → remaining-term fees payable, no separate kill fee).

**ALTERNATIVE offer (secondary, shown below the ladder — not removed): own it outright, one-off, no lock-in, source code yours.**

| Tier | Advertised price | Timeline | Key inclusions | On-site CTA link |
|---|---|---|---|---|
| **Starter** | **€695** all-in | 7 days | 1 long landing page, 150–300 words + 6 photos, mobile/tablet/desktop, contact form, 1yr Cloudflare Pages hosting + SSL + GDPR, 1 revision round | full-price `cNi14m…0k` (§7; old deposit `cNicN40Y31imenO2HzaIM07` retired on redeploy) |
| **Pro** | **€1,595** all-in | 14 days | 5-page site, bespoke design, Decap CMS, copywriting (home+services+about), GBP setup, basic SEO, 2yr hosting + backups, 2 revision rounds | full-price `5kQ28q…0l` (§7; old deposit `bJe28qeOTf9c5RidmdaIM08` retired on redeploy) |
| **Custom** | **from €2,950** quoted | 4–6 wks | E-commerce / SaaS / multi-language / bespoke integrations. Scoped via €99 audit first | `bJe6oGbCH7GK0wY4PHaIM00` (the €99 audit) |

**Add-ons / entry points (websites):**
- **Copywriting add-on (Starter one-off):** **+€150** (Starter fine print).
- **€99 style-match scoping audit:** one-off €99, **credited against the final Custom invoice if booked within 30 days**. *Custom*-tier entry point. **The €99 is an audit, NOT a website price — we do not sell websites "from €99".**
- **Maintenance-only (clients who already own a site):** separate advertised add-on — see §4a. Distinct from the recurring build+maintain ladder above; do NOT conflate in copy.

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

## 4a. Website Maintenance-Only (legacy care plans — clients who already own a site)

**Resolved 2026-05-19 (D5):** legacy Owl Studio care plans are **kept as a separate, advertised maintenance-only add-on** — NOT folded into the §1 recurring build+maintain ladder, NOT archived. Target = clients who already have a site and only want hosting/upkeep; §1 ladder = clients who need the site built too. Keep the two distinct in all copy (no cross-contamination).

| Tier | Price | For |
|---|---|---|
| Essential | €45/mo | hosting + uptime + minor fixes |
| Growth | €95/mo | + content edits + monthly check |
| Concierge | €195/mo | + priority + proactive improvements |

6 existing Stripe links stay **active** (no archive). Surfacing them on-site = a website copy pass (not a Stripe mutation). Inclusions above are indicative — confirm against the live care-plan link descriptions during the site copy pass before advertising exact scope.

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
| **Websites Launch (recurring)** | **€69/mo, 12-mo min** | `price_1TYuVGCEqG2AuI1zqvAc1w2B` | ✅ **created live 2026-05-19** — link `buy.stripe.com/dRm00i6in7GK5Ri6XPaIM0s` |
| **Websites Business (recurring)** | **€100/mo, 12-mo min** | `price_1TYuVHCEqG2AuI1zDn31aEM1` | ✅ **created live 2026-05-19** — link `buy.stripe.com/00w7sK4af2mq5Ribe5aIM0t` |
| **Websites Premium (recurring)** | **€149/mo, 18-mo min** | `price_1TYuVHCEqG2AuI1zSmCBhi0f` | ✅ **created live 2026-05-19** — link `buy.stripe.com/9B68wOayD3quenO1DvaIM0u` |

### Orphans / duplicates active in Stripe (NOT on the live website)

- **Obsolete 4-tier receptionist ladder (AUD-021, 2026-04-30):** `CallMeIE · Starter €99/mo`, `Growth €299/mo`, `Pro €699/mo`, `Concierge €1,500/mo` (+ yearly + overage = 12 prices, products `prod_UQow…`). Superseded by current €149/€249/€397 model. **Candidate: archive.**
- **Doc Ops — triple-stacked ladders.** Three parallel generations all active: (a) Pilot Entry/Standard + Operations Monthly Starter €250/mo (2026-05-01); (b) "Document Ops — Self-serve Auto/Auto Plus/Rescue" (D22, 2026-05-04); (c) "Doc Ops Starter/Core/Rescue/Bespoke" (newest). ~10 active doc-ops links for 1 advertised product. **Candidate: keep one ladder, archive the rest.**
- **Owl Studio care plans** Essential €45/mo · Growth €95/mo · Concierge €195/mo (+ yearly) — 6 active links. **RESOLVED 2026-05-19 (D5): keep active, reposition as advertised maintenance-only add-on — see §4a. NOT archived, NOT folded into §1 recurring ladder.**
- **Document Ops Pilot - Standard €1,500 one-off** and **Operations Monthly Starter €250/mo** — in `INFRA.md §15.3` but not linked on the site. Reconcile.

---

## 6. Decisions — RESOLVED (Adam, 2026-05-15)

- **D1 (Websites): CHARGE FULL ALL-IN.** Stripe must charge Starter **€695** and Pro **€1,595** in full at checkout (one-off). The €348/€798 deposit prices are wrong. New prices + new payment links + website href update + deactivate old deposit links.
- **D2 (Doc Ops): canonical ladder = Entry €500 / Standard €1,500 / Auto €99 / Auto Plus €249 / Rescue+Export €499 / Bespoke €1,500.** All advertised + firm (full `#pricing` section on site). On-site links already correctly priced. Action: archive the **duplicate non-site generations** (`Document Ops — Self-serve Auto/Auto Plus/Rescue` IM0c/d/e). Keep Operations Monthly €250 (grandfathered). Optionally rename Stripe products to match site tier names/caps.
- **D3 (Receptionist): CREATE FULL LINK SET.** Live payment links for Starter €149/mo, Professional €249/mo, Growth €397/mo + setup €297 (Starter/Pro) + setup €497 (Growth). Create missing Starter €149/mo price + Growth €497 setup price.
- **D4 (Cleanup): ARCHIVE OBSOLETE ONLY.** Archive superseded 4-tier ladder (`CallMeIE · Starter/Growth/Pro/Concierge €99/299/699/1500`, products `prod_UQow…` + their prices) + duplicate Doc Ops links. **Keep** `Owl Studio · Essential/Growth/Concierge care plan` links untouched (legacy owlzone — confirm separately later).

## 6a. Decisions — RESOLVED (Adam, 2026-05-19)

- **D5 (Websites): LEAD WITH A RECURRING BUILD+MAINTAIN LADDER.** The website offer pitched first-and-foremost is now a 3-tier recurring managed-website bundle (build + maintain + host, source code retained): **Launch €69/mo (12-mo min) / Business €100/mo (12-mo min, most popular) / Premium €149/mo (18-mo min)**, **€0 onboarding all tiers**. The existing one-off ladder (Starter €695 / Pro €1,595 / Custom from €2,950, full-price §7 links) is **repositioned as the secondary "own it outright, no lock-in" alternative** shown below the ladder — NOT removed. Legacy Owl Studio care plans (€45/€95/€195) **kept as a separate advertised maintenance-only add-on** (§4a) — NOT folded, NOT archived. Onboarding €0 chosen (model default, lowest barrier). Premium 18-mo min: larger fronted build + €0 onboarding ∴ contract term is the build-cost recovery lever. Spec pressed-back + approved by Adam in-session (12/12/18 confirmed). Execution = §7 D5 pending (gated — live Stripe + live site).
- **D5.1 (Scope & contract clauses) — RESOLVED + BUILT (Adam "go", 2026-05-19).** Anti-scope-creep handled by REUSING the existing proven formula (not a new doc): `care-plans-scope.md` "simple rule" (existing-content edits = included to tier budget; new work = quoted €60/hr after `do it`; never billed silently) + per-tier time budgets + the proven fair-use soft-cap. New canonical artifacts created (Phase A.5, reversible, NOT live): `callmeie-hub/websites/managed-plans-scope.md` (D5 customer-facing scope, reuses Care rule by reference) + `order-form-template.md` §B1b (Managed engagement type). Clause levers locked at proposed defaults: **early termination before min term = remaining minimum-term fees payable** (no kill fee — €0 deposit); **managed IP = source retained for term, NOT transferred; want-to-own → take the one-off Build offer, no separate buy-out** (§B1b OVERRIDES order-form §C2/§F2/§H1/§H2 for Managed). Premium "unlimited reasonable edits" wording trap FIXED in §1 → proven 6hr-rolling/8hr-ceiling fair-use cap. Single-source: contract + site `#pricing` + ad copy all derive from `managed-plans-scope.md`. Order-form stays solicitor-review-pending (its existing status; pre-revenue/Ltd — boundary unchanged).

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

### Pending Adam (gated) — D5 website recurring bundle (2026-05-19)

Decision locked (Adam, 2026-05-19, in-session — spec pressed-back + approved). **Phase A** (reversible, internal): this file (§1/§4a/§5/§6a/§8) + `callmeie-fix/PRICING-SSOT.md` mirror — DONE. **Phase A.5** (reversible, internal — D5.1 clauses): `callmeie-hub/websites/managed-plans-scope.md` + `order-form-template.md` §B1b — DONE. Phase B BELOW = live mutations, gated — execute only on explicit Adam "go", then assemble one commit, hold for push confirm:

1. **Stripe (live mode) — create 3 recurring prices + 1 payment link each:**
   - `Websites Launch` — €69.00/mo recurring EUR.
   - `Websites Business` — €100.00/mo recurring EUR.
   - `Websites Premium` — €149.00/mo recurring EUR.
   - €0 onboarding ∴ NO setup price needed. Min term (12/12/18 mo) enforced **contractually in T&Cs with a plain monthly price** (simplest; Stripe subscription-schedule minimum-term is the heavier alternative — decide at execution, default = plain monthly + T&C term).
2. **Fill returned IDs back into:** this file §1 + §5 (replace `*(Stripe pending — §7 D5)*`), `callmeie-fix` mirror, `INFRA.md §15.3`, `~/.claude/routes/.env`.
3. **Live site `callmeie-hub/websites/index.html` `#pricing`:** restructure so the 3-tier recurring ladder is the FIRST/primary card block, pitched first-and-foremost; one-off ladder moves below as "Own it outright — no lock-in, source yours"; add the §4a maintenance-only care-plan block as a clearly-separate add-on (mixed into neither ladder). **Card bullets + the website-offer ad copy derive verbatim FROM `managed-plans-scope.md` (single-source — do not invent scope wording in the HTML/ad); include the visible "Bigger changes? Quoted, never billed silently" boundary line so scope is shown, not hidden.** Also render `managed-plans-scope.md` at `/websites/managed-plans-scope` (sibling to the existing `/websites/care-plans-scope`).
4. **One commit** (this file + mirror + INFRA + .env + site), Adam-gated — do NOT push without explicit go. New prices are net-new ∴ no old links dead-buttoned by this change (unlike the 2026-05-15 deposit-link case).
5. Sequencing note: the still-pending 2026-05-15 one-off redeploy (§7 items 1–2 above this subsection) and this D5 site change touch the SAME file (`callmeie-hub/websites/index.html`) — do them as ONE site edit + ONE commit when Adam greenlights, not two.

### Phase B execution log — D5 (2026-05-19, Adam "run now")

**Step 1 (Stripe live) — DONE.** 3 recurring prices + payment links created via the proven idempotent `provision-stripe.py` (CATALOGUE extended with 3 managed products; existing objects = [exists] no-op):

| Tier | Product | Price ID | Payment link |
|---|---|---|---|
| Launch €69/mo | `prod_UY0WJnpT1lVlpl` | `price_1TYuVGCEqG2AuI1zqvAc1w2B` | `buy.stripe.com/dRm00i6in7GK5Ri6XPaIM0s` |
| Business €100/mo | `prod_UY0WeCmuhtz6ij` | `price_1TYuVHCEqG2AuI1zDn31aEM1` | `buy.stripe.com/00w7sK4af2mq5Ribe5aIM0t` |
| Premium €149/mo | `prod_UY0WxEShnZFU6k` | `price_1TYuVHCEqG2AuI1zSmCBhi0f` | `buy.stripe.com/9B68wOayD3quenO1DvaIM0u` |

Plain monthly recurring EUR, no setup price (€0 onboarding). 12/12/18 min term = contractual (order-form §B1b), NOT a Stripe schedule.

**Side-effects of re-running the broad script (caught + corrected same run):**
- It re-created the OLD €348/€798 deposit payment links (stale CATALOGUE entries pre-D1). Both **deactivated** immediately (`plink_1TYuVL…1OMdyeHe` starter, `plink_1TYuVL…KPIaGtzn` pro; Stripe `active=false`, 200). Not on site → zero customer impact.
- It re-created + enabled a stray webhook at the migrated-away `callmeie.onrender.com` URL (`we_1TYuVOCEqG2AuI1zMnvxj6XW`). **Disabled** (status=disabled confirmed). Canonical live webhook `we_1TVCAzCEqG2AuI1zI2qB6OM5` (`api.callmeie.ie`, enabled, 7 events) was never touched — payments unaffected; the new one-time secret is moot (endpoint disabled). `provision-stripe.py` `--webhook-url` default fixed to `api.callmeie.ie` so a future re-run matches the canonical endpoint ([exists], no churn) — recurrence prevented at source.

**Step 2 (ID fill) — DONE in this same pass:** this file §1 + §5 (above) + `callmeie-fix` mirror + `INFRA.md §15.3` + `~/.claude/routes/.env`.
**Step 3 (site `#pricing` restructure) + Step 4 (one commit) — in this pass; git push HELD for explicit Adam confirm** (live callmeie.ie). Steps 1–2 above-this-subsection (2026-05-15 one-off redeploy) folded into the same site edit + commit.

## 8. Change log

- 2026-05-15 — file created; canonical extracted from live site; Stripe audited (21 prod/40 price/20 links). D1–D4 resolved by Adam. Reconciliation executed (websites full all-in, receptionist link set, obsolete ladder archived, duplicate DocOps links off). Old website deposit links left active pending site redeploy.
- 2026-05-19 — **D5: website offer repositioned to LEAD with a 3-tier recurring build+maintain ladder** (Launch €69/12-mo · Business €100/12-mo · Premium €149/18-mo · €0 onboarding). One-off ladder demoted to secondary "own it outright" alternative. Legacy care plans (€45/€95/€195) resolved → kept as separate advertised maintenance-only add-on (§4a), not folded/archived. §1/§4a/§5/§6a rewritten; callmeie-fix mirror updated. Phase B (Stripe create + site restructure + INFRA/.env ID-fill + one commit) staged in §7 "Pending Adam (gated) — D5" — not yet executed (live-pricing Adam-gate).
- 2026-05-19 — **D5 Phase B EXECUTED (Adam "run now"):** 3 live Stripe recurring prices+links created (Launch `…qvAc1w2B` €69 / Business `…Dn31aEM1` €100 / Premium `…SmCBhi0f` €149) via idempotent `provision-stripe.py`; IDs filled into §1/§5 + mirror + INFRA §15.3 + routes/.env. Side-effects caught+corrected: 2 stale €348/€798 deposit links deactivated; stray onrender webhook disabled (canonical api.callmeie.ie webhook untouched); script default URL fixed. Site `#pricing` restructure + ONE commit in same pass; **git push HELD for explicit Adam confirm** (live callmeie.ie). Detail = §7 "Phase B execution log — D5".
- 2026-05-19 — **D5.1: scope/contract clauses built (Phase A.5).** Research-first found the anti-scope-creep formula already exists (`care-plans-scope.md`); reused it, did not duplicate. Created `callmeie-hub/websites/managed-plans-scope.md` (D5 customer-facing scope) + `order-form-template.md` §B1b (Managed engagement type, OVERRIDES §C2/§F2/§H1/§H2 for managed). Clauses locked: early exit → remaining-min-term fees; managed IP = retained, want-to-own → one-off offer. §1 "unlimited reasonable edits" trap fixed → proven 6hr/8hr fair-use cap; §1 tier cells aligned to scope doc; §6a D5.1 + §7 Phase A.5 recorded. Site `#pricing`/ad copy single-sources from `managed-plans-scope.md` (Phase B). callmeie-fix mirror re-synced.
