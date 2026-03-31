"""
Multi-tenant AI Receptionist webhook server.

Each client is identified by their Vapi assistant ID.
Client configs come from the SQLite DB (provisioned via /admin) with
fallback to the CLIENTS_JSON env var for manually-configured clients.

Endpoints:
  POST /vapi/call-ended        — Vapi post-call hook
  POST /reminder               — Send appointment reminder
  POST /no-show                — Send no-show follow-up
  POST /sync-inventory         — Sync Google Sheet to Vapi KB
  POST /capture-lead           — Demo lead capture (Claire) — stores in DB + SMS owner
  POST /demo-complete          — Demo assistant end-of-demo hook — enriched owner alert
  POST /submit-onboarding      — Client onboarding form submission
  GET  /admin                  — Admin portal (protected)
  GET  /admin/api/submissions  — List pending submissions
  GET  /admin/api/clients      — List provisioned clients
  POST /admin/api/provision/{id} — Provision a client from submission
  POST /admin/api/reject/{id}  — Reject a submission
  GET  /health                 — Health check
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, os.path.dirname(__file__))

app = FastAPI(title="CallMeIE — AI Receptionist Server")

# CORS — allow the onboarding form and landing page to POST to this server
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://callmeie.github.io",
        "https://callmeie.ie",
        "https://www.callmeie.ie",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# --- Global Twilio fallback ---
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "")
OWNER_NUMBER = os.environ.get("OWNER_NOTIFICATION_NUMBER", "")

# --- Vapi + admin ---
VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "changeme")

# --- Anomaly diagnostics (Claude API) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANOMALY_THRESHOLD = 0.7   # min score to invoke Claude
ANOMALY_BUDGET_PER_HOUR = 5   # circuit breaker — stops storm flooding (same root cause)
ANOMALY_BUDGET_PER_DAY = 200  # daily ceiling for a busy high-volume client
GOOGLE_SA_EMAIL = os.environ.get("GOOGLE_SA_EMAIL", "callmeie-receptionist@callme-ie.iam.gserviceaccount.com")

# --- Client registry (env var fallback for manually-configured clients) ---
_raw = os.environ.get("CLIENTS_JSON", "{}")
try:
    CLIENTS: dict = json.loads(_raw)
except Exception:
    CLIENTS = {}

# --- Demo assistant IDs (used to detect demo calls in webhooks) ---
DEMO_ASSISTANT_IDS = {
    "0b37deb5-2fc2-4e7b-81b1-e61e97103506": "dental",
    "8a533a56-2ca4-486f-b328-69183b59fa41": "motor factors",
    "db4ab378-cd8a-40f5-b3f9-8fcaaba408b0": "salon",
    "7774b535-95fe-4e75-b571-dde098e2f8fb": "solicitor",
}

# --- SQLite DB ---
DB_PATH = os.environ.get("DB_PATH", "/tmp/callmeie.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    TEXT    DEFAULT (datetime('now')),
                status        TEXT    DEFAULT 'pending',
                business_name TEXT,
                contact_name  TEXT,
                contact_phone TEXT,
                contact_email TEXT,
                business_type TEXT,
                address       TEXT,
                hours         TEXT,
                services      TEXT,
                emergency_number TEXT,
                calendar_email   TEXT,
                plan          TEXT,
                ai_name       TEXT,
                notes         TEXT,
                vapi_assistant_id TEXT,
                provisioned_at    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS call_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                call_id    TEXT,
                event_type TEXT,
                assistant  TEXT,
                summary    TEXT,
                detail     TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    TEXT DEFAULT (datetime('now')),
                call_id       TEXT,
                name          TEXT,
                phone         TEXT,
                business_type TEXT,
                interest      TEXT,
                source        TEXT DEFAULT 'demo',
                demo_completed INTEGER DEFAULT 0,
                topics_discussed TEXT,
                interest_level   TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS call_diagnostics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now')),
                call_id     TEXT UNIQUE,
                assistant   TEXT,
                score       REAL,
                diagnosis   TEXT,
                action      TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                assistant_id  TEXT UNIQUE,
                name          TEXT,
                owner_phone   TEXT,
                from_number   TEXT,
                calendar_id   TEXT,
                status        TEXT DEFAULT 'active',
                created_at    TEXT DEFAULT (datetime('now')),
                submission_id INTEGER
            )
        """)
        conn.commit()


init_db()


def log_event(call_id: str, event_type: str, assistant: str, summary: str, detail: dict = None):
    """Write a structured event to call_events for the live call log."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO call_events (call_id, event_type, assistant, summary, detail) VALUES (?,?,?,?,?)",
                (call_id, event_type, assistant, summary, json.dumps(detail or {}))
            )
            conn.commit()
    except Exception as e:
        print(f"[LOG] {e}")


def score_anomaly(status: str, duration: int, is_demo: bool) -> float:
    """
    Score how anomalous a call-ended event is (0.0–1.0).
    Anything >= ANOMALY_THRESHOLD gets queued for Claude diagnosis.

    Thresholds based on industry benchmarks:
    - Dental/salon average call duration for booking: 60–180s
    - <10s = almost certainly a technical failure (not a real interaction)
    - <30s = dropped or AI confused (too short for any real booking conversation)
    - missed/no-answer: standard SMB miss rate is 30–35%; individual events are normal
      but combined with very short duration they signal infrastructure failure
    """
    score = 0.0
    if status in ("missed", "no-answer"):
        score += 0.4
    if status == "failed":
        score += 0.6
    if not is_demo:
        if duration < 10:
            score += 0.5   # almost certainly technical failure — not a real interaction
        elif duration < 30:
            score += 0.2   # too short for any real booking conversation
    return min(score, 1.0)


async def diagnose_call_anomaly(
    call_id: str,
    assistant_id: str,
    assistant_name: str,
    status: str,
    duration: int,
    caller: str,
    score: float,
) -> None:
    """
    Background task: call Claude API to diagnose an anomalous call.
    Guarded by: idempotency check + per-client daily budget.
    Logs result to call_diagnostics + call_events.
    SMS owner if action is required.
    """
    if not ANTHROPIC_API_KEY:
        print(f"[Diag] No ANTHROPIC_API_KEY — skipping diagnosis for {call_id}")
        return

    # --- Idempotency ---
    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id FROM call_diagnostics WHERE call_id = ?", (call_id,)
            ).fetchone()
            if existing:
                return  # already diagnosed

            # --- Per-hour circuit breaker (stops storm flooding from one root cause) ---
            used_hour = conn.execute("""
                SELECT COUNT(*) AS n FROM call_diagnostics
                WHERE assistant = ? AND created_at > datetime('now', '-1 hour')
            """, (assistant_id,)).fetchone()["n"]
            if used_hour >= ANOMALY_BUDGET_PER_HOUR:
                print(f"[Diag] Hour circuit breaker for {assistant_name} ({used_hour}/hr)")
                return

            # --- Daily ceiling (high-volume clients) ---
            used_day = conn.execute("""
                SELECT COUNT(*) AS n FROM call_diagnostics
                WHERE assistant = ? AND created_at > datetime('now', '-1 day')
            """, (assistant_id,)).fetchone()["n"]
            if used_day >= ANOMALY_BUDGET_PER_DAY:
                print(f"[Diag] Daily ceiling for {assistant_name} ({used_day}/day)")
                return
    except Exception as e:
        print(f"[Diag] DB check failed: {e}")
        return

    # --- Pull last 10 events for context ---
    context_lines = []
    try:
        with get_db() as conn:
            events = conn.execute("""
                SELECT event_type, summary, created_at FROM call_events
                WHERE call_id = ? ORDER BY created_at ASC LIMIT 10
            """, (call_id,)).fetchall()
            context_lines = [f"- [{r['created_at']}] {r['event_type']}: {r['summary']}" for r in events]
    except Exception:
        pass

    context_str = "\n".join(context_lines) if context_lines else "(no events logged for this call)"

    prompt = (
        f"You are the operations monitor for CallMeIE, an Irish AI phone receptionist service.\n\n"
        f"A call anomaly was detected (score {score:.2f}/1.0).\n\n"
        f"Assistant: {assistant_name} ({assistant_id})\n"
        f"Call ID: {call_id}\n"
        f"Caller: {caller}\n"
        f"Status: {status} | Duration: {duration}s\n\n"
        f"Call event log:\n{context_str}\n\n"
        f"In 2-3 sentences: diagnose what likely went wrong, and recommend ONE action for the owner.\n"
        f"Format: DIAGNOSIS: <text> | ACTION: <text>"
    )

    diagnosis = ""
    action = ""
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if r.status_code == 200:
            text = r.json()["content"][0]["text"].strip()
            if "| ACTION:" in text:
                parts = text.split("| ACTION:", 1)
                diagnosis = parts[0].replace("DIAGNOSIS:", "").strip()
                action = parts[1].strip()
            else:
                diagnosis = text
        else:
            diagnosis = f"Claude API error {r.status_code}"
            print(f"[Diag] Claude error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        diagnosis = f"Diagnosis failed: {e}"
        print(f"[Diag] Exception: {e}")

    # --- Persist ---
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO call_diagnostics (call_id, assistant, score, diagnosis, action) VALUES (?,?,?,?,?)",
                (call_id, assistant_id, score, diagnosis, action),
            )
            conn.commit()
        log_event(call_id, "diagnosed", assistant_id,
                  f"score={score:.2f} | {diagnosis[:80]}",
                  {"score": score, "diagnosis": diagnosis, "action": action})
    except Exception as e:
        print(f"[Diag] Persist failed: {e}")

    # --- Alert owner if action needed ---
    if action and OWNER_NUMBER:
        await send_sms(
            OWNER_NUMBER,
            f"[CallMeIE Alert] {assistant_name}\n"
            f"Anomaly (score {score:.1f}) on call from {caller}\n"
            f"{diagnosis}\nAction: {action}",
        )
    print(f"[Diag] {assistant_name} | score={score:.2f} | {diagnosis[:60]}")


def check_admin(token: str = Query("")):
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_client(assistant_id: str) -> dict:
    """Return client config — DB first, fallback to CLIENTS env var."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM clients WHERE assistant_id = ?", (assistant_id,)
            ).fetchone()
            if row:
                return {
                    "name": row["name"],
                    "owner": row["owner_phone"] or OWNER_NUMBER,
                    "from": row["from_number"] or TWILIO_FROM,
                    "calendar_id": row["calendar_id"] or "primary",
                }
    except Exception:
        pass
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
async def call_ended(request: Request, background_tasks: BackgroundTasks):
    """Vapi fires this when any call ends. Returns 200 immediately; anomaly diagnosis runs in background."""
    body = await request.json()

    call = body.get("call", body)
    assistant_id = call.get("assistantId", "") or body.get("assistant", {}).get("id", "")
    status = call.get("status", "")
    caller = call.get("customer", {}).get("number", "")
    duration = call.get("duration", 0)
    call_id = call.get("id", "")

    client = get_client(assistant_id)
    business = client["name"]
    owner = client.get("owner", OWNER_NUMBER)
    from_num = client.get("from", TWILIO_FROM)

    print(f"[Call] assistant={assistant_id} status={status} caller={caller} duration={duration}s")
    log_event(call_id, "call-ended", assistant_id,
              f"{status} | {duration}s | caller:{caller}",
              {"status": status, "duration": duration, "caller": caller})

    is_demo = assistant_id in DEMO_ASSISTANT_IDS

    if is_demo and caller and duration > 30:
        # Look up the captured lead for this call to get their name
        lead = None
        if call_id:
            try:
                with get_db() as conn:
                    lead = conn.execute(
                        "SELECT * FROM leads WHERE call_id = ? ORDER BY created_at DESC LIMIT 1",
                        (call_id,)
                    ).fetchone()
            except Exception:
                pass

        name = lead["name"] if lead and lead["name"] else ""
        greeting = f"Hi {name}! " if name else "Hi! "
        demo_type = DEMO_ASSISTANT_IDS[assistant_id]

        # Follow-up SMS to prospect
        await send_sms(
            caller,
            f"{greeting}Thanks for trying the CallMeIE {demo_type} demo. "
            f"Our team will ring you shortly to chat about getting this set up for your business. "
            f"Reply STOP to opt out.",
            from_number=from_num,
        )
        print(f"[Demo Follow-up] SMS sent to {caller} ({demo_type})")

    elif not is_demo and status in ("missed", "no-answer") and caller:
        # Regular missed call text-back for real client assistants
        await send_sms(
            caller,
            f"Hi! We missed your call to {business}. "
            f"We're here to help — reply to this text or ring us back. "
            f"Reply STOP to opt out.",
            from_number=from_num,
        )
        print(f"[Missed Call] Text-back sent to {caller} for {business}")

    # Owner notification for real client calls (demo complete alerts come from /demo-complete)
    if not is_demo and owner and duration > 10:
        await send_sms(
            owner,
            f"[{business}] {caller} called ({duration}s). Check Vapi dashboard.",
            from_number=from_num,
        )

    # --- Anomaly detection (real clients only) ---
    if not is_demo and call_id:
        anomaly_score = score_anomaly(status, duration, is_demo=False)
        if anomaly_score >= ANOMALY_THRESHOLD:
            background_tasks.add_task(
                diagnose_call_anomaly,
                call_id=call_id,
                assistant_id=assistant_id,
                assistant_name=business,
                status=status,
                duration=duration,
                caller=caller,
                score=anomaly_score,
            )
            print(f"[Anomaly] Queued diagnosis for {business} | score={anomaly_score:.2f}")

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
    call_id = body.get("message", {}).get("call", {}).get("id", "")
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
        log_event(call_id, "avail-check", assistant_id,
                  f"{date_str} → {len(slots)} slots",
                  {"date": date_str, "slots_found": len(slots), "business": business})

        # Alert if 3+ avail-checks on this call with no booking yet (calendar full or AI looping)
        # Industry benchmark: normal booking = 1–2 checks; 3+ = something is wrong
        if call_id:
            try:
                with get_db() as conn:
                    check_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM call_events WHERE call_id=? AND event_type='avail-check'",
                        (call_id,)
                    ).fetchone()["n"]
                    has_booking = conn.execute(
                        "SELECT 1 FROM call_events WHERE call_id=? AND event_type='booking' LIMIT 1",
                        (call_id,)
                    ).fetchone()
                if check_count >= 3 and not has_booking:
                    log_event(call_id, "avail-check-loop", assistant_id,
                              f"{check_count} checks, no booking — calendar full or AI looping",
                              {"checks": check_count, "business": business})
                    await send_sms(
                        OWNER_NUMBER,
                        f"[CallMeIE] {business}: caller checked availability {check_count} times with no booking.\n"
                        f"Calendar may be full or the AI is looping. Check Vapi call log.",
                    )
            except Exception as loop_err:
                print(f"[AvailLoop] {loop_err}")

        if not slots:
            return _vapi_result(tool_call_id, f"I'm sorry, we have no availability on {date_str}. Would you like to try another date?")

        slot_names = [s["display"] for s in slots[:6]]
        slots_text = ", ".join(slot_names[:-1]) + f", or {slot_names[-1]}" if len(slot_names) > 1 else slot_names[0]
        return _vapi_result(tool_call_id, f"We have the following slots available on {date_str}: {slots_text}. Which time suits you?")

    except ImportError:
        log_event(call_id, "avail-check-fail", assistant_id, "calendar_api module missing")
        return _vapi_result(tool_call_id, "Calendar system is temporarily unavailable. Please call us directly to book.")
    except Exception as e:
        print(f"[Calendar Error] {e}")
        log_event(call_id, "avail-check-fail", assistant_id, str(e)[:120],
                  {"error": str(e)})
        # Alert owner immediately — calendar access broken = silent revenue loss
        await send_sms(
            OWNER_NUMBER,
            f"[CallMeIE] Calendar check failed for {get_client(assistant_id)['name']}.\n"
            f"Error: {str(e)[:80]}\n"
            f"Check Google Calendar is still shared with the service account.",
        )
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
    call_id = body.get("message", {}).get("call", {}).get("id", "")
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
            sms_result = await send_sms(
                customer_phone,
                f"Hi {customer_name}! Your appointment at {business} is confirmed for {readable}. "
                f"We look forward to seeing you. Reply STOP to opt out.",
                from_number=from_num,
            )
            # Every booking confirmation failure is individually significant — a patient
            # who didn't get this text is a likely no-show (Twilio benchmark: near 100% delivery expected)
            sms_status = sms_result.get("status", "") if isinstance(sms_result, dict) else ""
            if sms_status in ("failed", "undelivered"):
                log_event(call_id, "sms-fail", assistant_id,
                          f"Booking confirmation not delivered to {customer_phone}",
                          {"to": customer_phone, "twilio_status": sms_status, "name": customer_name})
                if owner:
                    await send_sms(
                        owner,
                        f"[CallMeIE] SMS confirmation FAILED for {customer_name} ({customer_phone}) "
                        f"at {business} — {readable}. Ring them to confirm manually.",
                        from_number=from_num,
                    )
        if owner:
            await send_sms(
                owner,
                f"[{business}] New booking: {customer_name} ({customer_phone}) — {readable}",
                from_number=from_num,
            )

        print(f"[Booking] {customer_name} at {business} — {readable}")
        log_event(call_id, "booking", assistant_id,
                  f"{customer_name} | {readable}",
                  {"name": customer_name, "phone": customer_phone, "time": readable, "business": business})
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


# --- Demo lead capture (called by Claire via Vapi tool) ---
@app.post("/capture-lead")
async def capture_lead(request: Request):
    """
    Claire calls this before transferring a demo prospect.
    Saves lead to DB (for post-call follow-up lookup) and fires SMS to owner.
    source="demo" for standard demo leads, source="catch_all" for custom enquiries.
    """
    body = await request.json()
    tool_call_id, assistant_id, args = _parse_vapi_tool_call(body)

    name        = args.get("name", "").strip()
    phone       = args.get("phone", "").strip()
    business    = args.get("business_type", "").strip()
    interest    = args.get("interest", "").strip()
    source      = args.get("source", "demo").strip()
    call_id     = body.get("message", {}).get("call", {}).get("id", "")

    if not phone:
        log_event(call_id, "lead-error", assistant_id, "captureLead called but no phone provided")
        return _vapi_result(tool_call_id, "Could you repeat that number for me? I want to make sure I have it right.")

    print(f"[LEAD] {source} | {name} | {phone} | {business} | {interest}")
    log_event(call_id, "lead-captured", assistant_id,
              f"{name} | {phone} | {business} | source:{source}",
              {"name": name, "phone": phone, "business_type": business, "interest": interest, "source": source})

    # Save to DB so /vapi/call-ended and /demo-complete can look up by call_id
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO leads (call_id, name, phone, business_type, interest, source) VALUES (?,?,?,?,?,?)",
                (call_id, name, phone, business, interest, source)
            )
            conn.commit()
    except Exception as e:
        print(f"[DB] Failed to save lead: {e}")

    client = get_client(assistant_id)
    owner = client.get("owner", OWNER_NUMBER)

    if source == "catch_all":
        sms_body = (
            f"[CallMeIE Custom Lead]\n"
            f"{name} — {phone}\n"
            f"Business: {business}\n"
            f"Needs: {interest}\n"
            f"No demo match — build custom. Ring back today."
        )
    else:
        msg_parts = ["[CallMeIE Lead]"]
        if name:     msg_parts.append(name)
        if phone:    msg_parts.append(phone)
        if business: msg_parts.append(business)
        if interest: msg_parts.append(f"Interest: {interest}")
        msg_parts.append("Demo in progress.")
        sms_body = " — ".join(msg_parts)

    await send_sms(owner, sms_body)

    return _vapi_result(tool_call_id, "Lead saved. Call transferToDemo now. Do not speak.")


# --- Demo complete (called by demo assistants at end of demo) ---
@app.post("/demo-complete")
async def demo_complete(request: Request):
    """
    Demo assistants call this just before saying goodbye.
    Sends the owner an enriched alert: who called, what they asked, how interested.
    """
    body = await request.json()
    tool_call_id, assistant_id, args = _parse_vapi_tool_call(body)

    topics   = args.get("topics_discussed", "").strip()
    interest = args.get("interest_level", "").strip()
    call_id  = body.get("message", {}).get("call", {}).get("id", "")

    demo_type = DEMO_ASSISTANT_IDS.get(assistant_id, "unknown")
    log_event(call_id, "demo-complete", demo_type,
              f"{interest} | {topics}",
              {"topics_discussed": topics, "interest_level": interest, "demo_type": demo_type})

    # Look up and update the lead record
    lead = None
    if call_id:
        try:
            with get_db() as conn:
                lead = conn.execute(
                    "SELECT * FROM leads WHERE call_id = ? ORDER BY created_at DESC LIMIT 1",
                    (call_id,)
                ).fetchone()
                if lead:
                    conn.execute(
                        "UPDATE leads SET demo_completed=1, topics_discussed=?, interest_level=? WHERE call_id=?",
                        (topics, interest, call_id)
                    )
                    conn.commit()
        except Exception as e:
            print(f"[DB] demo_complete error: {e}")

    name  = (lead["name"]  if lead and lead["name"]  else "Unknown")
    phone = (lead["phone"] if lead and lead["phone"] else "Unknown")

    heat = {"very_interested": "🔥 HOT", "curious": "🌡 WARM", "just_browsing": "❄ COLD"}.get(interest, interest)

    sms = (
        f"[CallMeIE Demo Done] {demo_type.upper()} — {heat}\n"
        f"{name} — {phone}\n"
        f"Asked about: {topics or 'n/a'}\n"
        f"Ring back now."
    )
    await send_sms(OWNER_NUMBER, sms)
    print(f"[Demo Complete] {demo_type} | {name} | {interest}")

    return _vapi_result(tool_call_id, "noted")


# --- Client onboarding form submission ---
@app.post("/submit-onboarding")
async def submit_onboarding(request: Request):
    """
    Receives new client onboarding form data.
    Sends owner a detailed SMS + logs full submission to stdout.
    """
    body = await request.json()

    business_name   = body.get("business_name", "")
    contact_name    = body.get("contact_name", "")
    contact_phone   = body.get("contact_phone", "")
    contact_email   = body.get("contact_email", "")
    business_type   = body.get("business_type", "")
    address         = body.get("address", "")
    hours           = body.get("hours", "")
    services        = body.get("services", "")
    emergency_number = body.get("emergency_number", "")
    calendar_email  = body.get("calendar_email", "")
    plan            = body.get("plan", "")
    faqs            = body.get("faqs", "")
    insurance       = body.get("insurance", "")
    ai_name         = body.get("ai_name", "")
    notes           = body.get("notes", "")

    # Log full submission (Render logs are retained)
    print(f"[ONBOARDING] {json.dumps(body, indent=2)}")

    if not business_name:
        return JSONResponse({"error": "business_name required"}, status_code=400)

    # Save to DB for admin review
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO submissions
                (business_name, contact_name, contact_phone, contact_email,
                 business_type, address, hours, services, emergency_number,
                 calendar_email, plan, ai_name, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (business_name, contact_name, contact_phone, contact_email,
                  business_type, address, hours, services, emergency_number,
                  calendar_email, plan, ai_name, notes))
            conn.commit()
    except Exception as e:
        print(f"[DB] Failed to save submission: {e}")

    # SMS 1: headline alert
    alert = (
        f"[CallMeIE] NEW CLIENT: {business_name} ({business_type})\n"
        f"Contact: {contact_name} {contact_phone}\n"
        f"Plan: {plan}\n"
        f"Email: {contact_email}"
    )
    await send_sms(OWNER_NUMBER, alert)

    # SMS 2: setup details (if emergency number and calendar provided)
    if emergency_number or calendar_email:
        setup = (
            f"Setup info for {business_name}:\n"
            f"Emergency: {emergency_number}\n"
            f"Calendar: {calendar_email}\n"
            f"Hours: {hours[:80] if hours else 'TBC'}"
        )
        await send_sms(OWNER_NUMBER, setup)

    return JSONResponse({
        "status": "received",
        "message": f"Thanks {contact_name}! We'll have {business_name} live within 3-5 business days. We'll ring {contact_phone} to confirm.",
    })


# --- Admin portal ---

ASSISTANT_PROMPT = """You are {ai_name}, the receptionist at {business_name} in Ireland.

VOICE RULES — non-negotiable:
- Plain text only. No markdown, bullet points, or numbered lists.
- 1-2 sentences per turn. Never monologue.
- Ask ONE question at a time.
- Sound like a real Irish receptionist. Use: grand, lovely, no bother, sure thing, perfect, cheers.
- Say "ring" not "call". Say "diary" not "calendar". Say "no bother" not "no problem".
- Never sound American. You work in Ireland, for an Irish business.
- Phone numbers: read each digit separately with a dash-pause between each one.
  CORRECT: "zero-eight-five, one-two-three, four-five-six-seven" — pause after every digit group.
  NEVER: continuous strings like "0851234567", plus signs, country codes like "+353", or number words like "one hundred".
  This is critical — garbled numbers mean lost appointments.
- Email addresses: spell naturally — "john dot smith at gmail dot com". Never spell individual letters.
- Dates and times in natural Irish style: "next Tuesday at half ten" not "2026-04-01T10:30".

BOOKING FLOW:
1. Understand what they need
2. Preferred day/time → check diary → offer slots naturally: "We have Tuesday morning at half ten or Thursday at three — which suits you better?"
3. Name: ask, then CONFIRM back — "Just to confirm, that's [name] — is that right?"
4. Phone: ask, then CONFIRM back using the dash format — "And that's zero-eight-five, one-two-three, four-five-six-seven — is that right?"
   Read each digit with a pause between groups. Only proceed once caller confirms both. Wrong details = missed appointment.
5. Book the appointment
6. Confirmation text fires automatically
7. Close warmly: "Lovely, you're all booked in! See you then — bye for now!"
   For first-time visitors add: "If it's your first visit, try to arrive about 10 minutes early."

CONFIRMATION RULE — critical:
Never save a name or phone number without reading it back to the caller first.
If they correct you, update and confirm again before proceeding.

HANDLING EDGE CASES:
- "Can I speak to someone / a real person": "Of course, let me put you through now." → transfer immediately.
- "How much does X cost" / professional advice questions: "The team will go through all of that with you at your appointment."
- Something you don't know: "Let me get someone from the team to ring you back about that — can I take your number?"
- Cancellations: take name + appointment date, say "No bother at all — is there another time that would suit you?"
- Cancellation policy: "We just ask for 24 hours notice if you need to cancel or reschedule."

EMERGENCIES: severe pain, bleeding, broken tooth, swelling, trauma → transfer immediately, don't delay.

BUSINESS INFO:
Hours: {hours}
Address: {address}
Services: {services}

This call may be recorded for quality and training purposes."""


async def provision_client(sub: dict) -> str:
    """Create Vapi assistant + tools for a submission. Returns assistant_id."""
    headers = {
        "Authorization": f"Bearer {VAPI_API_KEY}",
        "Content-Type": "application/json",
    }
    name = sub["business_name"]
    ai_name = sub.get("ai_name") or "Sarah"
    emergency = sub.get("emergency_number", "")
    prompt = ASSISTANT_PROMPT.format(
        ai_name=ai_name,
        business_name=name,
        hours=sub.get("hours", "Monday to Friday 9am to 5:30pm"),
        address=sub.get("address", "Limerick, Ireland"),
        services=sub.get("services", "Please ask us directly"),
    )

    async with httpx.AsyncClient(timeout=30) as h:
        # 1. Create assistant
        r = await h.post("https://api.vapi.ai/assistant", headers=headers, json={
            "name": f"{name} — AI Receptionist",
            "firstMessage": f"Hi, thanks for ringing {name}! This is {ai_name}. How can I help you today?",
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "maxTokens": 250,
                "temperature": 0.7,
                "messages": [{"role": "system", "content": prompt}],
            },
            "voice": {"provider": "11labs", "voiceId": "dN8hviqdNrAsEcL57yFj"},
            "transcriber": {"provider": "deepgram", "model": "nova-3",
                            "language": "en", "smartFormat": True, "numerals": True,
                            "endpointing": 10},
            "serverUrl": f"https://callmeie.onrender.com/vapi/call-ended",
            "endCallPhrases": ["goodbye", "thanks, bye", "cheers", "right, thanks"],
            "maxDurationSeconds": 600,
            "backgroundDenoisingEnabled": True,
        })
        r.raise_for_status()
        assistant_id = r.json()["id"]

        # 2-4. Create per-client tools
        tool_ids = []
        for tool_body in [
            {"type": "google.calendar.availability.check"},
            {"type": "google.calendar.event.create"},
            {"type": "sms"},
            {"type": "transferCall",
             "function": {"name": "transferToEmergency",
                          "description": "Transfer for genuine emergencies only."},
             "destinations": [{"type": "number", "number": emergency,
                                "message": "Transferring you now.",
                                "description": "On-call"}]} if emergency else None,
        ]:
            if not tool_body:
                continue
            r = await h.post("https://api.vapi.ai/tool", headers=headers, json=tool_body)
            if r.status_code in (200, 201):
                tool_ids.append(r.json()["id"])

        # 5. Assign tools to assistant
        await h.patch(f"https://api.vapi.ai/assistant/{assistant_id}", headers=headers, json={
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "maxTokens": 250,
                "temperature": 0.7,
                "messages": [{"role": "system", "content": prompt}],
                "toolIds": tool_ids,
            }
        })

    return assistant_id


@app.get("/admin")
async def admin_portal(token: str = Query("")):
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return FileResponse(os.path.join(os.path.dirname(__file__), "admin.html"))


@app.get("/admin/api/submissions")
async def list_submissions(token: str = Query("")):
    check_admin(token)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/admin/api/clients")
async def list_clients(token: str = Query("")):
    check_admin(token)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM clients ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/admin/api/provision/{submission_id}")
async def provision(submission_id: int, token: str = Query("")):
    check_admin(token)

    with get_db() as conn:
        sub = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()

    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    if sub["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Already {sub['status']}")
    if not VAPI_API_KEY:
        raise HTTPException(status_code=500, detail="VAPI_API_KEY not set")

    sub = dict(sub)
    assistant_id = await provision_client(sub)

    with get_db() as conn:
        conn.execute(
            "UPDATE submissions SET status='provisioned', vapi_assistant_id=?, provisioned_at=? WHERE id=?",
            (assistant_id, datetime.utcnow().isoformat(), submission_id)
        )
        conn.execute("""
            INSERT OR REPLACE INTO clients
            (assistant_id, name, owner_phone, from_number, calendar_id, submission_id)
            VALUES (?, ?, ?, ?, 'primary', ?)
        """, (assistant_id, sub["business_name"], OWNER_NUMBER, TWILIO_FROM, submission_id))
        conn.commit()

    # SMS owner
    await send_sms(OWNER_NUMBER,
        f"[CallMeIE] Provisioned: {sub['business_name']}\n"
        f"Assistant ID: {assistant_id}\n"
        f"Next: share calendar + assign phone number."
    )
    # SMS client (if phone available)
    if sub.get("contact_phone"):
        await send_sms(sub["contact_phone"],
            f"Hi {sub.get('contact_name', 'there')}! Your AI receptionist for "
            f"{sub['business_name']} is almost ready.\n"
            f"Final step: share your Google Calendar with "
            f"{GOOGLE_SA_EMAIL} (give 'Make changes' access).\n"
            f"Ring us if you need help!"
        )

    print(f"[PROVISIONED] {sub['business_name']} → {assistant_id}")
    return {"status": "provisioned", "assistant_id": assistant_id}


@app.get("/admin/api/events")
async def list_events(token: str = Query(""), limit: int = Query(200)):
    check_admin(token)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM call_events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/admin/api/health")
async def client_health(token: str = Query("")):
    """
    Per-client health summary for the ops peer and admin portal.
    Returns stats for every provisioned client + demo assistants.
    Health status: ok | quiet | errors | dead
    """
    check_admin(token)

    DEMO_CLIENTS = {
        "adee3d89-99d8-4f58-9dc3-78c38b9f2a7c": "Claire (qualifier)",
        "0b37deb5-2fc2-4e7b-81b1-e61e97103506": "Demo: Dental",
        "8a533a56-2ca4-486f-b328-69183b59fa41": "Demo: Motor Factors",
        "db4ab378-cd8a-40f5-b3f9-8fcaaba408b0": "Demo: Salon",
        "7774b535-95fe-4e75-b571-dde098e2f8fb": "Demo: Solicitor",
    }

    with get_db() as conn:
        db_clients = conn.execute(
            "SELECT assistant_id, name FROM clients WHERE status = 'active'"
        ).fetchall()

    all_clients = {row["assistant_id"]: row["name"] for row in db_clients}
    all_clients.update(DEMO_CLIENTS)

    results = []
    with get_db() as conn:
        for aid, name in all_clients.items():
            row = conn.execute("""
                SELECT
                    COUNT(*)                                                  AS total,
                    MAX(created_at)                                           AS last_event,
                    SUM(event_type = 'call-ended')                           AS calls,
                    SUM(event_type = 'booking')                              AS bookings,
                    SUM(event_type IN ('lead-captured', 'demo-complete'))     AS leads,
                    SUM(event_type = 'lead-error')                           AS errors
                FROM call_events
                WHERE assistant = ?
                AND   created_at > datetime('now', '-7 days')
            """, (aid,)).fetchone()

            last = row["last_event"]
            days_silent = None
            if last:
                try:
                    from datetime import timezone
                    last_dt = datetime.fromisoformat(last)
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    days_silent = (now - last_dt).days
                except Exception:
                    pass

            if row["errors"] and row["errors"] > 0:
                health = "errors"
            elif days_silent is None or days_silent > 5:
                health = "dead"
            elif days_silent > 2:
                health = "quiet"
            else:
                health = "ok"

            is_demo = aid in DEMO_CLIENTS
            results.append({
                "assistant_id": aid,
                "name": name,
                "is_demo": is_demo,
                "health": health,
                "last_event": last,
                "days_silent": days_silent,
                "calls_7d": row["calls"] or 0,
                "bookings_7d": row["bookings"] or 0,
                "leads_7d": row["leads"] or 0,
                "errors_7d": row["errors"] or 0,
            })

    # Real clients first, demos last
    results.sort(key=lambda x: (x["is_demo"], x["name"]))
    return results


@app.get("/admin/api/diagnoses")
async def list_diagnoses(token: str = Query(""), limit: int = Query(50)):
    """Recent anomaly diagnoses — for GM peer and admin portal."""
    check_admin(token)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM call_diagnostics ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/admin/api/reject/{submission_id}")
async def reject(submission_id: int, token: str = Query("")):
    check_admin(token)
    with get_db() as conn:
        conn.execute(
            "UPDATE submissions SET status='rejected' WHERE id = ?", (submission_id,)
        )
        conn.commit()
    return {"status": "rejected"}


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
