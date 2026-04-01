"""
Google Calendar integration for AI Receptionist.

Each client shares their Google Calendar with the service account email.
The service account JSON is stored as GOOGLE_SERVICE_ACCOUNT_JSON env var.

Setup per client:
1. Client goes to calendar.google.com
2. Settings → Share with specific people
3. Add the service account email (from GOOGLE_SERVICE_ACCOUNT_JSON)
4. Grant "Make changes to events" permission
5. Store their calendar ID in CLIENTS_JSON config
"""

import json
import os
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

SCOPES = ["https://www.googleapis.com/auth/calendar"]
DUBLIN_TZ = ZoneInfo("Europe/Dublin")


def _get_service():
    """Build Google Calendar service from service account JSON env var."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")

    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_available_slots(calendar_id: str, date_str: str, duration_minutes: int = 30) -> list[dict]:
    """
    Return available time slots for a given date.

    Args:
        calendar_id: Google Calendar ID (e.g. 'primary' or email address)
        date_str: Date in YYYY-MM-DD format
        duration_minutes: Appointment length in minutes

    Returns:
        List of dicts with 'start' and 'end' ISO strings (business hours only)
    """
    service = _get_service()

    # Parse date in Dublin local time (handles GMT/IST automatically)
    target = datetime.strptime(date_str, "%Y-%m-%d")
    day_start = target.replace(hour=9, minute=0, second=0, tzinfo=DUBLIN_TZ)
    day_end   = target.replace(hour=17, minute=0, second=0, tzinfo=DUBLIN_TZ)
    # Exclude 1pm–2pm lunch break
    lunch_start = target.replace(hour=13, minute=0, second=0, tzinfo=DUBLIN_TZ)
    lunch_end   = target.replace(hour=14, minute=0, second=0, tzinfo=DUBLIN_TZ)

    # Get existing events for the day
    events_result = service.events().list(
        calendarId=calendar_id,
        timeMin=day_start.isoformat(),
        timeMax=day_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    busy_slots = []
    for event in events_result.get("items", []):
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))
        if start and end:
            busy_slots.append((
                datetime.fromisoformat(start.replace("Z", "+00:00")),
                datetime.fromisoformat(end.replace("Z", "+00:00")),
            ))

    # Build free slots
    available = []
    slot_start = day_start
    delta = timedelta(minutes=duration_minutes)

    while slot_start + delta <= day_end:
        slot_end = slot_start + delta
        # Check if this slot overlaps any busy period
        conflict = any(
            slot_start < b_end and slot_end > b_start
            for b_start, b_end in busy_slots
        )
        # Also exclude lunch (1pm–2pm)
        lunch_overlap = slot_start < lunch_end and slot_end > lunch_start
        if not conflict and not lunch_overlap:
            # strftime %-I is Linux-only; use lstrip("0") for cross-platform
            hour_str = slot_start.strftime("%I:%M %p").lstrip("0")
            available.append({
                "start": slot_start.isoformat(),
                "end": slot_end.isoformat(),
                "display": hour_str,
            })
        slot_start += delta

    return available


def book_appointment(
    calendar_id: str,
    title: str,
    start_iso: str,
    end_iso: str,
    customer_name: str,
    customer_phone: str,
    customer_email: str = "",
    notes: str = "",
) -> dict:
    """
    Create an appointment on the client's Google Calendar.

    Returns the created event dict with 'id' and 'htmlLink'.
    """
    service = _get_service()

    description_parts = [f"Phone: {customer_phone}"]
    if customer_email:
        description_parts.append(f"Email: {customer_email}")
    if notes:
        description_parts.append(notes)

    event = {
        "summary": f"{title} — {customer_name}",
        "description": "\n".join(description_parts).strip(),
        "start": {"dateTime": start_iso, "timeZone": "Europe/Dublin"},
        "end": {"dateTime": end_iso, "timeZone": "Europe/Dublin"},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ],
        },
    }

    if customer_email:
        event["attendees"] = [{"email": customer_email}]

    try:
        created = service.events().insert(
            calendarId=calendar_id,
            body=event,
            sendUpdates="none",
        ).execute()
    except Exception:
        # Some shared-calendar configurations reject attendee writes.
        # In that case, keep the booking itself instead of failing the whole flow.
        event.pop("attendees", None)
        created = service.events().insert(
            calendarId=calendar_id,
            body=event,
            sendUpdates="none",
        ).execute()
    return {
        "id": created.get("id"),
        "link": created.get("htmlLink"),
        "start": created["start"].get("dateTime"),
        "end": created["end"].get("dateTime"),
    }
