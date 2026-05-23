"""Search Twilio IE Local inventory in Limerick + buy 1 DID for pilot.

Adam-authorized one-shot purchase 2026-05-22 to press-back regulatory
clearance. Uses the same IE_ADDRESS_SID / IE_BUNDLE_SID wiring as
build-dunne-demo.py:241-242 (Twilio-approved 2026-04-03).

Run:
  python scripts/buy-pilot-did.py --search-only   # safe, no charge
  python scripts/buy-pilot-did.py --buy            # commits one number

Cost: ~EUR 1.00 setup + EUR 1.00/mo recurring on Twilio account
$TWILIO_ACCOUNT_SID.
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request
import urllib.error
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

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
if not TWILIO_SID or not TWILIO_TOKEN:
    print("FAIL: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing", file=sys.stderr)
    sys.exit(2)

TWILIO_BASE = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}"
IE_ADDRESS_SID = "ADa6af451f3c25f1e5ff44f5b587c62fe7"
IE_BUNDLE_SID = "BUe3a9d5fdaa25fa8065f7dc1607b5551a"
UA = "Mozilla/5.0 callmeie-pilot-did/1.0"


def _auth():
    return b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()


def tw_get(path, params=None):
    url = TWILIO_BASE + path + (".json" if not path.endswith(".json") else "")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {_auth()}",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def tw_post(path, data):
    url = TWILIO_BASE + path + (".json" if not path.endswith(".json") else "")
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Basic {_auth()}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def search_limerick(limit=5):
    print(f"\n[search] IE Local in Limerick (PageSize={limit})...")
    status, data = tw_get("/AvailablePhoneNumbers/IE/Local", {
        "VoiceEnabled": "true",
        "InLocality": "Limerick",
        "PageSize": str(limit),
    })
    if status != 200 or not isinstance(data, dict):
        print(f"  HTTP {status}: {data}")
        return []
    found = data.get("available_phone_numbers", []) or []
    if not found:
        print("  no IE-Limerick Local inventory right now")
    for i, n in enumerate(found):
        print(f"  [{i}] {n.get('phone_number')}  locality={n.get('locality')}  region={n.get('region')}  voice={n.get('capabilities', {}).get('voice')}")
    return found


def buy(phone_number):
    print(f"\n[buy] {phone_number} with AddressSid={IE_ADDRESS_SID} BundleSid={IE_BUNDLE_SID}")
    body = {
        "PhoneNumber": phone_number,
        "FriendlyName": f"CallMeIE Pilot Pool {phone_number}",
        "AddressSid": IE_ADDRESS_SID,
        "BundleSid": IE_BUNDLE_SID,
    }
    status, data = tw_post("/IncomingPhoneNumbers", body)
    if status not in (200, 201) or not isinstance(data, dict):
        print(f"  FAIL HTTP {status}\n  {data}", file=sys.stderr)
        sys.exit(3)
    print(f"  PURCHASED")
    print(f"    SID:           {data.get('sid')}")
    print(f"    Phone Number:  {data.get('phone_number')}")
    print(f"    Friendly Name: {data.get('friendly_name')}")
    print(f"    Date Created:  {data.get('date_created')}")
    print(f"    Capabilities:  {data.get('capabilities')}")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search-only", action="store_true", help="Only search, do not buy")
    ap.add_argument("--buy", action="store_true", help="Buy the first available number")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    found = search_limerick(limit=args.limit)
    if not found:
        sys.exit(1)
    if args.buy:
        pick = found[0]
        print(f"\nPicked first option: {pick.get('phone_number')}")
        buy(pick["phone_number"])
    elif args.search_only:
        print("\n--search-only set; not buying")
    else:
        print("\nNo --buy or --search-only flag; default = search only")


if __name__ == "__main__":
    main()
