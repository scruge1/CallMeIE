"""
Webhook handler for Vapi AI assistant.

Receives function calls from Vapi during phone conversations and returns
results. Deploy this on any Python hosting (Railway, Render, Replit) or
use Make.com webhooks for no-code equivalent.

Endpoints:
  POST /vapi/function-call  — handles tool calls from Vapi assistant

Functions handled:
  - check_availability: returns available appointment slots
  - book_appointment: creates a calendar event + sends SMS confirmation
  - transfer_call: returns transfer number for emergencies
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
import json

app = FastAPI(title="AI Receptionist Webhook")

# ---- CONFIGURATION (per client) ----
# In production, load these from a database keyed by assistant_id

BUSINESS_CONFIG = {
    "name": "Bright Smile Dental",
    "timezone": "America/New_York",
    "emergency_number": "+15551234567",
    "appointment_duration_minutes": 60,  # new patients
    "followup_duration_minutes": 30,
    "business_hours": {
        "monday": ("08:00", "17:00"),
        "tuesday": ("08:00", "17:00"),
        "wednesday": ("08:00", "17:00"),
        "thursday": ("08:00", "17:00"),
        "friday": ("08:00", "17:00"),
        "saturday": ("09:00", "13:00"),
    },
}

# ---- MOCK DATA (replace with Google Calendar API in production) ----

MOCK_BOOKED_SLOTS = [
    "2026-04-01 09:00",
    "2026-04-01 10:00",
    "2026-04-01 14:00",
    "2026-04-02 09:00",
    "2026-04-02 11:00",
]


def get_available_slots(preferred_date: str, appointment_type: str = "new_patient"):
    """Return available slots for a given date. Mock implementation."""
    duration = BUSINESS_CONFIG["appointment_duration_minutes"]
    if appointment_type in ("follow_up", "cleaning"):
        duration = BUSINESS_CONFIG["followup_duration_minutes"]

    day_name = datetime.strptime(preferred_date, "%Y-%m-%d").strftime("%A").lower()
    hours = BUSINESS_CONFIG["business_hours"].get(day_name)

    if not hours:
        return {"available": False, "message": "We are closed on that day.", "slots": []}

    start_hour = int(hours[0].split(":")[0])
    end_hour = int(hours[1].split(":")[0])

    slots = []
    for hour in range(start_hour, end_hour):
        slot_str = f"{preferred_date} {hour:02d}:00"
        if slot_str not in MOCK_BOOKED_SLOTS:
            display = datetime.strptime(slot_str, "%Y-%m-%d %H:%M").strftime(
                "%I:%M %p"
            )
            slots.append({"time": slot_str, "display": display})

    if not slots:
        return {
            "available": False,
            "message": f"No availability on {preferred_date}. Would you like to try another day?",
            "slots": [],
        }

    return {
        "available": True,
        "message": f"We have {len(slots)} slots available on {preferred_date}.",
        "slots": slots[:5],
    }


def book_appointment(
    patient_name: str,
    date_time: str,
    phone: str,
    email: str = "",
    insurance: str = "",
    reason: str = "General appointment",
):
    """Book an appointment. Mock implementation — replace with Google Calendar API."""
    # In production: create Google Calendar event + send Twilio SMS
    return {
        "success": True,
        "confirmation_number": f"BS-{datetime.now().strftime('%Y%m%d%H%M')}",
        "message": f"Appointment confirmed for {patient_name} on {date_time}. A confirmation text has been sent to {phone}.",
        "details": {
            "patient": patient_name,
            "datetime": date_time,
            "phone": phone,
            "email": email,
            "insurance": insurance,
            "reason": reason,
        },
    }


# ---- VAPI WEBHOOK ENDPOINT ----


@app.post("/vapi/function-call")
async def handle_vapi_function_call(request: Request):
    """Handle function calls from Vapi assistant.

    Vapi sends a POST with the function name and arguments.
    We return the result as a JSON message for the assistant to speak.
    """
    body = await request.json()

    # Vapi sends the function call in message.functionCall
    message = body.get("message", {})
    function_call = message.get("functionCall", {})
    function_name = function_call.get("name", "")
    parameters = function_call.get("parameters", {})

    print(f"[Webhook] Function: {function_name}, Params: {json.dumps(parameters)}")

    if function_name == "check_availability":
        preferred_date = parameters.get("preferred_date", "")
        appointment_type = parameters.get("appointment_type", "new_patient")

        if not preferred_date:
            # If no date given, suggest tomorrow
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            preferred_date = tomorrow

        result = get_available_slots(preferred_date, appointment_type)
        return JSONResponse({"result": json.dumps(result)})

    elif function_name == "book_appointment":
        result = book_appointment(
            patient_name=parameters.get("patient_name", "Unknown"),
            date_time=parameters.get("date_time", ""),
            phone=parameters.get("phone", ""),
            email=parameters.get("email", ""),
            insurance=parameters.get("insurance", ""),
            reason=parameters.get("reason", "General appointment"),
        )
        return JSONResponse({"result": json.dumps(result)})

    elif function_name == "transfer_call":
        return JSONResponse(
            {
                "result": json.dumps(
                    {
                        "action": "transfer",
                        "number": BUSINESS_CONFIG["emergency_number"],
                        "message": "Transferring to on-call dentist now.",
                    }
                )
            }
        )

    else:
        return JSONResponse(
            {"result": json.dumps({"error": f"Unknown function: {function_name}"})}
        )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-receptionist-webhook"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
