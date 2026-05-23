"""Twilio account audit — REST API sweep for CallMeIE improvement opportunities.

Hits high-value endpoints in parallel via stdlib + writes a consolidated
markdown report. NO login required (uses TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN
already in ~/.claude/routes/.env).

Run: python scripts/twilio-audit.py [--out PATH]
Default out: ./TWILIO-AUDIT-{utc-date}.md

What it pulls:
  - Account info + balance
  - Last 24h + last 7d usage records (calls/min/SMS/numbers cost)
  - All IncomingPhoneNumbers + their webhook config + capabilities
  - Messaging Services (SMS routing pools, A2P registration)
  - TrustHub Customer Profiles + Trust Products (regulatory bundles)
  - Last 50 Calls + 50 Messages + 100 Alerts/errors
  - Geo Permissions (international voice — abuse vector)
  - Sub-accounts (if any)
  - API Keys count
  - TwiML Applications
  - Recordings count + storage estimate
  - Verify Services (2FA/OTP)
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ENV_PATH = pathlib.Path.home() / ".claude" / "routes" / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
if not SID or not TOKEN:
    print("FAIL: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing", file=sys.stderr)
    sys.exit(2)

AUTH = b64encode(f"{SID}:{TOKEN}".encode()).decode()
UA = "Mozilla/5.0 callmeie-twilio-audit/1.0"


def get(url, params=None):
    """Generic Twilio GET. Returns (status, parsed_json_or_text)."""
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {AUTH}",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path,
                    default=pathlib.Path(f"TWILIO-AUDIT-{dt.datetime.utcnow().date().isoformat()}.md"))
    args = ap.parse_args()

    findings = {}

    # 1. Account + balance
    findings["account"] = get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}.json")
    findings["balance"] = get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Balance.json")

    # 2. Usage — last 7 days
    findings["usage_today"] = get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Usage/Records/Today.json")
    findings["usage_last7"] = get(
        f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Usage/Records.json",
        {"StartDate": (dt.datetime.utcnow() - dt.timedelta(days=7)).date().isoformat()},
    )

    # 3. Numbers + their webhooks
    findings["numbers"] = get(
        f"https://api.twilio.com/2010-04-01/Accounts/{SID}/IncomingPhoneNumbers.json",
        {"PageSize": "100"},
    )

    # 4. Messaging Services
    findings["messaging_services"] = get("https://messaging.twilio.com/v1/Services", {"PageSize": "50"})

    # 5. TrustHub — regulatory bundles + business verification
    findings["customer_profiles"] = get("https://trusthub.twilio.com/v1/CustomerProfiles", {"PageSize": "50"})
    findings["trust_products"] = get("https://trusthub.twilio.com/v1/TrustProducts", {"PageSize": "50"})

    # 6. Recent calls + messages
    findings["recent_calls"] = get(
        f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json",
        {"PageSize": "50"},
    )
    findings["recent_messages"] = get(
        f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json",
        {"PageSize": "50"},
    )

    # 7. Alerts / errors
    findings["alerts"] = get(
        "https://monitor.twilio.com/v1/Alerts",
        {"PageSize": "50"},
    )

    # 8. Geo permissions — international abuse vector
    findings["voice_geo"] = get(f"https://voice.twilio.com/v1/DialingPermissions/Countries", {"PageSize": "10"})

    # 9. Sub-accounts
    findings["subaccounts"] = get("https://api.twilio.com/2010-04-01/Accounts.json", {"PageSize": "50"})

    # 10. API Keys
    findings["api_keys"] = get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Keys.json")

    # 11. TwiML Applications
    findings["twiml_apps"] = get(f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Applications.json")

    # 12. Recordings count
    findings["recordings"] = get(
        f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Recordings.json",
        {"PageSize": "1"},
    )

    # 13. Verify services
    findings["verify_services"] = get("https://verify.twilio.com/v2/Services", {"PageSize": "20"})

    # 14. SIP Trunking
    findings["trunks"] = get("https://trunking.twilio.com/v1/Trunks", {"PageSize": "20"})

    # 15. Notify services
    findings["notify_services"] = get("https://notify.twilio.com/v1/Services", {"PageSize": "20"})

    # 16. Conversations (Studio Flow / IVR alternative)
    findings["conversations"] = get("https://conversations.twilio.com/v1/Services", {"PageSize": "20"})

    # 17. Studio flows
    findings["studio_flows"] = get("https://studio.twilio.com/v2/Flows", {"PageSize": "20"})

    # 18. Functions / Serverless
    findings["serverless"] = get("https://serverless.twilio.com/v1/Services", {"PageSize": "20"})

    # ---- Render markdown report
    out = []
    now = dt.datetime.utcnow().isoformat() + "Z"
    out.append(f"# Twilio Account Audit — {now}\n")
    out.append(f"**Account SID:** `{SID}`\n\n")

    # Account section
    s, acc = findings["account"]
    if s == 200 and isinstance(acc, dict):
        out.append("## Account\n")
        out.append(f"- Friendly name: `{acc.get('friendly_name')}`")
        out.append(f"- Status: **{acc.get('status')}**")
        out.append(f"- Type: **{acc.get('type')}** (`Full` = paid / `Trial` = free)")
        out.append(f"- Date created: {acc.get('date_created')}")
        out.append(f"- Owner SID: `{acc.get('owner_account_sid')}`\n")
    else:
        out.append(f"## Account — HTTP {s}\n```\n{acc}\n```\n")

    # Balance
    s, bal = findings["balance"]
    if s == 200 and isinstance(bal, dict):
        out.append("## Balance\n")
        out.append(f"- Balance: **{bal.get('balance')} {bal.get('currency')}**\n")
    else:
        out.append(f"## Balance — HTTP {s}\n```\n{bal}\n```\n")

    # Usage today
    s, usg = findings["usage_today"]
    if s == 200 and isinstance(usg, dict):
        records = usg.get("usage_records", []) or []
        out.append(f"## Usage — TODAY ({len(records)} categories)\n")
        if records:
            out.append("| Category | Count | Usage | Price |\n|---|---|---|---|")
            for r in records:
                if int(r.get("count") or 0) > 0 or float(r.get("price") or 0) > 0:
                    out.append(f"| {r.get('category')} | {r.get('count')} {r.get('count_unit') or ''} | {r.get('usage')} {r.get('usage_unit') or ''} | {r.get('price')} {r.get('price_unit') or ''} |")
        out.append("")
    s, usg = findings["usage_last7"]
    if s == 200 and isinstance(usg, dict):
        records = usg.get("usage_records", []) or []
        # filter to non-zero
        nz = [r for r in records if (float(r.get("price") or 0) > 0 or int(r.get("count") or 0) > 0)]
        out.append(f"## Usage — LAST 7 DAYS ({len(nz)} non-zero categories)\n")
        if nz:
            out.append("| Category | Count | Usage | Price |\n|---|---|---|---|")
            for r in nz[:40]:
                out.append(f"| {r.get('category')} | {r.get('count')} {r.get('count_unit') or ''} | {r.get('usage')} {r.get('usage_unit') or ''} | {r.get('price')} {r.get('price_unit') or ''} |")
        out.append("")

    # Numbers
    s, nums = findings["numbers"]
    if s == 200 and isinstance(nums, dict):
        items = nums.get("incoming_phone_numbers", []) or []
        out.append(f"## Owned Numbers ({len(items)})\n")
        if items:
            out.append("| Phone | Friendly | Voice URL | SMS URL | Status Callback | Caps | SID |\n|---|---|---|---|---|---|---|")
            for n in items:
                caps = n.get("capabilities", {})
                cap_str = "".join([("V" if caps.get("voice") else "."), ("S" if caps.get("sms") else "."), ("M" if caps.get("mms") else "."), ("F" if caps.get("fax") else ".")])
                vu = (n.get("voice_url") or "")[:40] + ("…" if len(n.get("voice_url") or "") > 40 else "")
                su = (n.get("sms_url") or "")[:40] + ("…" if len(n.get("sms_url") or "") > 40 else "")
                sc = (n.get("status_callback") or "")[:30] + ("…" if len(n.get("status_callback") or "") > 30 else "")
                out.append(f"| `{n.get('phone_number')}` | {n.get('friendly_name')} | `{vu or '—'}` | `{su or '—'}` | `{sc or '—'}` | {cap_str} | `{n.get('sid')}` |")
        out.append("")

    # Messaging Services
    s, ms = findings["messaging_services"]
    if s == 200 and isinstance(ms, dict):
        items = ms.get("services", []) or []
        out.append(f"## Messaging Services ({len(items)})\n")
        for item in items:
            out.append(f"### {item.get('friendly_name')} (`{item.get('sid')}`)")
            out.append(f"- Inbound request URL: `{item.get('inbound_request_url') or '—'}`")
            out.append(f"- Status callback: `{item.get('status_callback') or '—'}`")
            out.append(f"- Use case: {item.get('usecase') or '—'}")
            out.append(f"- US A2P campaign registered: **{item.get('us_app_to_person_registered')}**")
            out.append(f"- Sticky sender: {item.get('sticky_sender')}, smart encoding: {item.get('smart_encoding')}")
            out.append("")
        if not items:
            out.append("_None._\n")

    # TrustHub — Customer Profiles + Trust Products
    s, cp = findings["customer_profiles"]
    if s == 200 and isinstance(cp, dict):
        items = cp.get("results", []) or []
        out.append(f"## TrustHub — Customer Profiles ({len(items)})\n")
        for item in items:
            out.append(f"- `{item.get('sid')}` — {item.get('friendly_name')} — **{item.get('status')}** — policy: {item.get('policy_sid')}")
        if not items:
            out.append("_None._")
        out.append("")
    s, tp = findings["trust_products"]
    if s == 200 and isinstance(tp, dict):
        items = tp.get("results", []) or []
        out.append(f"## TrustHub — Trust Products / Regulatory Bundles ({len(items)})\n")
        for item in items:
            out.append(f"- `{item.get('sid')}` — {item.get('friendly_name')} — **{item.get('status')}** — policy: `{item.get('policy_sid')}`")
        if not items:
            out.append("_None._")
        out.append("")

    # Recent Calls
    s, rc = findings["recent_calls"]
    if s == 200 and isinstance(rc, dict):
        items = rc.get("calls", []) or []
        out.append(f"## Recent Calls (last {len(items)})\n")
        if items:
            out.append("| Date | From | To | Direction | Status | Dur | Price |\n|---|---|---|---|---|---|---|")
            for c in items[:25]:
                out.append(f"| {(c.get('date_created') or '')[:25]} | `{c.get('from')}` | `{c.get('to')}` | {c.get('direction')} | {c.get('status')} | {c.get('duration')}s | {c.get('price')} {c.get('price_unit') or ''} |")
        out.append("")

    # Recent Messages
    s, rm = findings["recent_messages"]
    if s == 200 and isinstance(rm, dict):
        items = rm.get("messages", []) or []
        out.append(f"## Recent Messages (last {len(items)})\n")
        if items:
            out.append("| Date | From | To | Status | Body (truncated) | Price |\n|---|---|---|---|---|---|")
            for m in items[:25]:
                body = (m.get("body") or "").replace("\n", " ").replace("|", "/")[:60]
                out.append(f"| {(m.get('date_created') or '')[:25]} | `{m.get('from')}` | `{m.get('to')}` | {m.get('status')} | {body} | {m.get('price')} |")
        out.append("")

    # Alerts
    s, al = findings["alerts"]
    if s == 200 and isinstance(al, dict):
        items = al.get("alerts", []) or []
        # group by error_code
        from collections import Counter
        codes = Counter(it.get("error_code") for it in items)
        out.append(f"## Recent Alerts / Errors ({len(items)} total)\n")
        if codes:
            out.append("**Top error codes (count):**\n")
            for code, n in codes.most_common(20):
                out.append(f"- `{code}` × {n} — https://www.twilio.com/docs/errors/{code}")
            out.append("\n**5 most recent alerts (detail):**\n")
            for it in items[:5]:
                out.append(f"- `{it.get('date_created')}` — `{it.get('error_code')}` — `{it.get('resource_sid') or '—'}` — {(it.get('alert_text') or '')[:120]}")
        out.append("")

    # Geo Permissions
    s, gp = findings["voice_geo"]
    if s == 200 and isinstance(gp, dict):
        items = gp.get("content", []) or []
        # We only have first page; show counts
        out.append(f"## International Voice — DialingPermissions (first page = {len(items)} countries)\n")
        out.append("_NOTE: this endpoint paginates; sampling top countries only. Full audit needs walking all pages._\n")
        if items:
            blocked = [c for c in items if not c.get("low_risk_numbers_enabled") and not c.get("high_risk_special_numbers_enabled") and not c.get("high_risk_tollfraud_numbers_enabled")]
            enabled = [c for c in items if c.get("low_risk_numbers_enabled") or c.get("high_risk_special_numbers_enabled") or c.get("high_risk_tollfraud_numbers_enabled")]
            out.append(f"- Sampled enabled: {len(enabled)} (allow some intl dialing)")
            out.append(f"- Sampled blocked: {len(blocked)}")
            out.append(f"- Examples enabled: {', '.join(c.get('iso_code') for c in enabled[:10])}")
        out.append("")

    # Sub-accounts
    s, sa = findings["subaccounts"]
    if s == 200 and isinstance(sa, dict):
        items = [a for a in (sa.get("accounts", []) or []) if a.get("sid") != SID]
        out.append(f"## Sub-Accounts ({len(items)})\n")
        for a in items:
            out.append(f"- `{a.get('sid')}` — {a.get('friendly_name')} — {a.get('status')}")
        if not items:
            out.append("_None._")
        out.append("")

    # API Keys
    s, ak = findings["api_keys"]
    if s == 200 and isinstance(ak, dict):
        items = ak.get("keys", []) or []
        out.append(f"## API Keys ({len(items)})\n")
        for k in items:
            out.append(f"- `{k.get('sid')}` — {k.get('friendly_name')} — created {k.get('date_created')}")
        if not items:
            out.append("_None — using AUTH_TOKEN only (revocation = rotate entire account)._")
        out.append("")

    # TwiML Apps
    s, ta = findings["twiml_apps"]
    if s == 200 and isinstance(ta, dict):
        items = ta.get("applications", []) or []
        out.append(f"## TwiML Applications ({len(items)})\n")
        for a in items:
            out.append(f"- `{a.get('sid')}` — {a.get('friendly_name')} — voice: `{a.get('voice_url')}` sms: `{a.get('sms_url')}`")
        if not items:
            out.append("_None._")
        out.append("")

    # Recordings
    s, rec = findings["recordings"]
    if s == 200 and isinstance(rec, dict):
        # If page_size=1 returned 1, more exist; we only check presence
        items = rec.get("recordings", []) or []
        out.append(f"## Recordings (presence check, page 1)\n")
        out.append(f"- First page: {len(items)} recording(s) shown of unknown total. Storage costs $0.0025/min/mo per recording.\n")

    # Verify
    s, vs = findings["verify_services"]
    if s == 200 and isinstance(vs, dict):
        items = vs.get("services", []) or []
        out.append(f"## Verify Services ({len(items)})\n")
        for v in items:
            out.append(f"- `{v.get('sid')}` — {v.get('friendly_name')}")
        if not items:
            out.append("_None._")
        out.append("")

    # SIP Trunks
    s, tr = findings["trunks"]
    if s == 200 and isinstance(tr, dict):
        items = tr.get("trunks", []) or []
        out.append(f"## SIP Trunks ({len(items)})\n")
        for t in items:
            out.append(f"- `{t.get('sid')}` — {t.get('friendly_name')} — domain: {t.get('domain_name')}")
        if not items:
            out.append("_None._")
        out.append("")

    # Notify
    s, ns = findings["notify_services"]
    if s == 200 and isinstance(ns, dict):
        items = ns.get("services", []) or []
        out.append(f"## Notify Services ({len(items)})\n")
        if not items:
            out.append("_None._")
        out.append("")

    # Conversations
    s, cs = findings["conversations"]
    if s == 200 and isinstance(cs, dict):
        items = cs.get("services", []) or []
        out.append(f"## Conversations Services ({len(items)})\n")
        if not items:
            out.append("_None._")
        out.append("")

    # Studio
    s, sf = findings["studio_flows"]
    if s == 200 and isinstance(sf, dict):
        items = sf.get("flows", []) or []
        out.append(f"## Studio Flows ({len(items)})\n")
        for f in items:
            out.append(f"- `{f.get('sid')}` — {f.get('friendly_name')} — status: {f.get('status')}")
        if not items:
            out.append("_None._")
        out.append("")

    # Serverless
    s, sr = findings["serverless"]
    if s == 200 and isinstance(sr, dict):
        items = sr.get("services", []) or []
        out.append(f"## Serverless Functions ({len(items)})\n")
        if not items:
            out.append("_None._")
        out.append("")

    text = "\n".join(out)
    args.out.write_text(text, encoding="utf-8")
    print(f"WROTE {args.out} ({len(text)} bytes)")
    print(f"\n--- preview ---\n{text[:2000]}\n--- /preview ---")


if __name__ == "__main__":
    main()
