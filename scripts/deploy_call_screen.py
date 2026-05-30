#!/usr/bin/env python3
"""Deploy a Twilio Serverless Function that screens inbound demo-line calls.

Denylisted callers -> <Reject>. Everyone else -> <Redirect> to Vapi inbound.
The number's VoiceFallbackUrl is set to the Vapi URL so any Function failure
still routes to Vapi (demo line cannot break from this change).

Idempotent: reuses the service/function/env if already present.
Run:  python deploy_call_screen.py            # deploy + repoint number
      python deploy_call_screen.py --dry      # build+test only, do NOT repoint
"""
import base64, json, sys, time, urllib.parse, urllib.request, urllib.error, mimetypes, uuid

DRY = "--dry" in sys.argv
NUMBER = "+35361788120"
NUMBER_SID = "PNfa6047f8f4dc100a5c64b28638f547e4"
VAPI_INBOUND = "https://api.vapi.ai/twilio/inbound_call"
DENYLIST = "+353852345595"          # comma-separated; edit + redeploy to change
SERVICE_NAME = "callmeie-call-screen"
FN_PATH = "/screen"

FUNCTION_JS = """exports.handler = function (context, event, callback) {
  const deny = (context.DENYLIST || '')
    .split(',').map(function (s) { return s.trim(); }).filter(Boolean);
  const from = (event.From || '').trim();
  const twiml = new Twilio.twiml.VoiceResponse();
  if (deny.indexOf(from) !== -1) {
    twiml.reject({ reason: 'rejected' });
    return callback(null, twiml);
  }
  twiml.redirect({ method: 'POST' }, context.VAPI_INBOUND_URL);
  return callback(null, twiml);
};
"""

env = {}
for line in open(r"C:\Users\a33_s\.claude\routes\.env", encoding="utf-8", errors="ignore"):
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
ASID, ATOK = env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"]
AUTH = base64.b64encode(f"{ASID}:{ATOK}".encode()).decode()


def call(method, url, fields=None, multipart=None):
    headers = {"Authorization": f"Basic {AUTH}", "User-Agent": "callmeie-deploy"}
    data = None
    if multipart is not None:
        boundary = "----b" + uuid.uuid4().hex
        parts = []
        for k, v in multipart["fields"].items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
        fn, content, ctype = multipart["file"]
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"Content\"; filename=\"{fn}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n")
        body = "".join(parts).encode() + content.encode() + f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        data = body
    elif fields is not None:
        data = urllib.parse.urlencode(fields, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        print(f"  ! {method} {url} -> {e.code}\n    {e.read().decode()[:300]}")
        raise


SLS = "https://serverless.twilio.com/v1"
UP = "https://serverless-upload.twilio.com/v1"

# 1. Service (reuse if present)
svcs = call("GET", f"{SLS}/Services?PageSize=50").get("services", [])
svc = next((s for s in svcs if s["unique_name"] == SERVICE_NAME), None)
if svc:
    print(f"1. service exists: {svc['sid']}")
else:
    svc = call("POST", f"{SLS}/Services",
               {"UniqueName": SERVICE_NAME, "FriendlyName": "CallMeIE inbound call screen",
                "IncludeCredentials": "true"})
    print(f"1. service created: {svc['sid']}")
SVC = svc["sid"]

# 2. Function (reuse if present)
fns = call("GET", f"{SLS}/Services/{SVC}/Functions?PageSize=50").get("functions", [])
fn = next((f for f in fns if f["friendly_name"] == "screen"), None)
if fn:
    print(f"2. function exists: {fn['sid']}")
else:
    fn = call("POST", f"{SLS}/Services/{SVC}/Functions", {"FriendlyName": "screen"})
    print(f"2. function created: {fn['sid']}")
FN = fn["sid"]

# 3. Upload a new Version of the function content
ver = call("POST", f"{UP}/Services/{SVC}/Functions/{FN}/Versions", multipart={
    "fields": {"Path": FN_PATH, "Visibility": "public"},
    "file": ("screen.js", FUNCTION_JS, "application/javascript"),
})
print(f"3. function version: {ver['sid']}")
FNV = ver["sid"]

# 4. Environment (reuse 'prod')
envs = call("GET", f"{SLS}/Services/{SVC}/Environments?PageSize=50").get("environments", [])
en = next((e for e in envs if e["unique_name"] == "prod"), None)
if en:
    print(f"4. env exists: {en['sid']} domain={en['domain_name']}")
else:
    en = call("POST", f"{SLS}/Services/{SVC}/Environments",
              {"UniqueName": "prod", "DomainSuffix": "scr"})
    print(f"4. env created: {en['sid']} domain={en['domain_name']}")
ENV, DOMAIN = en["sid"], en["domain_name"]

# 4b. Runtime env vars (DENYLIST + VAPI_INBOUND_URL) — upsert
existing = {v["key"]: v["sid"] for v in
            call("GET", f"{SLS}/Services/{SVC}/Environments/{ENV}/Variables?PageSize=50").get("variables", [])}
for k, val in (("DENYLIST", DENYLIST), ("VAPI_INBOUND_URL", VAPI_INBOUND)):
    if k in existing:
        call("POST", f"{SLS}/Services/{SVC}/Environments/{ENV}/Variables/{existing[k]}", {"Value": val})
    else:
        call("POST", f"{SLS}/Services/{SVC}/Environments/{ENV}/Variables", {"Key": k, "Value": val})
print(f"4b. env vars set: DENYLIST={DENYLIST}  VAPI_INBOUND_URL={VAPI_INBOUND}")

# 5. Build
build = call("POST", f"{SLS}/Services/{SVC}/Builds", {"FunctionVersions": FNV})
BUILD = build["sid"]
print(f"5. build {BUILD} status={build['status']} ... polling")
for _ in range(40):
    time.sleep(3)
    b = call("GET", f"{SLS}/Services/{SVC}/Builds/{BUILD}")
    if b["status"] in ("completed", "failed"):
        print(f"   build status={b['status']}")
        break
if b["status"] != "completed":
    print("BUILD DID NOT COMPLETE — aborting."); sys.exit(1)

# 6. Deploy build to env
dep = call("POST", f"{SLS}/Services/{SVC}/Environments/{ENV}/Deployments", {"BuildSid": BUILD})
print(f"6. deployed: {dep['sid']}")
FUNC_URL = f"https://{DOMAIN}{FN_PATH}"
print(f"   FUNCTION URL: {FUNC_URL}")

# 7. Self-test the deployed function (allow propagation)
def twiml_test(from_num):
    body = urllib.parse.urlencode({"From": from_num, "To": NUMBER, "CallSid": "CAtest"}).encode()
    r = urllib.request.Request(FUNC_URL, data=body, method="POST",
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "User-Agent": "deploy-test"})
    for attempt in range(6):
        try:
            return urllib.request.urlopen(r, timeout=30).read().decode()
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            time.sleep(5)
    return "(no response)"

print("7. self-test:")
t_deny = twiml_test(DENYLIST.split(",")[0])
t_ok = twiml_test("+353861234567")
print("   denylisted ->", " ".join(t_deny.split())[:160])
print("   allowed    ->", " ".join(t_ok.split())[:160])
ok = "Reject" in t_deny and "Redirect" in t_ok and VAPI_INBOUND in t_ok
print("   SELFTEST", "PASS" if ok else "FAIL")
if not ok:
    print("Self-test failed — NOT repointing number."); sys.exit(1)

# 8. Repoint number (skip on --dry). Set fallback to Vapi as safety net.
if DRY:
    print("\n--dry: number NOT repointed. To activate:")
    print(f"  VoiceUrl  -> {FUNC_URL}")
    print(f"  Fallback  -> {VAPI_INBOUND}")
else:
    call("POST", f"https://api.twilio.com/2010-04-01/Accounts/{ASID}/IncomingPhoneNumbers/{NUMBER_SID}.json",
         {"VoiceUrl": FUNC_URL, "VoiceMethod": "POST",
          "VoiceFallbackUrl": VAPI_INBOUND, "VoiceFallbackMethod": "POST"})
    print(f"\n8. NUMBER REPOINTED.")
    print(f"   {NUMBER} VoiceUrl -> {FUNC_URL}")
    print(f"   VoiceFallbackUrl -> {VAPI_INBOUND} (safety: Function failure still reaches Vapi)")
print("\nDONE.")
