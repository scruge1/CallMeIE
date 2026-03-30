"""
Set up appointment reminder and missed call text-back flows.

These use Vapi's server URL webhook to trigger actions after calls end.
The webhook receives call data and triggers:
1. Appointment reminders (24hr before via scheduled SMS)
2. Missed call text-back (immediate SMS when call is missed)
3. Post-call summary to business owner

Requires: Make.com account (free tier) or this webhook server running.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import json
import os
import httpx

app = FastAPI(title="AI Receptionist Post-Call Hooks")

# Twilio credentials (for SMS)
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")  # Your Twilio number


async def send_sms(to: str, body: str):
    """Send SMS via Twilio."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        print(f"[SMS MOCK] To: {to} Body: {body}")
        return {"status": "mocked"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"To": to, "From": TWILIO_FROM, "Body": body},
        )
        return resp.json()


@app.post("/webhook/call-ended")
async def handle_call_ended(request: Request):
    """Vapi sends this when a call ends. We use it to trigger follow-up actions."""
    body = await request.json()

    call = body.get("call", body)
    status = call.get("status", "")
    caller_number = call.get("customer", {}).get("number", "")
    assistant_name = call.get("assistant", {}).get("name", "")
    duration = call.get("duration", 0)
    transcript = call.get("transcript", "")

    print(f"[Call Ended] Status: {status}, Caller: {caller_number}, Duration: {duration}s")

    # MISSED CALL TEXT-BACK
    if status in ("missed", "no-answer") and caller_number:
        msg = (
            f"Hi! We missed your call to Bright Smile Dental. "
            f"We're here to help — reply to this text or ring us back "
            f"and we'll get you sorted. Thanks!"
        )
        await send_sms(caller_number, msg)
        print(f"[Missed Call Text-Back] Sent to {caller_number}")

    # POST-CALL SUMMARY TO BUSINESS OWNER
    if duration > 10 and transcript:
        # Extract key info from transcript for the summary
        summary_msg = (
            f"[AI Call Summary]\n"
            f"Caller: {caller_number}\n"
            f"Duration: {duration}s\n"
            f"Transcript excerpt: {transcript[:300]}..."
        )
        # Send to business owner
        owner_number = os.environ.get("OWNER_NUMBER", "")
        if owner_number:
            await send_sms(owner_number, summary_msg)

    return JSONResponse({"status": "ok"})


@app.post("/webhook/appointment-reminder")
async def send_reminder(request: Request):
    """Called by a scheduler (Make.com or cron) 24hr before each appointment."""
    body = await request.json()

    patient_name = body.get("patient_name", "")
    patient_phone = body.get("patient_phone", "")
    appointment_date = body.get("appointment_date", "")
    appointment_time = body.get("appointment_time", "")
    business_name = body.get("business_name", "Bright Smile Dental")

    if not patient_phone:
        return JSONResponse({"error": "No phone number"}, status_code=400)

    msg = (
        f"Hi {patient_name}! Just a reminder that you have an appointment "
        f"at {business_name} tomorrow ({appointment_date}) at {appointment_time}. "
        f"Please arrive 10 minutes early. "
        f"Reply CANCEL if you need to cancel. See you then!"
    )

    result = await send_sms(patient_phone, msg)
    print(f"[Reminder] Sent to {patient_phone}: {appointment_date} {appointment_time}")

    return JSONResponse({"status": "sent", "result": result})


@app.post("/webhook/no-show-followup")
async def no_show_followup(request: Request):
    """Called when a patient doesn't show up. Sends a reschedule text."""
    body = await request.json()

    patient_name = body.get("patient_name", "")
    patient_phone = body.get("patient_phone", "")
    business_name = body.get("business_name", "Bright Smile Dental")

    msg = (
        f"Hi {patient_name}! We missed you at your appointment today at "
        f"{business_name}. No worries — would you like to reschedule? "
        f"Just reply to this text or ring us and we'll sort it out."
    )

    result = await send_sms(patient_phone, msg)
    return JSONResponse({"status": "sent", "result": result})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-receptionist-hooks"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081)
