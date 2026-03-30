"""
Multi-tenant AI Receptionist webhook server — CallMe.ie

Each client is identified by their Vapi assistant ID.
Client configs stored in CLIENTS_JSON env var:

  {
    "vapi-assistant-id": {
      "name": "Bright Smile Dental",
      "owner": "+353851234567",
      "from": "+16617643212"
    }
  }

Endpoints:
  POST /vapi/call-ended   — Vapi post-call hook
  POST /reminder          — 24hr appointment reminder
  POST /no-show           — No-show follow-up
  POST /sync-inventory    — Sync Google Sheet to Vapi KB
  GET  /health            — Health + client count
"""

import json
import os
import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(__file__))

app = FastAPI(title="CallMe.ie — AI Receptionist Server")

# --- Global Twilio config (fallback) ---
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
OWNER_NUMBER = os.environ.get("OWNER_NOTIFICATION_NUMBER", "")

# --- Client registry ---
_raw = os.environ.get("CLIENTS_JSON", "{}")
try:
    CLIENTS: dict = json.loads(_raw)
except Exception:
    CLIENTS = {}


def get_client(assistant_id: str) -> dict:
    """Return client config by Vapi assistant ID, or safe defaults."""
    return CLIENTS.get(assistant_id, {
        "name": "the business",
        "owner": OWNER_NUMBER,
        "from": TWILIO_FROM,
    })


# --- SMS ---
async def send_sms(to: str, body: str, from_number: str = "") -> dict:
    sender = from_number or TWILIO_FROM
    if not all([TWILIO_SID, TWILIO_TOKEN, sender]):
        print(f"[SMS MOCK] To: {to} | {body[:80]}...")
        return {"status": "mocked"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"To": to, "From": sender, "Body": body},
        )
        result = resp.json()
        if resp.status_code not in (200, 201):
            print(f"[SMS ERROR] {resp.status_code}: {result}")
        return result


# --- Vapi post-call webhook ---
@app.post("/vapi/call-ended")
async def call_ended(request: Request):
    body = await request.json()
    call = body.get("call", body)
    assistant_id = (call.get("assistantId", "")
                    or body.get("assistant", {}).get("id", ""))
    status = call.get("status", "")
    caller = call.get("customer", {}).get("number", "")
    duration = call.get("duration", 0)

    client = get_client(assistant_id)
    business = client["name"]
    owner = client.get("owner", OWNER_NUMBER)
    from_num = client.get("from", TWILIO_FROM)

    print(f"[Call] {business} | status={status} caller={caller} duration={duration}s")

    # Missed call text-back with GDPR opt-out
    if status in ("missed", "no-answer") and caller:
        await send_sms(
            caller,
            f"Hi! We missed your call to {business}. "
            f"We're here to help — reply or ring us back. "
            f"Reply STOP to opt out.",
            from_number=from_num,
        )
        print(f"[Missed Call] Text-back sent to {caller}")

    # Owner notification
    if owner and duration > 10:
        await send_sms(
            owner,
            f"[{business}] Call from {caller} ({duration}s). Check Vapi dashboard.",
            from_number=from_num,
        )

    return JSONResponse({"status": "ok"})


# --- Appointment reminder ---
@app.post("/reminder")
async def send_reminder(request: Request):
    body = await request.json()
    phone = body.get("phone", "")
    name = body.get("name", "")
    date = body.get("date", "")
    time = body.get("time", "")
    business = body.get("business", "the practice")
    from_num = body.get("from_number", TWILIO_FROM)

    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)

    await send_sms(
        phone,
        f"Hi {name}! Reminder: appointment at {business} "
        f"tomorrow ({date}) at {time}. Arrive 10 min early. "
        f"Reply CANCEL to cancel or STOP to opt out.",
        from_number=from_num,
    )
    return JSONResponse({"status": "sent"})


# --- No-show follow-up ---
@app.post("/no-show")
async def no_show(request: Request):
    body = await request.json()
    phone = body.get("phone", "")
    name = body.get("name", "")
    business = body.get("business", "the practice")
    from_num = body.get("from_number", TWILIO_FROM)

    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)

    await send_sms(
        phone,
        f"Hi {name}! We missed you at {business} today. "
        f"Reply to reschedule. Reply STOP to opt out.",
        from_number=from_num,
    )
    return JSONResponse({"status": "sent"})


# --- Inventory sync ---
@app.post("/sync-inventory")
async def sync_inventory_endpoint(request: Request):
    try:
        from sync_inventory import sync
        body = await request.json()
        sheet_id = body.get("sheet_id", "")
        assistant_id = body.get("assistant_id", "")
        sheet_name = body.get("sheet_name", "Sheet1")

        if not sheet_id or not assistant_id:
            return JSONResponse(
                {"error": "sheet_id and assistant_id required"}, status_code=400
            )

        sync(sheet_id, assistant_id, sheet_name)
        return JSONResponse({"status": "synced", "sheet_id": sheet_id})
    except ImportError:
        return JSONResponse({"error": "sync_inventory module not found"}, status_code=500)
    except Exception as e:
        print(f"[Sync Error] {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Health ---
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "callmeie-receptionist",
        "clients_loaded": len(CLIENTS),
        "twilio_configured": bool(TWILIO_SID),
        "global_owner_notifications": bool(OWNER_NUMBER),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
