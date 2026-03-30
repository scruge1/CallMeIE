"""
Combined webhook server for AI Receptionist agency.

Handles:
1. Vapi function calls (check availability, book appointment)
2. Post-call hooks (missed call text-back, call summaries)
3. Appointment reminders (24hr before)
4. No-show follow-ups

Deploy to: Railway, Render, or Fly.io (all have free tiers)

Usage:
    uvicorn server:app --host 0.0.0.0 --port $PORT
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import json
import os
import httpx
import sys
sys.path.insert(0, os.path.dirname(__file__))

app = FastAPI(title="AI Receptionist Server")

# --- Config ---
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
OWNER_NUMBER = os.environ.get("OWNER_NOTIFICATION_NUMBER", "")


# --- SMS ---
async def send_sms(to: str, body: str):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        print(f"[SMS MOCK] To: {to} | {body[:80]}...")
        return {"status": "mocked"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"To": to, "From": TWILIO_FROM, "Body": body},
        )
        return resp.json()


# --- Vapi Webhooks ---
@app.post("/vapi/call-ended")
async def call_ended(request: Request):
    """Vapi calls this when a call ends."""
    body = await request.json()
    call = body.get("call", body)
    status = call.get("status", "")
    caller = call.get("customer", {}).get("number", "")
    duration = call.get("duration", 0)

    # Missed call text-back
    if status in ("missed", "no-answer") and caller:
        await send_sms(
            caller,
            "Hi! We missed your call to Bright Smile Dental. "
            "We're here to help — reply to this text or ring us back. Thanks!",
        )
        print(f"[Missed Call] Text-back sent to {caller}")

    # Notify owner of after-hours calls
    if OWNER_NUMBER and duration > 10:
        await send_sms(
            OWNER_NUMBER,
            f"[AI Call] {caller} called ({duration}s). Check Vapi dashboard for details.",
        )

    return JSONResponse({"status": "ok"})


@app.post("/reminder")
async def send_reminder(request: Request):
    """Send appointment reminder. Called by scheduler."""
    body = await request.json()
    phone = body.get("phone", "")
    name = body.get("name", "")
    date = body.get("date", "")
    time = body.get("time", "")
    business = body.get("business", "Bright Smile Dental")

    if not phone:
        return JSONResponse({"error": "no phone"}, status_code=400)

    await send_sms(
        phone,
        f"Hi {name}! Reminder: you have an appointment at {business} "
        f"tomorrow ({date}) at {time}. Please arrive 10 min early. "
        f"Reply CANCEL to cancel.",
    )
    return JSONResponse({"status": "sent"})


@app.post("/no-show")
async def no_show(request: Request):
    """Send no-show follow-up."""
    body = await request.json()
    phone = body.get("phone", "")
    name = body.get("name", "")
    business = body.get("business", "Bright Smile Dental")

    if not phone:
        return JSONResponse({"error": "no phone"}, status_code=400)

    await send_sms(
        phone,
        f"Hi {name}! We missed you at your appointment at {business} today. "
        f"No worries — reply to reschedule or ring us.",
    )
    return JSONResponse({"status": "sent"})


@app.post("/sync-inventory")
async def sync_inventory_endpoint(request: Request):
    """Sync Google Sheet to Vapi knowledge base."""
    try:
        from sync_inventory import sync

        body = await request.json()
        sheet_id = body.get("sheet_id", "")
        assistant_id = body.get("assistant_id", "0b37deb5-2fc2-4e7b-81b1-e61e97103506")
        sheet_name = body.get("sheet_name", "Sheet1")

        if not sheet_id:
            return JSONResponse({"error": "sheet_id required"}, status_code=400)

        sync(sheet_id, assistant_id, sheet_name)
        return JSONResponse({"status": "synced", "sheet_id": sheet_id})
    except ImportError:
        return JSONResponse({"error": "sync_inventory module not found"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "ai-receptionist-server",
        "twilio_configured": bool(TWILIO_SID),
        "owner_notifications": bool(OWNER_NUMBER),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
