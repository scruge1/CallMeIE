"""
Multi-tenant AI Receptionist webhook server.

Each client is identified by their Vapi assistant ID.
Client configs are stored in CLIENTS_JSON env var.

CLIENTS_JSON format:
{
  "vapi-assistant-id-here": {
    "name": "Bright Smile Dental",
    "owner": "+353851234567",
    "from": "+16617643212"
  }
}

Endpoints:
  POST /vapi/call-ended   — Vapi post-call hook (missed call text-back, owner alert)
  POST /reminder          — Send appointment reminder (called by external scheduler)
  POST /no-show           — Send no-show follow-up
  POST /sync-inventory    — Sync Google Sheet to Vapi knowledge base
  GET  /health            — Health check + loaded client count
"""

import json
import os
import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.dirname(__file__))

app = FastAPI(title="CallMe.ie — AI Receptionist Server")

# --- Global Twilio fallback (used if client has no per-number config) ---
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
OWNER_NUMBER = os.environ.get("OWNER_NOTIFICATION_NUMBER", "")

# --- Client registry ---
# Each key is a Vapi assistant ID. Value is client-specific config.
_raw = os.environ.get("CLIENTS_JSON", "{}")
try:
    CLIENTS: dict = json.loads(_raw)
except Exception:
    CLIENTS = {}


def get_client(assistant_id: str) -> dict:
    """Return client config for this assistant, or safe defaults."""
    return CLIENTS.get(assistant_id, {
        "name": "the business",
        "owner": OWNER_NUMBER,
        "from": TWILIO_FROM,
    })


# --- SMS ---
async def send_sms(to: str, body: str, from_number: str = "") -> dict:
    """Send SMS via Twilio. Uses per-client from_number if provided."""
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
    """Vapi fires this when any call ends."""
    body = await request.json()

    # Vapi wraps the call object differently depending on event type
    call = body.get("call", body)
    assistant_id = call.get("assistantId", "") or body.get("assistant", {}).get("id", "")
    status = call.get("status", "")
    caller = call.get("customer", {}).get("number", "")
    duration = call.get("duration", 0)

    client = get_client(assistant_id)
    business = client["name"]
    owner = client.get("owner", OWNER_NUMBER)
    from_num = client.get("from", TWILIO_FROM)

    print(f"[Call] assistant={assistant_id} status={status} caller={caller} duration={duration}s")

    # Missed call text-back (with GDPR opt-out)
    if status in ("missed", "no-answer") and caller:
        await send_sms(
            caller,
            f"Hi! We missed your call to {business}. "
            f"We're here to help — reply to this text or ring us back. "
            f"Reply STOP to opt out.",
            from_number=from_num,
        )
        print(f"[Missed Call] Text-back sent to {caller} for {business}")

    # Notify business owner
    if owner and duration > 10:
        await send_sms(
            owner,
            f"[{business}] {caller} called ({duration}s). Check Vapi dashboard.",
            from_number=from_num,
        )

    return JSONResponse({"status": "ok"})


# --- Appointment reminder ---
@app.post("/reminder")
async def send_reminder(request: Request):
    """Send 24hr appointment reminder. Called by external scheduler."""
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
        f"tomorrow ({date}) at {time}. Please arrive 10 min early. "
        f"Reply CANCEL to cancel or STOP to opt out.",
        from_number=from_num,
    )
    return JSONResponse({"status": "sent"})


# --- No-show follow-up ---
@app.post("/no-show")
async def no_show(request: Request):
    """Send no-show follow-up. Called by external scheduler."""
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
        f"No worries — reply to reschedule. Reply STOP to opt out.",
        from_number=from_num,
    )
    return JSONResponse({"status": "sent"})


# --- Inventory sync ---
@app.post("/sync-inventory")
async def sync_inventory_endpoint(request: Request):
    """Sync a client's Google Sheet to their Vapi knowledge base."""
    try:
        from sync_inventory import sync

        body = await request.json()
        sheet_id = body.get("sheet_id", "")
        assistant_id = body.get("assistant_id", "")
        sheet_name = body.get("sheet_name", "Sheet1")

        if not sheet_id or not assistant_id:
            return JSONResponse({"error": "sheet_id and assistant_id required"}, status_code=400)

        sync(sheet_id, assistant_id, sheet_name)
        return JSONResponse({"status": "synced", "sheet_id": sheet_id})
    except ImportError:
        return JSONResponse({"error": "sync_inventory module not found"}, status_code=500)
    except Exception as e:
        print(f"[Sync Error] {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _parse_vapi_tool_call(body: dict) -> tuple[str, str, dict]:
    """
    Extract (tool_call_id, assistant_id, args) from a Vapi tool-call POST body.

    Vapi format:
      body.message.type == "tool-calls"
      body.message.toolCallList[0].id          -> tool_call_id
      body.message.toolCallList[0].function.arguments -> JSON string or dict
      body.message.call.assistantId            -> assistant_id
    """
    msg = body.get("message", {})
    tool_calls = msg.get("toolCallList", [])
    call = tool_calls[0] if tool_calls else {}

    tool_call_id = call.get("id", "")
    raw_args = call.get("function", {}).get("arguments", {})
    # Vapi may send arguments as a JSON string or already-parsed dict
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except Exception:
            args = {}
    else:
        args = raw_args or body  # fallback: direct POST for testing

    # assistant_id lives under message.call for Vapi webhooks
    assistant_id = (
        msg.get("call", {}).get("assistantId", "")
        or body.get("assistantId", "")
        or body.get("assistant_id", "")
    )
    return tool_call_id, assistant_id, args


def _vapi_result(tool_call_id: str, result: str) -> JSONResponse:
    """Return Vapi-compatible tool result response."""
    return JSONResponse({"results": [{"toolCallId": tool_call_id, "result": result}]})


# --- Google Calendar: check availability ---
@app.post("/check-availability")
async def check_availability(request: Request):
    """
    Vapi tool: check available appointment slots for a given date.
    Called by the AI when a caller asks about booking.
    """
    body = await request.json()
    tool_call_id, assistant_id, args = _parse_vapi_tool_call(body)
    try:
        from calendar_api import get_available_slots

        date_str = args.get("date", "")
        duration = int(args.get("duration_minutes", 30))

        if not date_str:
            return _vapi_result(tool_call_id, "I need a date to check availability. What date works for you?")

        client = get_client(assistant_id)
        calendar_id = client.get("calendar_id", "primary")
        business = client["name"]

        slots = get_available_slots(calendar_id, date_str, duration)
        print(f"[Availability] {date_str} — {len(slots)} slots for {business}")

        if not slots:
            return _vapi_result(tool_call_id, f"I'm sorry, we have no availability on {date_str}. Would you like to try another date?")

        # Build human-readable slot list for the AI to read out
        slot_names = [s["display"] for s in slots[:6]]  # cap at 6 to keep response short
        slots_text = ", ".join(slot_names[:-1]) + f", or {slot_names[-1]}" if len(slot_names) > 1 else slot_names[0]
        return _vapi_result(tool_call_id, f"We have the following slots available on {date_str}: {slots_text}. Which time suits you?")

    except ImportError:
        return _vapi_result(tool_call_id, "Calendar system is temporarily unavailable. Please call us directly to book.")
    except Exception as e:
        print(f"[Calendar Error] {e}")
        return _vapi_result(tool_call_id, "I had trouble checking the calendar. Let me take your details and we'll call you back to confirm.")


# --- Google Calendar: book appointment ---
@app.post("/book-appointment")
async def book_appointment_endpoint(request: Request):
    """
    Vapi tool: create an appointment on the client's Google Calendar.
    Called by the AI after confirming a time slot with the caller.
    """
    body = await request.json()
    tool_call_id, assistant_id, args = _parse_vapi_tool_call(body)
    try:
        from calendar_api import book_appointment

        customer_name = args.get("customer_name", "")
        customer_phone = args.get("customer_phone", "")
        start_iso = args.get("start_iso", "")
        end_iso = args.get("end_iso", "")
        title = args.get("title", "Appointment")

        if not all([customer_name, customer_phone, start_iso, end_iso]):
            return _vapi_result(tool_call_id, "I need your name, phone number, and preferred time to complete the booking. Could you provide those?")

        client = get_client(assistant_id)
        calendar_id = client.get("calendar_id", "primary")
        business = client["name"]

        event = book_appointment(
            calendar_id=calendar_id,
            title=f"{title} at {business}",
            start_iso=start_iso,
            end_iso=end_iso,
            customer_name=customer_name,
            customer_phone=customer_phone,
        )

        from datetime import datetime
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        readable = start_dt.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")

        # Fire-and-forget SMS notifications
        from_num = client.get("from", TWILIO_FROM)
        owner = client.get("owner", OWNER_NUMBER)
        if customer_phone:
            await send_sms(
                customer_phone,
                f"Hi {customer_name}! Your appointment at {business} is confirmed for {readable}. "
                f"We look forward to seeing you. Reply STOP to opt out.",
                from_number=from_num,
            )
        if owner:
            await send_sms(
                owner,
                f"[{business}] New booking: {customer_name} ({customer_phone}) — {readable}",
                from_number=from_num,
            )

        print(f"[Booking] {customer_name} at {business} — {readable}")
        return _vapi_result(
            tool_call_id,
            f"Perfect, {customer_name}! Your appointment at {business} is confirmed for {readable}. "
            f"We'll send a confirmation text to {customer_phone}. Is there anything else I can help you with?"
        )

    except ImportError:
        return _vapi_result(tool_call_id, "Calendar system is temporarily unavailable. Please call us to reschedule.")
    except Exception as e:
        print(f"[Booking Error] {e}")
        return _vapi_result(tool_call_id, f"I wasn't able to complete the booking right now. Please call us back and we'll sort it out.")


# --- Health check ---
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
