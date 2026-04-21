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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from google.oauth2 import service_account
from googleapiclient.discovery import build

sys.path.insert(0, os.path.dirname(__file__))

app = FastAPI(title="CallMeIE — AI Receptionist Server")
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
ADMIN_HTML_PATH = os.path.join(_SCRIPTS_DIR, "admin.html")
INDEX_HTML_PATH = os.path.join(_REPO_ROOT, "index.html")
ONBOARD_HTML_PATH = os.path.join(_REPO_ROOT, "onboard.html")
PRIVACY_HTML_PATH = os.path.join(_REPO_ROOT, "privacy.html")
TERMS_HTML_PATH = os.path.join(_REPO_ROOT, "terms.html")
FAVICON_SVG = b"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>
  <rect width='64' height='64' rx='16' fill='#0f172a'/>
  <path d='M19 41V23h6.5c4.6 0 8 2.9 8 9s-3.4 9-8 9H19Zm5.2-4h1c2.9 0 4.3-1.8 4.3-5s-1.4-5-4.3-5h-1v10Z' fill='#22d3ee'/>
  <path d='M34.5 34.2c0-5.4 3.3-8.8 8.2-8.8 4.8 0 7.4 2.7 7.6 6.5h-4.7c-.2-1.7-1.3-2.7-3-2.7-2.7 0-4.1 2.2-4.1 5s1.4 5 4.1 5c1.9 0 3.1-1.1 3.3-2.9h4.7c-.2 4-3 6.8-7.9 6.8-4.9 0-8.2-3.3-8.2-8.9Z' fill='#ffffff'/>
</svg>"""
ADMIN_HTML_FALLBACK = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CallMeIE Admin</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 40px; color: #0f172a; background: #f8fafc; }
    .card { max-width: 640px; background: #fff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 24px; box-shadow: 0 8px 32px rgba(15, 23, 42, 0.08); }
    h1 { margin: 0 0 12px; }
    p { line-height: 1.6; color: #475569; }
    code { background: #e2e8f0; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>CallMeIE Admin</h1>
    <p>The full admin portal asset was not packaged into this deployment, but the authenticated admin API is still available.</p>
    <p>Use <code>/admin/api/submissions?token=...</code>, <code>/admin/api/clients?token=...</code>, and the other admin endpoints to inspect or provision clients.</p>
  </div>
</body>
</html>
"""

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


@app.get("/favicon.svg")
async def favicon_svg():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.get("/favicon.ico")
async def favicon_ico():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")

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
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
CALLMEIE_CALLBACK_CALENDAR_ID = os.environ.get("CALLMEIE_CALLBACK_CALENDAR_ID", "primary")
CALLMEIE_TIMEZONE = os.environ.get("CALLMEIE_TIMEZONE", "Europe/Dublin")
CALLMEIE_BACKUP_SHEET_ID = os.environ.get("CALLMEIE_BACKUP_SHEET_ID", "")
CALLMEIE_BACKUP_SHEET_TAB = os.environ.get("CALLMEIE_BACKUP_SHEET_TAB", "submissions")

# --- Telegram notifications ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
BACKUP_SHEET_HEADERS = [
    "submitted_at_utc",
    "business_name",
    "contact_name",
    "contact_phone",
    "contact_email",
    "business_type",
    "address",
    "hours",
    "services",
    "emergency_number",
    "calendar_email",
    "plan",
    "ai_name",
    "notes",
]

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
    "3e2f8e1c-e4eb-46ab-b8be-d7f97cbe6080": "general business discovery",
}

# --- DB adapter (Postgres via DATABASE_URL, else SQLite) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
_USE_PG = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")
if _USE_PG:
    # psycopg v3 requires the postgresql:// prefix, not postgres://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    try:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
    except ImportError:
        # If psycopg isn't installed, degrade to SQLite so server still boots
        _USE_PG = False
        print("[warn] DATABASE_URL set but psycopg not installed — falling back to SQLite", file=sys.stderr)

DB_PATH = os.environ.get("DB_PATH", "/var/data/callmeie.db")


def _ddl_fix(sql: str) -> str:
    """Translate SQLite-flavour DDL to Postgres where they differ."""
    if not _USE_PG:
        return sql
    out = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    out = out.replace("datetime('now')", "NOW()")
    # datetime typed columns -> timestamp
    import re as _re
    out = _re.sub(r"\bDATETIME\b", "TIMESTAMP", out)
    return out


class _DbProxy:
    """SQLite-like wrapper over either sqlite3 or psycopg3.

    Same call-surface as sqlite3.Connection:
      - conn.execute(sql, params) -> cursor-like object with fetchone/fetchall
      - conn.commit(), conn.close()
      - context manager support; rows dict-accessible in both backends
    Translates '?' placeholders to '%s' on Postgres path. DDL normalisation
    handled via `_ddl_fix()` wrapping every CREATE TABLE / INDEX call.
    """
    def __init__(self) -> None:
        if _USE_PG:
            self._c = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
        else:
            db_dir = os.path.dirname(DB_PATH)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            self._c = sqlite3.connect(DB_PATH)
            self._c.row_factory = sqlite3.Row

    def execute(self, sql: str, params=()):
        if _USE_PG:
            cur = self._c.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self._c.execute(sql, params)

    def commit(self) -> None:
        self._c.commit()

    def close(self) -> None:
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self._c.commit()
            except Exception:
                pass
        else:
            try:
                self._c.rollback()
            except Exception:
                pass
        self._c.close()


# Exception type to catch on constraint violations, unified across backends
if _USE_PG:
    _DbIntegrityError = (sqlite3.IntegrityError, psycopg.errors.IntegrityError)  # type: ignore
else:
    _DbIntegrityError = (sqlite3.IntegrityError,)


def get_db() -> "_DbProxy":
    return _DbProxy()


def _load_google_credentials():
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        return service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
    except Exception as e:
        print(f"[Calendar] Failed to load service account credentials: {e}")
        return None


def _load_sheets_service(require_sheet_id: bool = True):
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    if require_sheet_id and not CALLMEIE_BACKUP_SHEET_ID:
        return None
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except Exception as e:
        print(f"[Sheets] Failed to load service account credentials: {e}")
        return None


def backup_sheet_status() -> dict:
    """Summarize the current Google Sheets backup configuration."""
    return {
        "configured": bool(GOOGLE_SERVICE_ACCOUNT_JSON and CALLMEIE_BACKUP_SHEET_ID),
        "has_service_account": bool(GOOGLE_SERVICE_ACCOUNT_JSON),
        "has_sheet_id": bool(CALLMEIE_BACKUP_SHEET_ID),
        "sheet_tab": CALLMEIE_BACKUP_SHEET_TAB,
    }


def bootstrap_backup_sheet() -> dict | None:
    """
    Initialize the backup Google Sheet with headers.

    If CALLMEIE_BACKUP_SHEET_ID is already set, writes headers to the existing
    sheet (requires the sheet to be shared with the service account). Otherwise
    attempts to create a new sheet.

    Returns a small metadata payload with the sheet ID and URL, or None if the
    service account is unavailable.
    """
    service = _load_sheets_service(require_sheet_id=False)
    if service is None:
        return None

    try:
        if CALLMEIE_BACKUP_SHEET_ID:
            # Use the existing sheet — just ensure the tab exists and write headers.
            spreadsheet_id = CALLMEIE_BACKUP_SHEET_ID
            meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
            existing_titles = [
                s["properties"]["title"] for s in meta.get("sheets", [])
            ]
            tab_title = CALLMEIE_BACKUP_SHEET_TAB or "submissions"

            if tab_title not in existing_titles:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": [{"addSheet": {"properties": {"title": tab_title}}}]},
                ).execute()
        else:
            spreadsheet = service.spreadsheets().create(
                body={"properties": {"title": "CallMeIE Onboarding Backups"}}
            ).execute()
            spreadsheet_id = spreadsheet["spreadsheetId"]
            sheets = spreadsheet.get("sheets", [])
            first_sheet = sheets[0].get("properties", {}) if sheets else {}
            default_sheet_id = first_sheet.get("sheetId")
            default_sheet_title = first_sheet.get("title", "Sheet1")
            tab_title = CALLMEIE_BACKUP_SHEET_TAB or default_sheet_title

            if default_sheet_id and tab_title != default_sheet_title:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={
                        "requests": [
                            {
                                "updateSheetProperties": {
                                    "properties": {
                                        "sheetId": default_sheet_id,
                                        "title": tab_title,
                                    },
                                    "fields": "title",
                                }
                            }
                        ]
                    },
                ).execute()
            else:
                tab_title = default_sheet_title

        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A1:N1",
            valueInputOption="RAW",
            body={"values": [BACKUP_SHEET_HEADERS]},
        ).execute()

        return {
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
            "sheet_tab": tab_title,
        }
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}") from e


def backup_submission_to_sheet(submission: dict) -> bool:
    """Append an onboarding submission to Google Sheets as an external backup."""
    service = _load_sheets_service()
    if service is None:
        return False

    row = [
        datetime.utcnow().isoformat(),
        submission.get("business_name", ""),
        submission.get("contact_name", ""),
        submission.get("contact_phone", ""),
        submission.get("contact_email", ""),
        submission.get("business_type", ""),
        submission.get("address", ""),
        submission.get("hours", ""),
        submission.get("services", ""),
        submission.get("emergency_number", ""),
        submission.get("calendar_email", ""),
        submission.get("plan", ""),
        submission.get("ai_name", ""),
        submission.get("notes", ""),
    ]

    try:
        service.spreadsheets().values().append(
            spreadsheetId=CALLMEIE_BACKUP_SHEET_ID,
            range=f"{CALLMEIE_BACKUP_SHEET_TAB}!A:N",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return True
    except Exception as e:
        print(f"[Sheets] Failed to back up submission: {e}")
        return False


def _next_business_callback(interest: str) -> datetime:
    now_local = datetime.now(ZoneInfo(CALLMEIE_TIMEZONE))
    candidate = now_local.replace(
        hour=14 if interest == "curious" else 10,
        minute=0,
        second=0,
        microsecond=0,
    )
    if candidate <= now_local:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def create_callback_event(
    name: str,
    phone: str,
    business_type: str,
    interest: str,
    topics: str,
    demo_type: str,
    call_id: str,
    pain_point: str = "",
    estimated_missed_calls_per_week: str = "",
    next_action: str = "",
):
    credentials = _load_google_credentials()
    if credentials is None or not CALLMEIE_CALLBACK_CALENDAR_ID:
        return None

    start_local = _next_business_callback(interest)
    end_local = start_local + timedelta(minutes=15)
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    event = {
        "summary": f"Call back {name or 'Unknown'} - {business_type or demo_type or 'demo lead'}",
        "description": "\n".join(
            [
                f"Lead: {name or 'Unknown'}",
                f"Phone: {phone or 'Unknown'}",
                f"Business: {business_type or 'Unknown'}",
                f"Demo type: {demo_type or 'Unknown'}",
                f"Interest: {interest or 'Unknown'}",
                f"Asked about: {topics or 'n/a'}",
                f"Pain point: {pain_point or 'n/a'}",
                f"Missed calls/week: {estimated_missed_calls_per_week or 'n/a'}",
                f"Next action: {next_action or 'n/a'}",
                f"Call ID: {call_id or 'n/a'}",
                "Created automatically from CallMeIE /demo-complete.",
            ]
        ),
        "start": {"dateTime": start_local.isoformat(), "timeZone": CALLMEIE_TIMEZONE},
        "end": {"dateTime": end_local.isoformat(), "timeZone": CALLMEIE_TIMEZONE},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 5},
            ],
        },
    }
    created = (
        service.events()
        .insert(calendarId=CALLMEIE_CALLBACK_CALENDAR_ID, body=event, sendUpdates="none")
        .execute()
    )
    return {
        "event_id": created.get("id", ""),
        "html_link": created.get("htmlLink", ""),
    }


def init_db():
    with get_db() as conn:
        conn.execute(_ddl_fix("""
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
        """))
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS call_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                call_id    TEXT,
                event_type TEXT,
                assistant  TEXT,
                summary    TEXT,
                detail     TEXT
            )
        """))
        conn.execute(_ddl_fix("""
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
                interest_level   TEXT,
                pain_point       TEXT,
                estimated_missed_calls_per_week TEXT,
                next_action      TEXT,
                callback_requested INTEGER DEFAULT 0
            )
        """))
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS call_diagnostics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now')),
                call_id     TEXT UNIQUE,
                assistant   TEXT,
                score       REAL,
                diagnosis   TEXT,
                action      TEXT
            )
        """))
        conn.execute(_ddl_fix("""
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
        """))
        # Dialect-specific column introspection for ALTER TABLE idempotency
        if _USE_PG:
            existing_columns = {
                row["column_name"] for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'leads'"
                ).fetchall()
            }
        else:
            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()
            }
        lead_column_migrations = {
            "pain_point": "ALTER TABLE leads ADD COLUMN pain_point TEXT",
            "estimated_missed_calls_per_week": "ALTER TABLE leads ADD COLUMN estimated_missed_calls_per_week TEXT",
            "next_action": "ALTER TABLE leads ADD COLUMN next_action TEXT",
            "callback_requested": "ALTER TABLE leads ADD COLUMN callback_requested INTEGER DEFAULT 0",
        }
        for column, sql in lead_column_migrations.items():
            if column not in existing_columns:
                try:
                    conn.execute(sql)
                except Exception as e:
                    # Postgres rejects duplicate ALTER - fine, column already exists
                    print(f"[init_db] skipped migration '{column}': {e}", file=sys.stderr)
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
    Score how anomalous a call-ended event is (0.0â1.0).
    Anything >= ANOMALY_THRESHOLD gets queued for Claude diagnosis.

    Thresholds based on industry benchmarks:
    - Dental/salon average call duration for booking: 60â180s
    - <10s = almost certainly a technical failure (not a real interaction)
    - <30s = dropped or AI confused (too short for any real booking conversation)
    - missed/no-answer: standard SMB miss rate is 30â35%; individual events are normal
      but combined with very short duration they signal infrastructure failure
    """
    score = 0.0
    if status in ("missed", "no-answer"):
        score += 0.4
    if status == "failed":
        score += 0.6
    if not is_demo:
        if duration < 10:
            score += 0.5   # almost certainly technical failure â not a real interaction
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
        print(f"[Diag] No ANTHROPIC_API_KEY â skipping diagnosis for {call_id}")
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
    """Return client config â DB first, fallback to CLIENTS env var."""
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
        return {"status": "mocked", "ok": True, "http_status": 200}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"To": to, "From": sender, "Body": body},
        )
        try:
            result = resp.json()
        except Exception:
            result = {"status": "failed", "message": resp.text[:200]}
        if resp.status_code not in (200, 201):
            print(f"[SMS ERROR] {resp.status_code}: {result}")
        status = result.get("status", "") if isinstance(result, dict) else ""
        ok = resp.status_code in (200, 201) and status not in ("failed", "undelivered")
        if not ok and isinstance(result, dict):
            result.setdefault("status", "failed")
        return {
            **(result if isinstance(result, dict) else {"result": result}),
            "ok": ok,
            "http_status": resp.status_code,
        }


# --- Telegram ---
async def send_telegram(message: str) -> None:
    """Send a Telegram message to TELEGRAM_CHAT_ID via the configured bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM MOCK] {message[:120]}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})
            if resp.status_code != 200:
                print(f"[TELEGRAM ERROR] {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")


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

    if call_id:
        try:
            with get_db() as conn:
                duplicate = conn.execute(
                    "SELECT 1 FROM call_events WHERE call_id=? AND event_type='call-ended' LIMIT 1",
                    (call_id,),
                ).fetchone()
            if duplicate:
                print(f"[Call] duplicate call-ended webhook ignored for {call_id}")
                return JSONResponse({"status": "ok", "duplicate": True})
        except Exception as e:
            print(f"[Call] dedupe check failed: {e}")

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
            f"We're here to help â reply to this text or ring us back. "
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
    assistant_id = body.get("assistant_id", "")
    call_id = body.get("call_id", "")
    client = get_client(assistant_id) if assistant_id else {"owner": OWNER_NUMBER, "from": TWILIO_FROM, "name": business}
    from_num = body.get("from_number", client.get("from", TWILIO_FROM))
    owner = client.get("owner", OWNER_NUMBER)

    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)

    sms_result = await send_sms(
        phone,
        f"Hi {name}! Reminder: appointment at {business} "
        f"tomorrow ({date}) at {time}. Please arrive 10 min early. "
        f"Reply CANCEL to cancel or STOP to opt out.",
        from_number=from_num,
    )
    sms_status = sms_result.get("status", "") if isinstance(sms_result, dict) else ""
    sms_ok = sms_result.get("ok", True) if isinstance(sms_result, dict) else True
    log_event(
        call_id,
        "reminder",
        assistant_id or business,
        f"{phone} | {date} {time} | {sms_status or 'sent'}",
        {
            "phone": phone,
            "name": name,
            "business": business,
            "date": date,
            "time": time,
            "twilio_status": sms_status or "sent",
        },
    )
    if (not sms_ok or sms_status in ("failed", "undelivered")) and owner:
        await send_sms(
            owner,
            f"[CallMeIE] Reminder SMS FAILED for {name or 'unknown'} ({phone}) at {business} "
            f"for {date} {time}. Ring them manually.",
            from_number=TWILIO_FROM,
        )
    return JSONResponse({"status": "sent", "twilio_status": sms_status or "sent"})


# --- No-show follow-up ---
@app.post("/no-show")
async def no_show(request: Request):
    """Send no-show follow-up. Called by external scheduler."""
    body = await request.json()
    phone = body.get("phone", "")
    name = body.get("name", "")
    business = body.get("business", "the practice")
    assistant_id = body.get("assistant_id", "")
    call_id = body.get("call_id", "")
    client = get_client(assistant_id) if assistant_id else {"owner": OWNER_NUMBER, "from": TWILIO_FROM, "name": business}
    from_num = body.get("from_number", client.get("from", TWILIO_FROM))
    owner = client.get("owner", OWNER_NUMBER)

    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)

    sms_result = await send_sms(
        phone,
        f"Hi {name}! We missed you at {business} today. "
        f"No worries â reply to reschedule. Reply STOP to opt out.",
        from_number=from_num,
    )
    sms_status = sms_result.get("status", "") if isinstance(sms_result, dict) else ""
    sms_ok = sms_result.get("ok", True) if isinstance(sms_result, dict) else True
    log_event(
        call_id,
        "no-show",
        assistant_id or business,
        f"{phone} | {sms_status or 'sent'}",
        {
            "phone": phone,
            "name": name,
            "business": business,
            "twilio_status": sms_status or "sent",
        },
    )
    if (not sms_ok or sms_status in ("failed", "undelivered")) and owner:
        await send_sms(
            owner,
            f"[CallMeIE] No-show follow-up SMS FAILED for {name or 'unknown'} ({phone}) at {business}. "
            f"Ring them manually.",
            from_number=TWILIO_FROM,
        )
    return JSONResponse({"status": "sent", "twilio_status": sms_status or "sent"})


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
        print(f"[Availability] {date_str} â {len(slots)} slots for {business}")
        log_event(call_id, "avail-check", assistant_id,
                  f"{date_str} â {len(slots)} slots",
                  {"date": date_str, "slots_found": len(slots), "business": business})

        # Alert if 3+ avail-checks on this call with no booking yet (calendar full or AI looping)
        # Industry benchmark: normal booking = 1â2 checks; 3+ = something is wrong
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
                              f"{check_count} checks, no booking â calendar full or AI looping",
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
        # Alert owner immediately â calendar access broken = silent revenue loss
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
        customer_email = args.get("customer_email", "").strip()
        start_iso = args.get("start_iso", "")
        end_iso = args.get("end_iso", "")
        title = args.get("title", "Appointment")
        notes = args.get("notes", "").strip()

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
            customer_email=customer_email,
            notes=notes,
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
            # Every booking confirmation failure is individually significant â a patient
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
                        f"at {business} â {readable}. Ring them to confirm manually.",
                        from_number=TWILIO_FROM,
                    )
            else:
                log_event(call_id, "sms-booking-confirmation", assistant_id,
                          f"{customer_phone} | {sms_status or 'sent'}",
                          {"to": customer_phone, "twilio_status": sms_status or "sent", "name": customer_name})
        if owner:
            await send_sms(
                owner,
                f"[{business}] New booking: {customer_name} ({customer_phone}) â {readable}",
                from_number=from_num,
            )

        print(f"[Booking] {customer_name} at {business} â {readable}")
        log_event(call_id, "booking", assistant_id,
                  f"{customer_name} | {readable}",
                  {
                      "name": customer_name,
                      "phone": customer_phone,
                      "email": customer_email,
                      "time": readable,
                      "business": business,
                      "event_id": event.get("id", ""),
                      "event_link": event.get("link", ""),
                      "notes": notes,
                  })
        return _vapi_result(
            tool_call_id,
            f"Perfect, {customer_name}! Your appointment at {business} is confirmed for {readable}. "
            f"We'll send a confirmation text to {customer_phone}. Is there anything else I can help you with?"
        )

    except ImportError:
        await send_sms(
            OWNER_NUMBER,
            f"[CallMeIE] Booking failed for {get_client(assistant_id)['name']} because calendar_api could not load.",
            from_number=TWILIO_FROM,
        )
        return _vapi_result(tool_call_id, "Calendar system is temporarily unavailable. Please call us to reschedule.")
    except Exception as e:
        print(f"[Booking Error] {e}")
        log_event(call_id, "booking-fail", assistant_id, str(e)[:120], {"error": str(e)})
        await send_sms(
            OWNER_NUMBER,
            f"[CallMeIE] Booking failed for {get_client(assistant_id)['name']}.\n"
            f"Caller: {customer_name or 'Unknown'} ({customer_phone or 'Unknown'})\n"
            f"Error: {str(e)[:120]}",
            from_number=TWILIO_FROM,
        )
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
            f"{name} â {phone}\n"
            f"Business: {business}\n"
            f"Needs: {interest}\n"
            f"No demo match â build custom. Ring back today."
        )
    else:
        msg_parts = ["[CallMeIE Lead]"]
        if name:     msg_parts.append(name)
        if phone:    msg_parts.append(phone)
        if business: msg_parts.append(business)
        if interest: msg_parts.append(f"Interest: {interest}")
        msg_parts.append("Demo in progress.")
        sms_body = " â ".join(msg_parts)

    await send_sms(owner, sms_body)

    # Telegram alert
    label = "Custom Lead" if source == "catch_all" else "Demo Lead"
    tg_parts = [f"📞 <b>{label}: {name or 'Unknown'}</b>", phone]
    if business:
        tg_parts.append(f"Business: {business}")
    if interest:
        tg_parts.append(f"Needs: {interest}")
    await send_telegram("\n".join(tg_parts))

    # Tell the LLM exactly which handoff tool to call next
    biz = business.lower()
    if any(w in biz for w in ["dental", "dentist", "clinic", "medical", "health"]):
        next_tool = "transfer_dental_demo"
    elif any(w in biz for w in ["motor", "garage", "mechanic", "car", "auto", "parts"]):
        next_tool = "transfer_motor_factors_demo"
    elif any(w in biz for w in ["salon", "beauty", "hair", "barber", "nail", "spa"]):
        next_tool = "transfer_salon_demo"
    elif any(w in biz for w in ["solicitor", "lawyer", "legal", "law"]):
        next_tool = "transfer_solicitor_demo"
    else:
        next_tool = None

    if next_tool:
        instruction = f"Lead saved. Call {next_tool} now. Do not speak."
    else:
        instruction = "Lead saved. This is a catch-all lead â end the call politely."

    return _vapi_result(tool_call_id, instruction)


# --- Demo complete (called by demo assistants at end of demo) ---
@app.post("/demo-complete")
async def demo_complete(request: Request):
    """
    Demo assistants call this just before saying goodbye.
    Sends the owner an enriched alert: who called, what they asked, how interested.
    """
    body = await request.json()
    tool_call_id, assistant_id, args = _parse_vapi_tool_call(body)

    topics = args.get("topics_discussed", "").strip()
    interest = args.get("interest_level", "").strip()
    business_type_arg = args.get("business_type", "").strip()
    pain_point = args.get("pain_point", "").strip()
    estimated_missed_calls = str(args.get("estimated_missed_calls_per_week", "")).strip()
    next_action = args.get("next_action", "").strip()
    callback_requested = bool(args.get("callback_requested", False))
    call_id = body.get("message", {}).get("call", {}).get("id", "")

    demo_type = DEMO_ASSISTANT_IDS.get(assistant_id, "unknown")
    log_event(call_id, "demo-complete", demo_type,
              f"{interest} | {topics}",
              {
                  "topics_discussed": topics,
                  "interest_level": interest,
                  "demo_type": demo_type,
                  "business_type": business_type_arg,
                  "pain_point": pain_point,
                  "estimated_missed_calls_per_week": estimated_missed_calls,
                  "next_action": next_action,
                  "callback_requested": callback_requested,
              })

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
                        """
                        UPDATE leads
                        SET demo_completed=1,
                            topics_discussed=?,
                            interest_level=?,
                            pain_point=?,
                            estimated_missed_calls_per_week=?,
                            next_action=?,
                            callback_requested=?
                        WHERE call_id=?
                        """,
                        (
                            topics,
                            interest,
                            pain_point,
                            estimated_missed_calls,
                            next_action,
                            1 if callback_requested else 0,
                            call_id,
                        )
                    )
                    conn.commit()
        except Exception as e:
            print(f"[DB] demo_complete error: {e}")

    name  = (lead["name"]  if lead and lead["name"]  else "Unknown")
    phone = (lead["phone"] if lead and lead["phone"] else "Unknown")
    business_type = business_type_arg or (lead["business_type"] if lead and lead["business_type"] else demo_type)

    callback_event = None
    callback_error = ""
    should_create_callback = interest in ("very_interested", "curious") or callback_requested
    if should_create_callback:
        try:
            callback_event = create_callback_event(
                name=name,
                phone=phone,
                business_type=business_type,
                interest=interest,
                topics=topics,
                demo_type=demo_type,
                call_id=call_id,
                pain_point=pain_point,
                estimated_missed_calls_per_week=estimated_missed_calls,
                next_action=next_action,
            )
        except Exception as e:
            callback_error = str(e)
            print(f"[Calendar] demo_complete callback error: {e}")

    heat = {"very_interested": "ð¥ HOT", "curious": "ð¡ WARM", "just_browsing": "â COLD"}.get(interest, interest)

    sms = (
        f"[CallMeIE Demo Done] {demo_type.upper()} â {heat}\n"
        f"{name} â {phone}\n"
        f"Business: {business_type or 'n/a'}\n"
        f"Asked about: {topics or 'n/a'}\n"
        f"Pain point: {pain_point or 'n/a'}\n"
        f"Next action: {next_action or 'n/a'}\n"
        f"{'Callback calendar event created.' if callback_event else ('Callback calendar not configured.' if not callback_error and should_create_callback else 'No callback created.')}"
    )
    await send_sms(OWNER_NUMBER, sms)
    log_event(
        call_id,
        "callback-calendar",
        demo_type,
        "created" if callback_event else ("error" if callback_error else "skipped"),
        {
            "interest_level": interest,
            "callback_calendar_configured": bool(CALLMEIE_CALLBACK_CALENDAR_ID),
            "callback_event": callback_event or {},
            "error": callback_error,
            "business_type": business_type,
            "pain_point": pain_point,
            "estimated_missed_calls_per_week": estimated_missed_calls,
            "next_action": next_action,
            "callback_requested": callback_requested,
        },
    )
    print(f"[Demo Complete] {demo_type} | {name} | {interest} | callback={'yes' if callback_event else 'no'}")

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

    backup_submission_to_sheet({
        "business_name": business_name,
        "contact_name": contact_name,
        "contact_phone": contact_phone,
        "contact_email": contact_email,
        "business_type": business_type,
        "address": address,
        "hours": hours,
        "services": services,
        "emergency_number": emergency_number,
        "calendar_email": calendar_email,
        "plan": plan,
        "ai_name": ai_name,
        "notes": notes,
    })

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

    # Telegram alert
    tg_msg = (
        f"🆕 <b>New onboarding: {business_name}</b>\n"
        f"Type: {business_type} | Plan: {plan}\n"
        f"Contact: {contact_name} · {contact_phone}\n"
        f"Email: {contact_email}"
    )
    if ai_name:
        tg_msg += f"\nAI name: {ai_name}"
    await send_telegram(tg_msg)

    return JSONResponse({
        "status": "received",
        "message": f"Thanks {contact_name}! We'll have {business_name} live within 3-5 business days. We'll ring {contact_phone} to confirm.",
    })


# --- Admin portal ---

ASSISTANT_PROMPT = """You are {ai_name}, the receptionist at {business_name} in Ireland.

VOICE RULES â non-negotiable:
- Plain text only. No markdown, bullet points, or numbered lists.
- 1-2 sentences per turn. Never monologue.
- Ask ONE question at a time.
- Sound like a real Irish receptionist. Use: grand, lovely, no bother, sure thing, perfect, cheers.
- Say "ring" not "call". Say "diary" not "calendar". Say "no bother" not "no problem".
- Never sound American. You work in Ireland, for an Irish business.
- Phone numbers: read each digit separately with a dash-pause between each one.
  CORRECT: "zero-eight-five, one-two-three, four-five-six-seven" â pause after every digit group.
  NEVER: continuous strings like "0851234567", plus signs, country codes like "+353", or number words like "one hundred".
  This is critical â garbled numbers mean lost appointments.
- Email addresses: spell naturally â "john dot smith at gmail dot com". Never spell individual letters.
- Dates and times in natural Irish style: "next Tuesday at half ten" not "2026-04-01T10:30".

BOOKING FLOW:
1. Understand what they need
2. Preferred day/time â check diary â offer slots naturally: "We have Tuesday morning at half ten or Thursday at three â which suits you better?"
3. Name: ask, then CONFIRM back â "Just to confirm, that's [name] â is that right?"
4. Phone: ask, then CONFIRM back using the dash format â "And that's zero-eight-five, one-two-three, four-five-six-seven â is that right?"
   Read each digit with a pause between groups. Only proceed once caller confirms both. Wrong details = missed appointment.
5. Book the appointment
6. Confirmation text fires automatically
7. Close warmly: "Lovely, you're all booked in! See you then â bye for now!"
   For first-time visitors add: "If it's your first visit, try to arrive about 10 minutes early."

CONFIRMATION RULE â critical:
Never save a name or phone number without reading it back to the caller first.
If they correct you, update and confirm again before proceeding.

HANDLING EDGE CASES:
- "Can I speak to someone / a real person": "Of course, let me put you through now." â transfer immediately.
- "How much does X cost" / professional advice questions: "The team will go through all of that with you at your appointment."
- Something you don't know: "Let me get someone from the team to ring you back about that â can I take your number?"
- Cancellations: take name + appointment date, say "No bother at all â is there another time that would suit you?"
- Cancellation policy: "We just ask for 24 hours notice if you need to cancel or reschedule."

EMERGENCIES: severe pain, bleeding, broken tooth, swelling, trauma â transfer immediately, don't delay.

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
            "name": f"{name} â AI Receptionist",
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


@app.get("/")
async def index():
    if os.path.exists(INDEX_HTML_PATH):
        return FileResponse(INDEX_HTML_PATH)
    return HTMLResponse("<h1>CallMe.ie</h1>")


@app.get("/onboard.html")
async def onboard():
    if os.path.exists(ONBOARD_HTML_PATH):
        return FileResponse(ONBOARD_HTML_PATH)
    return HTMLResponse("<h1>Onboarding coming soon</h1>")


@app.get("/privacy")
@app.get("/privacy.html")
async def privacy():
    if os.path.exists(PRIVACY_HTML_PATH):
        return FileResponse(PRIVACY_HTML_PATH)
    return HTMLResponse("<h1>Privacy Policy</h1>")


@app.get("/terms")
@app.get("/terms.html")
async def terms():
    if os.path.exists(TERMS_HTML_PATH):
        return FileResponse(TERMS_HTML_PATH)
    return HTMLResponse("<h1>Terms of Service</h1>")


@app.get("/admin")
async def admin_portal(token: str = Query("")):
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if os.path.exists(ADMIN_HTML_PATH):
        return FileResponse(ADMIN_HTML_PATH)
    return HTMLResponse(ADMIN_HTML_FALLBACK)


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


@app.get("/admin/api/backup-sheet/status")
async def backup_sheet_status_endpoint(token: str = Query("")):
    check_admin(token)
    return backup_sheet_status()


@app.post("/admin/api/backup-sheet/bootstrap")
async def bootstrap_backup_sheet_endpoint(token: str = Query("")):
    check_admin(token)
    try:
        return bootstrap_backup_sheet()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to bootstrap backup sheet: {e}",
        ) from e


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

    print(f"[PROVISIONED] {sub['business_name']} â {assistant_id}")
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
    """Recent anomaly diagnoses â for GM peer and admin portal."""
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
        "google_backup": backup_sheet_status(),
    }


# =========================================================================
# OWL STUDIO routes — multi-tenant lead/ticket backend for client sites
# See PDR-BACKEND.md in owl-studio-website-directions repo.
# =========================================================================

import secrets as _secrets

OWL_OWNER_TOKEN = os.environ.get("OWL_OWNER_TOKEN", "").strip()


def _owl_init_tables() -> None:
    """Create the Owl Studio tables if they don't exist yet."""
    with get_db() as conn:
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS owl_sites (
                site_id            TEXT PRIMARY KEY,
                display_name       TEXT NOT NULL,
                tier               TEXT NOT NULL,
                care_tier          TEXT,
                lead_email         TEXT NOT NULL,
                lead_sms           TEXT,
                edit_emails        TEXT NOT NULL DEFAULT '[]',
                admin_token        TEXT NOT NULL,
                live_url           TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'active',
                created_at         TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS owl_leads (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id          TEXT NOT NULL,
                ts               TEXT NOT NULL DEFAULT (datetime('now')),
                form_type        TEXT NOT NULL DEFAULT 'contact',
                payload_json     TEXT NOT NULL,
                submitter_ip     TEXT,
                submitted_from   TEXT,
                status           TEXT NOT NULL DEFAULT 'new'
            )
        """))
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS owl_tickets (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id          TEXT NOT NULL,
                ts               TEXT NOT NULL DEFAULT (datetime('now')),
                submitter_email  TEXT NOT NULL,
                subject          TEXT NOT NULL,
                body             TEXT NOT NULL,
                priority         TEXT NOT NULL DEFAULT 'normal',
                status           TEXT NOT NULL DEFAULT 'open',
                sla_due          TEXT
            )
        """))
        conn.execute(_ddl_fix("CREATE INDEX IF NOT EXISTS idx_owl_leads_site_ts ON owl_leads(site_id, ts)"))
        conn.execute(_ddl_fix("CREATE INDEX IF NOT EXISTS idx_owl_tickets_site_ts ON owl_tickets(site_id, ts)"))


_owl_init_tables()


def _owl_site_by_id(site_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM owl_sites WHERE site_id = ? AND status = 'active'",
            (site_id,),
        ).fetchone()
    return dict(row) if row else None


def _owl_site_by_token(token: str) -> dict | None:
    if not token or len(token) < 20:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM owl_sites WHERE admin_token = ? AND status = 'active'",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def _owl_check_owner(token: str) -> bool:
    return bool(OWL_OWNER_TOKEN) and _secrets.compare_digest(token, OWL_OWNER_TOKEN)


@app.post("/owl/submit")
async def owl_submit(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Public form submission endpoint — called by every Owl Studio client site.

    Request body: { site_id, form_data (obj), form_type?, submitted_from? }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    site_id = str(body.get("site_id", "")).strip()
    form_data = body.get("form_data", {})
    form_type = str(body.get("form_type", "contact")).strip().lower() or "contact"
    submitted_from = str(body.get("submitted_from", ""))[:400]

    # Honeypot: silently accept + drop if bots filled it
    if isinstance(form_data, dict) and form_data.get("nickname"):
        return JSONResponse({"ok": True})

    if not site_id or not isinstance(form_data, dict):
        raise HTTPException(status_code=400, detail="site_id and form_data required")

    site = _owl_site_by_id(site_id)
    if not site:
        # Don't leak valid site_ids; return generic error
        raise HTTPException(status_code=404, detail="unknown site")

    client_ip = request.client.host if request.client else ""
    payload_json = json.dumps(form_data, ensure_ascii=False)[:8000]

    with get_db() as conn:
        conn.execute(
            "INSERT INTO owl_leads (site_id, form_type, payload_json, submitter_ip, submitted_from) VALUES (?, ?, ?, ?, ?)",
            (site_id, form_type, payload_json, client_ip, submitted_from),
        )

    # Notify owner via SMS in background (existing Twilio infra)
    summary_bits = []
    for k in ("name", "contact_name", "phone", "email", "contact_phone", "contact_email"):
        v = form_data.get(k)
        if v:
            summary_bits.append(f"{k}: {v}")
            if len(summary_bits) >= 3:
                break
    msg_tail = " · ".join(summary_bits)[:120] if summary_bits else "(see dashboard)"
    sms_body = f"OwlStudio · new {form_type} on {site['display_name']} · {msg_tail}"
    owner_sms = site.get("lead_sms") or OWNER_NUMBER
    if owner_sms:
        background_tasks.add_task(send_sms, owner_sms, sms_body)

    return JSONResponse({"ok": True, "message": "We'll reply within 24 hours."})


@app.post("/owl/care/ticket")
async def owl_care_ticket(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Care-plan edit request. Body: site_id, submitter_email, subject, body, priority?"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    site_id = str(body.get("site_id", "")).strip()
    submitter_email = str(body.get("submitter_email", "")).strip().lower()
    subject = str(body.get("subject", "")).strip()[:200]
    msg = str(body.get("body", "")).strip()[:4000]
    priority = str(body.get("priority", "normal")).strip().lower()
    if priority not in ("low", "normal", "high"):
        priority = "normal"

    if not all([site_id, submitter_email, subject, msg]):
        raise HTTPException(status_code=400, detail="site_id, submitter_email, subject, body all required")

    site = _owl_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="unknown site")

    # Verify submitter is authorised to open tickets for this site
    try:
        allowed = json.loads(site.get("edit_emails") or "[]")
    except Exception:
        allowed = []
    if submitter_email not in [e.lower() for e in allowed]:
        raise HTTPException(status_code=403, detail="email not authorised for this site")

    # SLA by care tier
    care = site.get("care_tier") or ""
    days = {"concierge": 1, "growth": 2, "essential": 5}.get(care, 5)
    sla_due = (datetime.now() + timedelta(days=days)).isoformat()

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO owl_tickets (site_id, submitter_email, subject, body, priority, sla_due)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (site_id, submitter_email, subject, msg, priority, sla_due),
        )
        ticket_id = cur.lastrowid

    sms_body = f"OwlStudio · ticket #{ticket_id} {priority.upper()} for {site['display_name']} · SLA {days}d · {subject[:60]}"
    if OWNER_NUMBER:
        background_tasks.add_task(send_sms, OWNER_NUMBER, sms_body)

    return JSONResponse({"ok": True, "ticket_id": ticket_id, "sla_due": sla_due})


@app.get("/owl/admin", response_class=HTMLResponse)
def owl_admin(token: str = Query("")) -> HTMLResponse:
    """Per-client dashboard — shows leads + tickets for the site matched by token."""
    site = _owl_site_by_token(token)
    if not site:
        return HTMLResponse("<h1>401 · invalid or missing token</h1>", status_code=401)

    with get_db() as conn:
        leads = [dict(r) for r in conn.execute(
            "SELECT id, ts, form_type, payload_json, status FROM owl_leads WHERE site_id = ? ORDER BY ts DESC LIMIT 100",
            (site["site_id"],),
        ).fetchall()]
        tickets = [dict(r) for r in conn.execute(
            "SELECT id, ts, subject, body, priority, status, sla_due FROM owl_tickets WHERE site_id = ? ORDER BY ts DESC LIMIT 50",
            (site["site_id"],),
        ).fetchall()]

    def esc(s: str) -> str:
        return (str(s or "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    lead_rows = "".join(
        f"<tr><td class='mono'>{esc(l['ts'])}</td><td>{esc(l['form_type'])}</td>"
        f"<td><pre>{esc(l['payload_json'])}</pre></td><td>{esc(l['status'])}</td></tr>"
        for l in leads
    ) or "<tr><td colspan='4' class='empty'>No leads yet.</td></tr>"
    ticket_rows = "".join(
        f"<tr><td class='mono'>#{t['id']}</td><td class='mono'>{esc(t['ts'])}</td>"
        f"<td>{esc(t['subject'])}</td><td>{esc(t['priority'])}</td>"
        f"<td>{esc(t['status'])}</td><td class='mono'>{esc(t['sla_due'])}</td></tr>"
        for t in tickets
    ) or "<tr><td colspan='6' class='empty'>No care tickets yet.</td></tr>"

    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(site['display_name'])} · Owl Studio admin</title>
<link href='https://fonts.googleapis.com/css2?family=Archivo+Black&family=Fraunces:opsz,wght@9..144,400;9..144,600&family=JetBrains+Mono:wght@400;600&display=swap' rel='stylesheet'>
<style>
:root {{ --paper:#F5F1E8; --ink:#0b0a08; --burgundy:#5A1420; --grey:#545454; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: var(--paper); color: var(--ink); font-family: 'Fraunces', Georgia, serif; padding: 40px 24px; max-width: 1200px; margin: 0 auto; }}
h1 {{ font-family: 'Archivo Black', sans-serif; font-size: clamp(28px, 4vw, 44px); letter-spacing: -0.02em; }}
h2 {{ font-family: 'Archivo Black', sans-serif; font-size: 20px; margin: 48px 0 12px; padding-bottom: 10px; border-bottom: 3px solid var(--ink); }}
.kicker {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; color: var(--burgundy); margin-bottom: 8px; }}
.mono {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid rgba(11,10,8,0.14); font-size: 14px; vertical-align: top; }}
th {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--grey); background: transparent; }}
pre {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; white-space: pre-wrap; word-break: break-word; max-width: 560px; margin: 0; }}
.empty {{ color: var(--grey); font-style: italic; }}
.meta {{ display: flex; gap: 24px; flex-wrap: wrap; margin-top: 18px; font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--grey); }}
.meta b {{ color: var(--ink); font-weight: 600; }}
@media (max-width: 640px) {{ body {{ padding: 24px 14px; }} }}
</style></head><body>
<div class='kicker'>OWL STUDIO · CLIENT ADMIN</div>
<h1>{esc(site['display_name'])}</h1>
<div class='meta'>
  <div>tier · <b>{esc(site['tier'])}</b></div>
  <div>care · <b>{esc(site.get('care_tier') or 'none')}</b></div>
  <div>live · <b><a href='{esc(site['live_url'])}'>{esc(site['live_url'])}</a></b></div>
  <div>site_id · <b>{esc(site['site_id'])}</b></div>
</div>
<p style='margin: 8px 0 32px;'><a href='/owl/reports/{site['site_id']}?token={token}' style='display:inline-block;padding:10px 16px;background:var(--ink);color:var(--paper);font-family:"JetBrains Mono",monospace;font-size:11px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;text-decoration:none;'>View latest report -></a></p>
<h2>Leads <span class='mono' style='color: var(--grey); font-weight: 400;'>· {len(leads)} latest</span></h2>
<table><thead><tr><th>When</th><th>Form</th><th>Payload</th><th>Status</th></tr></thead><tbody>{lead_rows}</tbody></table>
<h2>Care tickets <span class='mono' style='color: var(--grey); font-weight: 400;'>· {len(tickets)} latest</span></h2>
<table><thead><tr><th>ID</th><th>When</th><th>Subject</th><th>Priority</th><th>Status</th><th>SLA due</th></tr></thead><tbody>{ticket_rows}</tbody></table>
</body></html>"""
    return HTMLResponse(html)


@app.post("/owl/sites")
async def owl_register_site(request: Request, token: str = Query("")) -> JSONResponse:
    """Owner-only: register a new client site. Returns the generated admin_token."""
    if not _owl_check_owner(token):
        raise HTTPException(status_code=401, detail="owner token required")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    site_id = str(body.get("site_id", "")).strip().lower()
    display_name = str(body.get("display_name", "")).strip()
    tier = str(body.get("tier", "starter")).strip().lower()
    _ct_raw = body.get("care_tier")
    care_tier = (str(_ct_raw).strip().lower() if _ct_raw not in (None, "") else None) or None
    lead_email = str(body.get("lead_email", "")).strip().lower()
    lead_sms = str(body.get("lead_sms", "")).strip() or None
    edit_emails = body.get("edit_emails", [])
    live_url = str(body.get("live_url", "")).strip()

    if not all([site_id, display_name, tier, lead_email, live_url]):
        raise HTTPException(status_code=400, detail="site_id, display_name, tier, lead_email, live_url required")
    if tier not in ("starter", "pro", "custom"):
        raise HTTPException(status_code=400, detail="tier must be starter|pro|custom")
    if care_tier and care_tier not in ("essential", "growth", "concierge"):
        raise HTTPException(status_code=400, detail="care_tier must be essential|growth|concierge")

    admin_token = _secrets.token_urlsafe(32)

    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO owl_sites (site_id, display_name, tier, care_tier, lead_email,
                        lead_sms, edit_emails, admin_token, live_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (site_id, display_name, tier, care_tier, lead_email,
                 lead_sms, json.dumps(edit_emails), admin_token, live_url),
            )
    except _DbIntegrityError:
        raise HTTPException(status_code=409, detail="site_id already exists")

    return JSONResponse({
        "ok": True,
        "site_id": site_id,
        "admin_url": f"/owl/admin?token={admin_token}",
        "admin_token": admin_token,
        "embed_snippet": _owl_embed_snippet(site_id),
    })


@app.get("/owl/sites")
def owl_list_sites(token: str = Query("")) -> JSONResponse:
    if not _owl_check_owner(token):
        raise HTTPException(status_code=401, detail="owner token required")
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT site_id, display_name, tier, care_tier, live_url, status, created_at FROM owl_sites ORDER BY created_at DESC"
        ).fetchall()]
    return JSONResponse({"sites": rows})


def _owl_embed_snippet(site_id: str) -> str:
    """JS snippet to paste on a client site's contact form."""
    return (
        f"<script>document.querySelectorAll('form[data-owl]').forEach(f => "
        f"f.addEventListener('submit', async e => {{ e.preventDefault(); "
        f"const fd = Object.fromEntries(new FormData(f)); "
        f"const r = await fetch('https://callmeie.onrender.com/owl/submit', {{"
        f"method:'POST', headers:{{'Content-Type':'application/json'}}, "
        f"body: JSON.stringify({{site_id:'{site_id}', form_data: fd, "
        f"submitted_from: location.href}}) }}); "
        f"const j = await r.json(); "
        f"f.dispatchEvent(new CustomEvent('owl-result', {{detail: j}})); "
        f"if (j.ok) f.reset(); "
        f"}}));</script>"
    )


@app.get("/owl/health/{site_id}")
def owl_health(site_id: str) -> JSONResponse:
    """Simple health check UptimeRobot can poll."""
    site = _owl_site_by_id(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="unknown site")
    return JSONResponse({"ok": True, "site_id": site_id, "live_url": site["live_url"]})


def _owl_report_stats(site_id: str, period_days: int = 30) -> dict:
    """Stats a monthly care-plan report shows for one site."""
    cutoff = (datetime.utcnow() - timedelta(days=period_days)).isoformat()
    prev_cutoff = (datetime.utcnow() - timedelta(days=period_days * 2)).isoformat()
    with get_db() as conn:
        leads_now = conn.execute(
            "SELECT COUNT(*) FROM owl_leads WHERE site_id = ? AND ts >= ?",
            (site_id, cutoff),
        ).fetchone()[0]
        leads_prev = conn.execute(
            "SELECT COUNT(*) FROM owl_leads WHERE site_id = ? AND ts >= ? AND ts < ?",
            (site_id, prev_cutoff, cutoff),
        ).fetchone()[0]
        tickets_opened = conn.execute(
            "SELECT COUNT(*) FROM owl_tickets WHERE site_id = ? AND ts >= ?",
            (site_id, cutoff),
        ).fetchone()[0]
        tickets_closed = conn.execute(
            "SELECT COUNT(*) FROM owl_tickets WHERE site_id = ? AND ts >= ? AND status = 'done'",
            (site_id, cutoff),
        ).fetchone()[0]
        form_types = [dict(r) for r in conn.execute(
            "SELECT form_type, COUNT(*) AS n FROM owl_leads WHERE site_id = ? AND ts >= ? GROUP BY form_type ORDER BY n DESC",
            (site_id, cutoff),
        ).fetchall()]
        recent_payments = [dict(r) for r in conn.execute(
            "SELECT event_type, amount, currency, product_key, ts FROM owl_payments WHERE site_id = ? AND ts >= ? ORDER BY ts DESC LIMIT 10",
            (site_id, cutoff),
        ).fetchall()]
    delta = leads_now - leads_prev
    delta_pct = round(100 * delta / leads_prev) if leads_prev > 0 else (100 if leads_now > 0 else 0)
    return {
        "period_days": period_days,
        "leads_now": leads_now, "leads_prev": leads_prev,
        "leads_delta": delta, "leads_delta_pct": delta_pct,
        "tickets_opened": tickets_opened, "tickets_closed": tickets_closed,
        "form_types": form_types, "recent_payments": recent_payments,
    }


@app.get("/owl/reports/{site_id}", response_class=HTMLResponse)
def owl_report(site_id: str, token: str = Query(""), period: int = Query(30)) -> HTMLResponse:
    """Branded 1-page monthly report — same token as /owl/admin.
    Print-friendly (Ctrl+P for PDF). Real-time stats."""
    site = _owl_site_by_token(token)
    if not site or site["site_id"] != site_id:
        return HTMLResponse("<h1>401 - invalid or missing token</h1>", status_code=401)

    period = max(7, min(365, int(period)))
    s = _owl_report_stats(site_id, period)

    def esc(x):
        return (str(x or "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    care = (site.get("care_tier") or "").lower()
    care_cls = {"essential": "essential", "growth": "growth", "concierge": "concierge"}.get(care, "none")

    form_rows = "".join(
        f"<tr><td>{esc(f['form_type'])}</td><td class='num'>{f['n']}</td></tr>"
        for f in s["form_types"]
    ) or "<tr><td colspan='2' class='empty'>No form submissions in the period.</td></tr>"

    pay_rows = "".join(
        f"<tr><td class='mono'>{esc(p['ts'])}</td>"
        f"<td>{esc(p['event_type'])}</td>"
        f"<td>{esc(p['product_key'] or '-')}</td>"
        f"<td class='num'>{(p['amount'] or 0)/100:.0f} {esc((p['currency'] or '').upper())}</td></tr>"
        for p in s["recent_payments"]
    ) or "<tr><td colspan='4' class='empty'>No payment events in the period.</td></tr>"

    delta_cls = "up" if s["leads_delta"] > 0 else ("down" if s["leads_delta"] < 0 else "flat")
    delta_sign = "+" if s["leads_delta"] > 0 else ""
    now_iso = datetime.now().isoformat()[:19].replace("T", " ")

    html = f"""<!doctype html><html lang='en'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(site['display_name'])} - Monthly report</title>
<link href='https://fonts.googleapis.com/css2?family=Archivo+Black&family=Fraunces:opsz,wght@9..144,400;9..144,600&family=JetBrains+Mono:wght@400;500;600&display=swap' rel='stylesheet'>
<style>
:root {{ --paper:#F5F1E8; --ink:#0b0a08; --burgundy:#5A1420; --grey:#545454; --accent:#c96f32; }}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--paper);color:var(--ink);font-family:'Fraunces',Georgia,serif;padding:clamp(28px,4vw,64px) clamp(20px,4vw,40px);max-width:1000px;margin:0 auto;line-height:1.55;}}
.kicker{{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:var(--burgundy);margin-bottom:10px;}}
h1{{font-family:'Archivo Black',sans-serif;font-size:clamp(30px,4vw,52px);letter-spacing:-0.02em;line-height:1.04;margin-bottom:6px;}}
.sub{{font-size:18px;color:var(--grey);margin-bottom:32px;}}
.meta{{display:flex;gap:18px;flex-wrap:wrap;font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--grey);padding:12px 0;border-top:3px solid var(--ink);border-bottom:1px solid rgba(11,10,8,0.14);margin-bottom:36px;}}
.meta b{{color:var(--ink);font-weight:600;}}
.badge{{padding:3px 8px;background:var(--ink);color:var(--paper);font-weight:600;letter-spacing:0.1em;}}
.badge.growth{{background:var(--accent);}}
.badge.concierge{{background:var(--burgundy);}}
.badge.essential{{background:var(--grey);color:var(--paper);}}
.badge.none{{background:rgba(11,10,8,0.2);color:var(--grey);}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:22px;margin-bottom:40px;}}
@media (max-width:720px){{.stats{{grid-template-columns:repeat(2,1fr);}}}}
.stat{{border-top:3px solid var(--ink);padding-top:14px;}}
.stat .label{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.14em;text-transform:uppercase;color:var(--grey);margin-bottom:10px;}}
.stat .num{{font-family:'Archivo Black',sans-serif;font-size:42px;letter-spacing:-0.03em;line-height:1;}}
.stat .delta{{font-family:'JetBrains Mono',monospace;font-size:11px;margin-top:6px;}}
.stat .delta.up{{color:#2d6e3f;}}
.stat .delta.down{{color:var(--burgundy);}}
.stat .delta.flat{{color:var(--grey);}}
h2{{font-family:'Archivo Black',sans-serif;font-size:22px;margin:40px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--ink);}}
table{{width:100%;border-collapse:collapse;}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid rgba(11,10,8,0.14);font-size:14px;}}
th{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:0.12em;text-transform:uppercase;color:var(--grey);}}
td.num{{font-family:'JetBrains Mono',monospace;font-weight:600;text-align:right;}}
td.mono{{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--grey);}}
.empty{{color:var(--grey);font-style:italic;}}
.footer-note{{margin-top:50px;padding-top:24px;border-top:2px solid var(--ink);font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--grey);line-height:1.6;}}
.footer-note a{{color:var(--accent);}}
@media print{{body{{padding:0;}}.meta,h2{{break-inside:avoid;}}}}
</style></head><body>
<div class='kicker'>Owl Studio * Care plan monthly report</div>
<h1>{esc(site['display_name'])}</h1>
<p class='sub'>{esc(site['live_url'])}</p>
<div class='meta'>
  <div>Period * <b>last {s['period_days']} days</b></div>
  <div>Care tier * <span class='badge {care_cls}'>{esc(care or 'none')}</span></div>
  <div>Tier * <b>{esc(site['tier'])}</b></div>
  <div>Report generated * <b>{esc(now_iso)}</b></div>
</div>
<div class='stats'>
  <div class='stat'>
    <div class='label'>Leads</div>
    <div class='num'>{s['leads_now']}</div>
    <div class='delta {delta_cls}'>{delta_sign}{s['leads_delta']} vs prior period ({s['leads_delta_pct']:+d}%)</div>
  </div>
  <div class='stat'>
    <div class='label'>Tickets opened</div>
    <div class='num'>{s['tickets_opened']}</div>
    <div class='delta flat'>{s['tickets_closed']} closed</div>
  </div>
  <div class='stat'>
    <div class='label'>Form types</div>
    <div class='num'>{len(s['form_types'])}</div>
    <div class='delta flat'>distinct form sources</div>
  </div>
  <div class='stat'>
    <div class='label'>Payments logged</div>
    <div class='num'>{len(s['recent_payments'])}</div>
    <div class='delta flat'>Stripe events captured</div>
  </div>
</div>
<h2>Form submissions by type</h2>
<table><thead><tr><th>Form type</th><th style='text-align:right;'>Count</th></tr></thead>
<tbody>{form_rows}</tbody></table>
<h2>Recent payment events</h2>
<table><thead><tr><th>When</th><th>Event</th><th>Product</th><th style='text-align:right;'>Amount</th></tr></thead>
<tbody>{pay_rows}</tbody></table>
<div class='footer-note'>
  Print this page (Ctrl+P / Cmd+P) to save as PDF. Data updates in real time -
  reload anytime. Different period via <code>?period=60</code> or <code>?period=90</code>.
  <br><br>
  Care tier: <b>{esc(care or 'none')}</b>. Upgrade paths at
  <a href='https://websites.owlzone.trade/#care'>websites.owlzone.trade/#care</a>.
</div>
</body></html>"""
    return HTMLResponse(html)


@app.post("/owl/reports/run-digest")
async def owl_run_digest(background_tasks: BackgroundTasks, token: str = Query("")) -> JSONResponse:
    """Owner-only: iterate every active site, compute stats, SMS the owner
    a per-site digest line + admin URL. Triggered by GitHub Actions cron
    (monthly-owl-digest.yml) on the 1st of each month at 08:00 UTC.
    Idempotent — safe to call ad-hoc too (e.g. to preview the digest)."""
    if not _owl_check_owner(token):
        raise HTTPException(status_code=401, detail="owner token required")

    with get_db() as conn:
        sites = [dict(r) for r in conn.execute(
            "SELECT site_id, display_name, tier, care_tier, admin_token, live_url FROM owl_sites WHERE status = 'active'"
        ).fetchall()]

    lines: list[str] = []
    for s in sites:
        stats = _owl_report_stats(s["site_id"], period_days=30)
        delta_sign = "+" if stats["leads_delta"] > 0 else ""
        lines.append(
            f"{s['display_name']}: {stats['leads_now']} leads ({delta_sign}{stats['leads_delta']}), "
            f"{stats['tickets_opened']} tickets. "
            f"https://callmeie.onrender.com/owl/reports/{s['site_id']}?token={s['admin_token']}"
        )

    digest = "OwlStudio monthly digest · " + datetime.now().strftime("%b %Y") + "\n\n" + "\n\n".join(lines) if lines else "OwlStudio: no active sites."

    # Twilio caps SMS at 1600 chars — chunk if needed
    owner = OWNER_NUMBER
    if owner:
        remaining = digest
        while remaining:
            chunk, remaining = remaining[:1500], remaining[1500:]
            background_tasks.add_task(send_sms, owner, chunk)

    return JSONResponse({
        "ok": True,
        "sites_reported": len(sites),
        "digest_length": len(digest),
        "preview": digest[:800],
    })


# ---------------- Stripe webhook -----------------------------------------
# Handles: checkout.session.completed, customer.subscription.*,
# invoice.paid, invoice.payment_failed. Signature verified against
# OWL_STRIPE_WEBHOOK_SECRET env var (set in Render Environment).

import hashlib as _hashlib
import hmac as _hmac
import time as _time

OWL_STRIPE_WEBHOOK_SECRET = os.environ.get("OWL_STRIPE_WEBHOOK_SECRET", "").strip()


def _owl_init_payments_table() -> None:
    with get_db() as conn:
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS owl_payments (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                stripe_event_id   TEXT UNIQUE,
                event_type        TEXT NOT NULL,
                ts                TEXT NOT NULL DEFAULT (datetime('now')),
                site_id           TEXT,
                customer_id       TEXT,
                subscription_id   TEXT,
                product_key       TEXT,
                amount            INTEGER,
                currency          TEXT,
                status            TEXT,
                payload_json      TEXT
            )
        """))
        conn.execute(_ddl_fix("CREATE INDEX IF NOT EXISTS idx_owl_pay_site ON owl_payments(site_id, ts)"))


_owl_init_payments_table()


def _owl_verify_stripe_sig(payload: bytes, sig_header: str, secret: str, tolerance_seconds: int = 300) -> bool:
    """Stripe signature check per their spec — no stripe-python dep needed."""
    if not secret or not sig_header:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    t = parts.get("t", "")
    v1 = parts.get("v1", "")
    if not t or not v1:
        return False
    try:
        t_int = int(t)
    except ValueError:
        return False
    if abs(_time.time() - t_int) > tolerance_seconds:
        return False  # replay protection
    signed = f"{t}.{payload.decode('utf-8', errors='replace')}"
    expected = _hmac.new(secret.encode(), signed.encode(), _hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, v1)


# Map Stripe product metadata.owl_key -> care_tier column value on owl_sites
_OWL_KEY_TO_CARE_TIER = {
    "care-essential": "essential",
    "care-growth": "growth",
    "care-concierge": "concierge",
}


@app.post("/owl/stripe/webhook")
async def owl_stripe_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Stripe webhook receiver. Verifies signature, dedupes by event id,
    logs to owl_payments, and updates owl_sites.care_tier on subscription
    lifecycle events."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not _owl_verify_stripe_sig(payload, sig, OWL_STRIPE_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        event = json.loads(payload.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {}) or {}

    # Extract common fields (tolerant — many missing fields across event types)
    customer_id = obj.get("customer") or ""
    subscription_id = obj.get("subscription") or (obj.get("id") if event_type.startswith("customer.subscription") else "")
    amount = obj.get("amount_total") or obj.get("amount_paid") or obj.get("amount") or 0
    currency = obj.get("currency") or ""
    status = obj.get("status") or ""

    # Metadata on the Checkout Session (one-off payments) or on the Subscription's item price
    meta = obj.get("metadata") or {}
    product_key = meta.get("owl_key") or ""
    site_id = meta.get("site_id") or ""

    # For subscription events, the product_key lives on the first item's price.lookup_key
    if event_type.startswith("customer.subscription") and not product_key:
        items = (obj.get("items") or {}).get("data") or []
        if items:
            price = items[0].get("price") or {}
            product_key = price.get("lookup_key") or (price.get("metadata") or {}).get("owl_key") or ""

    # Dedupe: the UNIQUE constraint on stripe_event_id handles accidental replays
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO owl_payments
                    (stripe_event_id, event_type, site_id, customer_id, subscription_id,
                     product_key, amount, currency, status, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, event_type, site_id, customer_id, subscription_id,
                 product_key, amount, currency, status,
                 json.dumps(obj, ensure_ascii=False)[:8000]),
            )
    except _DbIntegrityError:
        return JSONResponse({"ok": True, "deduped": True})

    # Subscription lifecycle -> update owl_sites.care_tier if site_id was attached
    care_tier_map = None
    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        # Active or trialing = care plan is "on"
        if status in ("active", "trialing"):
            # product_key is one of care-essential / care-growth / care-concierge
            # strip the monthly/yearly suffix for the site_tier mapping
            base = product_key.rsplit("-", 1)[0] if "-monthly" in product_key or "-yearly" in product_key else product_key
            care_tier_map = _OWL_KEY_TO_CARE_TIER.get(base)
    elif event_type == "customer.subscription.deleted":
        care_tier_map = "none"

    if site_id and care_tier_map:
        with get_db() as conn:
            conn.execute(
                "UPDATE owl_sites SET care_tier = ? WHERE site_id = ?",
                (None if care_tier_map == "none" else care_tier_map, site_id),
            )

    # Owner SMS on notable events
    if event_type == "checkout.session.completed":
        sms = f"OwlStudio Stripe * paid * {product_key} * {amount/100:.0f} {currency} * cust {customer_id[:12]}"
        if OWNER_NUMBER:
            background_tasks.add_task(send_sms, OWNER_NUMBER, sms)
    elif event_type == "invoice.payment_failed":
        sms = f"OwlStudio Stripe * PAYMENT FAILED * {product_key or subscription_id[:12]} * cust {customer_id[:12]}"
        if OWNER_NUMBER:
            background_tasks.add_task(send_sms, OWNER_NUMBER, sms)

    return JSONResponse({"ok": True, "event_type": event_type, "product_key": product_key})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
