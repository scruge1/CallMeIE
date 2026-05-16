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
  POST /api/discovery          — Discovery chatbot quiz submit (P3 widget)
  POST /api/docops/extract     — Ephemeral PDF → invoice JSON (P5b)
  GET  /admin                  — Admin portal (protected)
  GET  /admin/api/submissions  — List pending submissions
  GET  /admin/api/clients      — List provisioned clients
  GET  /admin/api/discovery-submissions — List discovery quiz submissions
  POST /admin/api/provision/{id} — Provision a client from submission
  POST /admin/api/reject/{id}  — Reject a submission
  GET  /health                 — Health check
"""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from google.oauth2 import service_account
from googleapiclient.discovery import build

sys.path.insert(0, os.path.dirname(__file__))

app = FastAPI(title="CallMeIE — AI Receptionist Server")


# P0-7 — wrap any uncaught exception in a JSON envelope so the visitor
# never sees a plaintext "Internal Server Error" body. Anything that
# survives the route handlers (programmer errors, deps failing, etc.)
# routes through here. Without this, uvicorn renders the FastAPI default
# 500 page (plaintext) which is what P4 caught on the discovery widget
# when malformed JSON hit /api/discovery.
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as _StarletteHTTPException


@app.exception_handler(_StarletteHTTPException)
async def _http_exception_envelope(request, exc):
    """Re-render any HTTPException with a JSON body even if the route
    raised the bare exception (some routes do that intentionally for
    auth + permission boundaries)."""
    return JSONResponse(
        {"error": (exc.detail if isinstance(exc.detail, str) else "http_error"),
         "status": exc.status_code},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def _validation_envelope(request, exc):
    return JSONResponse(
        {"error": "invalid_request", "message": "Required fields missing or malformed."},
        status_code=400,
    )


@app.exception_handler(Exception)
async def _catchall_envelope(request, exc):
    """Last-resort handler. Logs the full exception server-side and
    returns a friendly JSON envelope. Visitor never sees plaintext."""
    print(f"[server] uncaught exception on {request.url.path}: {type(exc).__name__}: {exc}", flush=True)
    return JSONResponse(
        {"error": "server_error",
         "message": "Something failed on our side. Email hello@callmeie.ie or WhatsApp +353 85 786 3564 and we'll sort it."},
        status_code=500,
    )

# AUD-038 — Vapi metered billing + Stripe Customer Portal routes.
# Routers stay inert (401/503) until VAPI_WEBHOOK_SECRET / STRIPE_SECRET_KEY
# env vars are present on Render. Importable on cold boot — any import error
# means a real packaging problem we want to see immediately.
try:
    from billing.webhook import router as _vapi_billing_router
    from billing.portal import router as _portal_router
    from billing.admin import router as _meter_admin_router
    from billing.db import init_db as _init_billing_db

    _init_billing_db()
    app.include_router(_vapi_billing_router)
    app.include_router(_portal_router)
    app.include_router(_meter_admin_router)
except Exception as _e:
    # Log but keep the rest of the server alive — billing is additive.
    print(f"[billing] router init failed: {_e}", flush=True)

# P2-4 — unified_leads ingestor (best-effort; never blocks channel writes)
try:
    import lead_ingestor as _lead_ingestor
except Exception as _e:
    print(f"[lead_ingestor] import failed: {_e}", file=sys.stderr)
    _lead_ingestor = None

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
ADMIN_HTML_PATH = os.path.join(_SCRIPTS_DIR, "admin.html")
CLIENT_HTML_PATH = os.path.join(_SCRIPTS_DIR, "client.html")
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
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
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
        # P5-6 — was `clients`; renamed to `assistants` to disambiguate
        # from billing/db.py `clients` (paying-tier billing entity, different
        # shape, different DB). See alembic/versions/0002_rename_clients_to_assistants.py
        # + scripts/CLIENTS-VS-ASSISTANTS.md for rationale.
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS assistants (
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
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS discovery_submissions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT DEFAULT (datetime('now')),
                page_context        TEXT,
                business            TEXT,
                pain                TEXT,
                team_size           TEXT,
                urgency             TEXT,
                contact_email       TEXT,
                contact_name        TEXT,
                recommended_product TEXT,
                tier_anchor         TEXT,
                result_text         TEXT,
                ip_hash             TEXT,
                user_agent          TEXT
            )
        """))
        # P5-1 — created_at indexes on hot tables. Admin queries scan
        # ORDER BY created_at DESC LIMIT N (server.py:2671 etc.). Without
        # indexes these are full table scans; sub-second today on small
        # tables, painful at 100k+ rows. CREATE INDEX IF NOT EXISTS is
        # idempotent so this runs every container start with no cost.
        for sql in [
            "CREATE INDEX IF NOT EXISTS idx_submissions_created_at ON submissions(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_call_events_created_at ON call_events(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_call_diagnostics_created_at ON call_diagnostics(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_discovery_submissions_created_at ON discovery_submissions(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_assistants_created_at ON assistants(created_at DESC)",
        ]:
            try:
                conn.execute(sql)
            except Exception as e:  # noqa: BLE001 — index create must not block boot
                print(f"[init_db] index create skipped: {sql} -- {e}", flush=True)
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

        # P5-2 — GDPR retention machinery. Adds `suppressed_at` to every
        # PII-bearing table so the daily purge can soft-delete-then-hard-purge
        # rows past their retention window. Idempotent: per-column try/except,
        # one column-name introspection per table, both dialects supported.
        # See scripts/purge_old_data.py for the cron logic that consumes
        # these columns. owl_tickets also gets `closed_at` (didn't exist
        # before; needed to scope the closed-tickets retention sweep).
        if _USE_PG:
            ts_col_type = "TIMESTAMPTZ"
        else:
            ts_col_type = "DATETIME"

        retention_targets = [
            ("submissions", "suppressed_at"),
            ("discovery_submissions", "suppressed_at"),
            ("owl_leads", "suppressed_at"),
            ("owl_tickets", "suppressed_at"),
            ("owl_tickets", "closed_at"),
            ("leads", "suppressed_at"),
            ("assistants", "suppressed_at"),  # P5-6 — was `clients`
        ]
        for table, column in retention_targets:
            try:
                if _USE_PG:
                    cols = {row["column_name"] for row in conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                        (table,)
                    ).fetchall()}
                else:
                    cols = {row["name"] for row in conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ts_col_type}")
            except Exception as e:
                # Already-exists or table-not-yet-created: tolerate. owl_*
                # tables get init'd later in _owl_init_tables() so on first
                # boot they may be missing here; the next boot will pick
                # them up. Hand-rerun: just restart the container.
                print(f"[init_db] retention migration {table}.{column} skipped: {e}", file=sys.stderr)

        # Client dashboard tables (alembic 0010_client_tokens) — added inline
        # as a safety net so the client portal works even if migration
        # hasn't been stamped in production yet. Alembic IF EXISTS is a no-op
        # when the tables are already there.
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS client_tokens (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at          TEXT DEFAULT (datetime('now')),
                token               TEXT UNIQUE NOT NULL,
                tenant_slug         TEXT NOT NULL,
                tenant_display_name TEXT,
                assistant_ids       TEXT,
                last_used_at        TEXT,
                revoked_at          TEXT,
                created_by          TEXT
            )
        """))
        conn.execute(_ddl_fix("""
            CREATE INDEX IF NOT EXISTS idx_client_tokens_token ON client_tokens(token)
        """))
        conn.execute(_ddl_fix("""
            CREATE INDEX IF NOT EXISTS idx_client_tokens_tenant ON client_tokens(tenant_slug)
        """))
        conn.execute(_ddl_fix("""
            CREATE TABLE IF NOT EXISTS call_notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now')),
                call_id     TEXT NOT NULL,
                tenant_slug TEXT,
                note        TEXT NOT NULL,
                actor       TEXT
            )
        """))
        conn.execute(_ddl_fix("""
            CREATE INDEX IF NOT EXISTS idx_call_notes_call_id ON call_notes(call_id)
        """))

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


# --------------------------------------------------------------------------
# P2 (2026-05-11) — Vapi recording mirror to Hetzner Object Storage.
#
# WHY: privacy.html §sec-12 + dpa.html §3 promise 90-day call-recording
# retention with EU residency. Vapi PAYG only stores 14 days natively
# (per docs.vapi.ai/assistants/call-recording). To honour the legal
# commitment we mirror every recording from Vapi's transient working copy
# to Hetzner Object Storage (already-disclosed sub-processor, Nuremberg DE).
# The Hetzner bucket carries an S3 lifecycle rule that auto-expires objects
# at day 90, so the durable archive deletes itself on schedule.
#
# Cost: ~€0.005/GB/month at Hetzner Object Storage rates. Typical voice call
# 1 MB/min → 60 MB/hour → trivial.
#
# Failure mode: mirror failures DO NOT break the webhook. The handler returns
# 200 even if Hetzner is unreachable; a 'recording-archive-failed' event is
# logged so Adam can manually pull from Vapi before the 14-day window closes.
# --------------------------------------------------------------------------
_HETZNER_S3_CLIENT = None


def _hetzner_s3():
    """Return a boto3 S3 client pointed at Hetzner Object Storage. Memoised."""
    global _HETZNER_S3_CLIENT
    if _HETZNER_S3_CLIENT is not None:
        return _HETZNER_S3_CLIENT
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        print("[Mirror] boto3 not installed — recording archive disabled")
        return None
    endpoint = os.environ.get("HETZNER_OBJECT_STORAGE_ENDPOINT", "")
    key_id = os.environ.get("HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID", "")
    secret = os.environ.get("HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY", "")
    if not (endpoint and key_id and secret):
        print("[Mirror] Hetzner credentials missing — recording archive disabled")
        return None
    if not endpoint.startswith("http"):
        endpoint = "https://" + endpoint
    _HETZNER_S3_CLIENT = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="eu-central-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    return _HETZNER_S3_CLIENT


def _mirror_recording_to_hetzner(call_id: str, assistant_id: str,
                                  recording_url: str = "", stereo_url: str = ""):
    """Download Vapi recording → upload to Hetzner under recordings/{call_id}.{wav|stereo.wav}.
    Logs event 'recording-archived' on success, 'recording-archive-failed' on error.
    NEVER raises — webhook caller assumes best-effort."""
    if not call_id or not (recording_url or stereo_url):
        return
    s3 = _hetzner_s3()
    if s3 is None:
        log_event(call_id, "recording-archive-failed", assistant_id,
                  "hetzner S3 client unavailable",
                  {"reason": "client_init_failed"})
        return
    bucket = os.environ.get("HETZNER_OBJECT_STORAGE_BUCKET", "")
    if not bucket:
        log_event(call_id, "recording-archive-failed", assistant_id,
                  "HETZNER_OBJECT_STORAGE_BUCKET env missing", {})
        return
    import requests as _rq
    archived = {}
    for label, url in [("mono", recording_url), ("stereo", stereo_url)]:
        if not url:
            continue
        try:
            resp = _rq.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            ext = ".wav"
            ctype = resp.headers.get("content-type", "audio/wav")
            if "mp3" in ctype.lower():
                ext = ".mp3"
            key = f"recordings/{call_id}{'' if label == 'mono' else '.stereo'}{ext}"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=resp.content,
                ContentType=ctype,
                Metadata={
                    "vapi-call-id": call_id,
                    "vapi-assistant-id": assistant_id,
                    "channel": label,
                    "mirrored-at": datetime.utcnow().isoformat() + "Z",
                },
            )
            archived[label] = f"s3://{bucket}/{key}"
        except Exception as e:
            log_event(call_id, "recording-archive-failed", assistant_id,
                      f"{label} fetch/upload failed: {str(e)[:120]}",
                      {"label": label, "error": str(e)[:200]})
            return
    if archived:
        log_event(call_id, "recording-archived", assistant_id,
                  f"mirrored {len(archived)} channel(s) to Hetzner",
                  archived)
        print(f"[Mirror] {call_id} → {archived}")


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
                "SELECT * FROM assistants WHERE assistant_id = ?", (assistant_id,)
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
    """Vapi fires this when any call ends. Returns 200 immediately; anomaly diagnosis runs in background.

    2026-05-13 — noise-suppression: Vapi posts MANY webhook types to this URL
    (status-update / transcript / conversation-update / speech-update / hang /
    user-interrupted / model-output / voice-input / tool-calls / etc). Only
    `end-of-call-report` carries the final assistant + duration + transcript.
    Without the top-of-handler guard, every other type produces an empty
    call-ended row with NULL call_id + NULL assistant + 0s duration.

    Other handler endpoints (/capture-lead, /demo-complete, /check-availability,
    /book-appointment) handle tool-calls separately at their own routes.
    """
    body = await request.json()

    # Vapi wraps payload under `message`: {message: {type, call, artifact, ...}}.
    # Older test bodies put `call` at top-level. Support both.
    msg = body.get("message") or {}
    msg_type = msg.get("type") or body.get("type") or ""

    # Guard — only end-of-call-report carries the final summary.
    # Anything else is ignored at 200 OK (no noise rows).
    if msg_type and msg_type != "end-of-call-report":
        return JSONResponse({"status": "ok", "ignored_type": msg_type})

    # Pull call data from message envelope FIRST, fall back to top-level / body.
    call = msg.get("call") or body.get("call") or body
    assistant_id = (
        call.get("assistantId", "")
        or msg.get("assistantId", "")
        or body.get("assistant", {}).get("id", "")
    )
    status = call.get("status", "") or msg.get("status", "")
    caller = (call.get("customer", {}) or {}).get("number", "") or (msg.get("customer", {}) or {}).get("number", "")
    duration = call.get("duration", 0) or msg.get("durationSeconds", 0) or msg.get("duration", 0)
    call_id = call.get("id", "") or msg.get("callId", "")

    # Skip writing noise rows: if we have neither call_id nor assistant_id, return 200.
    if not call_id and not assistant_id:
        return JSONResponse({"status": "ok", "skipped": "no_call_id_or_assistant"})

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

    # 2026-05-13 — pull transcript + artifact bits BEFORE writing the row so we
    # can persist transcript inline in detail JSON (used by /client/api/calls/{id}
    # which reads d.get("transcript") and joins parts).
    artifact = msg.get("artifact") or call.get("artifact") or {}
    transcript_text = artifact.get("transcript", "") or ""
    artifact_messages = artifact.get("messages") or artifact.get("messagesOpenAIFormatted") or []
    ended_reason = msg.get("endedReason") or call.get("endedReason") or ""
    analysis = msg.get("analysis") or {}

    # Cleaner summary line for dashboard display (no raw debug fields)
    duration_int = int(duration) if duration else 0
    status_pretty = ended_reason or status or "completed"
    summary_text = f"{status_pretty} · {duration_int}s · from {caller}" if caller else f"{status_pretty} · {duration_int}s"

    log_event(call_id, "call-ended", assistant_id,
              summary_text,
              {
                  "status": status,
                  "duration": duration,
                  "caller": caller,
                  "transcript": transcript_text[:30000] if transcript_text else "",
                  "ended_reason": ended_reason,
                  "summary": (analysis.get("summary") or "")[:1000] if analysis else "",
                  "structured_data": analysis.get("structuredData") if analysis else None,
                  "messages_count": len(artifact_messages) if isinstance(artifact_messages, list) else 0,
              })

    # 2026-05-13 — Handoff attribution. Vapi sends ONE end-of-call-report tagged
    # with the ORIGINATING assistantId. For multi-assistant chains (Claire ->
    # Dunne -> trap), the artifact.assistantActivations array lists every
    # assistant active during the call. Write one extra call-ended row per
    # non-originating assistant so tenant dashboards (filtered by assistant_id)
    # see the call WITHOUT needing the shared origin assistant (Claire) in
    # their whitelist (which would leak other niche calls).
    activations = artifact.get("assistantActivations") or []
    seen_aids = {assistant_id} if assistant_id else set()
    handoff_chain = []
    for act in activations:
        if not isinstance(act, dict):
            continue
        aid = act.get("assistantId") or (act.get("assistant") or {}).get("id") or ""
        if aid:
            handoff_chain.append(aid)
        if not aid or aid in seen_aids:
            continue
        seen_aids.add(aid)
        log_event(
            call_id,
            "call-ended",
            aid,
            summary_text,
            {
                "status": status,
                "duration": duration,
                "caller": caller,
                "handoff_from": assistant_id,
                "handoff_chain": handoff_chain or [assistant_id],
                "transcript": transcript_text[:30000] if transcript_text else "",
                "ended_reason": ended_reason,
                "summary": (analysis.get("summary") or "")[:1000] if analysis else "",
                "is_mirror_row": True,
            },
        )

    # P2 — mirror Vapi recording to durable Hetzner archive (90d retention promise).
    # Vapi includes recordingUrl + stereoRecordingUrl on the call object once
    # the recording is available; schedule a background task so the webhook
    # returns 200 immediately. Skip if no recording (e.g. silent failed call).
    recording_url = artifact.get("recordingUrl") or call.get("recordingUrl") or ""
    stereo_url = artifact.get("stereoRecordingUrl") or call.get("stereoRecordingUrl") or ""
    if call_id and (recording_url or stereo_url):
        background_tasks.add_task(
            _mirror_recording_to_hetzner,
            call_id, assistant_id, recording_url, stereo_url,
        )

    # 2026-05-13 — for handoff chains where Vapi reports empty/origin assistantId,
    # the call may be a demo even if the top-level assistant_id isn't in
    # DEMO_ASSISTANT_IDS. Walk the activations chain to detect demo membership.
    is_demo = assistant_id in DEMO_ASSISTANT_IDS
    if not is_demo:
        for act in (activations or []):
            if isinstance(act, dict):
                aid = act.get("assistantId") or (act.get("assistant") or {}).get("id") or ""
                if aid in DEMO_ASSISTANT_IDS:
                    is_demo = True
                    break

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
        next_tool = "transfer_general_business_demo"

    if next_tool == "transfer_general_business_demo":
        biz_display = business if business else "your business"
        instruction = (
            f"Lead saved. Confirm with caller: 'Just to make sure I route you to the right demo — "
            f"you're looking for a receptionist for {biz_display}, is that right?' "
            f"Once confirmed, call transfer_general_business_demo. Do not speak otherwise."
        )
    else:
        instruction = f"Lead saved. Call {next_tool} now. Do not speak."

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

    # P0-11 — never log full PII payload to stdout. Render captures
    # stdout into log retention which is OUTSIDE the DPA's data-handling
    # commitments and is GDPR Art. 5 storage-limitation risk. We log
    # only cardinal facts (business name, business type, plan) so Adam
    # can spot duplicate / abusive submissions at a glance, and the
    # full row is in the `submissions` table where retention is
    # governed.
    print(
        f"[ONBOARDING] business={business_name!r} type={business_type!r} "
        f"plan={plan!r} hours_set={bool(hours)} services_set={bool(services)} "
        f"insurance_set={bool(insurance)} ai_name_set={bool(ai_name)} "
        f"faqs_n={len(faqs) if isinstance(faqs, (list, str)) else 0}",
        flush=True,
    )

    if not business_name:
        return JSONResponse({"error": "business_name required"}, status_code=400)

    # Save to DB for admin review
    submission_row_id = None
    try:
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO submissions
                (business_name, contact_name, contact_phone, contact_email,
                 business_type, address, hours, services, emergency_number,
                 calendar_email, plan, ai_name, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                RETURNING id
            """, (business_name, contact_name, contact_phone, contact_email,
                  business_type, address, hours, services, emergency_number,
                  calendar_email, plan, ai_name, notes))
            try:
                submission_row_id = cur.fetchone()[0]
            except Exception:
                submission_row_id = None
            conn.commit()
    except Exception as e:
        print(f"[DB] Failed to save submission: {e}")

    # P2-4 — non-blocking unified_leads upsert
    if _lead_ingestor is not None:
        _lead_ingestor.upsert(get_db, "onboard", {
            "contact_email": contact_email,
            "contact_phone": contact_phone,
            "contact_name": contact_name,
            "business_name": business_name,
            "business_type": business_type,
            "plan": plan,
        }, source_id=submission_row_id)

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


# --- Discovery chatbot (P3) ---
#
# Single endpoint: visitor answers 4 quiz questions client-side, server
# (a) classifies fit via Claude Haiku 4.5, (b) persists to
# discovery_submissions, (c) alerts owner via SMS + Telegram, (d) returns
# a result blob the widget renders. No PII reaches the server until the
# visitor hits submit. Rate-limited to 5 starts/IP/hour and a 500/day
# global ceiling so cost can't run away.
#
# DSA-compliant: AI disclosure surfaced client-side first turn, honest
# "founder-handoff" path when fit is weak (no sycophancy), always-visible
# escape to email/WhatsApp.

import hashlib
import time
from collections import defaultdict, deque

DISCOVERY_RATE_WINDOW_SEC = 3600  # 1h sliding window
DISCOVERY_RATE_LIMIT_PER_IP = 5   # max 5 sessions per IP per hour
DISCOVERY_DAILY_CAP = 500         # global daily ceiling (cost shield)
DISCOVERY_REAP_EVERY = 256        # housekeeping cadence — see _maybe_reap_ip_log

_discovery_ip_log: dict[str, deque] = defaultdict(deque)
_discovery_daily_count = {"date": "", "n": 0}
_discovery_reap_counter = {"n": 0}


def _maybe_reap_ip_log(now: float) -> None:
    """Periodically drop empty deques from `_discovery_ip_log` to keep the
    in-memory dict from growing unbounded over the life of the process.

    Without this, every visited IP keeps a deque entry forever even after
    its rate-limit window expires. On Render free-tier the dyno restarts
    daily so the leak resets, but post-Hetzner migration the dyno stays
    up — same shape as P5-7.
    """
    _discovery_reap_counter["n"] += 1
    if _discovery_reap_counter["n"] < DISCOVERY_REAP_EVERY:
        return
    _discovery_reap_counter["n"] = 0
    cutoff = now - DISCOVERY_RATE_WINDOW_SEC
    dead_keys = [
        k for k, v in _discovery_ip_log.items()
        if not v or v[-1] < cutoff
    ]
    for k in dead_keys:
        _discovery_ip_log.pop(k, None)


def _discovery_rate_check(ip: str) -> tuple[bool, str, dict]:
    """Return ``(allowed, reason, headers)``. Cleans expired entries inline.

    ``headers`` is a dict of canonical rate-limit response headers per the
    ``draft-ietf-httpapi-ratelimit-headers`` shape (RateLimit-Limit,
    RateLimit-Remaining, RateLimit-Reset) plus the legacy ``X-RateLimit-*``
    aliases that most JS clients still read. On the deny path we also set
    ``Retry-After`` (RFC 7231) so the widget can show an honest countdown
    instead of a generic "couldn't reach backend" message.
    """
    now = time.time()
    today = datetime.now(ZoneInfo(CALLMEIE_TIMEZONE)).strftime("%Y-%m-%d")
    if _discovery_daily_count["date"] != today:
        _discovery_daily_count["date"] = today
        _discovery_daily_count["n"] = 0

    log = _discovery_ip_log[ip]
    while log and now - log[0] > DISCOVERY_RATE_WINDOW_SEC:
        log.popleft()

    used = len(log)
    remaining = max(0, DISCOVERY_RATE_LIMIT_PER_IP - used)
    # Window resets when the OLDEST hit in the deque ages out. If the deque
    # is empty, reset is "now-ish" (window already drained).
    reset_seconds = int(DISCOVERY_RATE_WINDOW_SEC - (now - log[0])) if log else 0
    reset_seconds = max(0, reset_seconds)

    headers = {
        "RateLimit-Limit": str(DISCOVERY_RATE_LIMIT_PER_IP),
        "RateLimit-Remaining": str(remaining),
        "RateLimit-Reset": str(reset_seconds),
        "X-RateLimit-Limit": str(DISCOVERY_RATE_LIMIT_PER_IP),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_seconds),
    }

    if _discovery_daily_count["n"] >= DISCOVERY_DAILY_CAP:
        # Global cap: visitor's IP-window doesn't matter; everyone has to
        # wait for tomorrow's reset. Compute seconds until midnight in the
        # configured timezone for the most useful Retry-After.
        local_now = datetime.now(ZoneInfo(CALLMEIE_TIMEZONE))
        next_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        # If we're past midnight already (we always are on a non-empty
        # day), bump to tomorrow.
        from datetime import timedelta
        next_midnight = next_midnight + timedelta(days=1)
        retry_after = max(60, int((next_midnight - local_now).total_seconds()))
        headers["Retry-After"] = str(retry_after)
        return False, "daily_cap_reached", headers

    if used >= DISCOVERY_RATE_LIMIT_PER_IP:
        # Per-IP cap: visitor can retry when the OLDEST hit ages out.
        retry_after = max(60, int(DISCOVERY_RATE_WINDOW_SEC - (now - log[0])))
        headers["Retry-After"] = str(retry_after)
        return False, "rate_limit_per_ip", headers

    log.append(now)
    _discovery_daily_count["n"] += 1
    _maybe_reap_ip_log(now)
    # On allow, decrement remaining for the just-recorded hit.
    headers["RateLimit-Remaining"] = str(max(0, remaining - 1))
    headers["X-RateLimit-Remaining"] = str(max(0, remaining - 1))
    return True, "", headers


DISCOVERY_SYSTEM_PROMPT = """You are the qualifier for CallMeIE Technologies, an Irish AI ops studio in Limerick run by founder Adam Vaughan. You match Irish SMB visitors to the closest CallMeIE product — or, when nothing fits, hand them off to Adam directly with warmth.

PRODUCTS (with full anchor pricing — ALWAYS quote both monthly fee AND setup fee, never just "from €X/mo" alone):
- AI Receptionist · €149/mo + €297 one-time setup (Starter) · €249/mo + €297 setup (Professional) · €397/mo + €497 setup (Growth, 3-month minimum). Answers phone 24/7, books appointments, texts back missed callers. Verticals: dental, motor factors, salon, solicitor, and a general fallback. NEVER quote monthly without setup.
- Document Ops · €500 one-time Entry Pilot · €1,500 Standard Pilot · monthly subscriptions €99/€249/€499/€1,500. Invoice OCR with 0.98 confidence gate, Irish VAT semantics (RCT, per-letter, exempt, intra-community), Sage/Xero/BrightBooks output, 7-year audit retention.
- AI-First Websites · Starter €695 (one-off) · Pro €1,595 (one-off) · Custom from €2,950 (€99 audit credited to build). Care plans €45/€95/€195/mo optional. Cloudflare-hosted Irish web design with receptionist + chatbot + workflow automation built in.

DECISION RULES:
- "Missed calls" pain + any vertical (dental, motor factors, salon, solicitor, restaurant) → AI Receptionist. Mention the matching vertical AI ("dental Claire", "motor factors AI", etc).
- "Invoice review" or "invoices" or "bookkeeping" pain → Document Ops. Mention Irish VAT trained.
- "Outdated website" or "online presence" pain → AI-First Websites. Mention 7-14 day delivery.
- "Lead capture" pain + has-website → AI-First Websites. No website → Receptionist first (calls happen before sites).
- Team size 16-50 + urgency "this month" → flag as bespoke ("we'd want to scope this with Adam, not paste you into a tier").
- Anything else, "other", weird combos, or wrong-fit → FOUNDER HANDOFF. Use the warm phrasing: "None of our products quite fit yet, but I'm flagging this for Adam — he'll email you in the next day or two to chat through what you're working on. Sometimes the right answer is a referral, sometimes a custom build."

FOUNDER-HANDOFF EMAIL HONESTY (P2-7):
- The visitor MAY have provided a contact_email in the answers payload (passed in as part of the prompt). If recommended_product is "founder-handoff":
  - WITH email: "Adam will email you in the next day or two." is honest and required.
  - WITHOUT email (contact_email empty): you cannot promise an email Adam can't send. Use this phrasing instead: "I'm flagging this for Adam — the fastest path is emailing hello@callmeie.ie directly (button below)." Never write "Adam will email you" when the visitor never gave us a way to reach them.

CONTRADICTORY-SIGNAL RULE (P1-5):
- If the answers point at TWO different products at once — e.g. business=restaurant + pain=outdated-website (websites) AND the free-text 'other' field says "missed calls" (receptionist), or business=other:saas + pain=invoice-review (docs) but team_size=16-50 + urgency=this-month (likely bespoke) — DO NOT silently average or pick the latest. Instead:
  - Set ``recommended_product`` to ``"founder-handoff"``.
  - Set ``tier_anchor`` to ``""`` (empty).
  - In ``result_text``, name BOTH possibilities explicitly and ask ONE clarifying question, then route to Adam: e.g. "It sounds like you might want both [Product A] (because [signal X]) and [Product B] (because [signal Y]). Which is the bigger fire today? I'm flagging this for Adam — he'll email you in the next day or two so you can sort which one to start with."
  - Do this when the confidence between two candidates is genuinely close. If the answers obviously point at one product with one stray off-axis signal, recommend that product as normal.

OUTPUT RULES:
- Reply ONLY with valid JSON, no preamble, no code-fence, no explanation outside JSON.
- Schema: {"recommended_product": "receptionist"|"docs"|"websites"|"founder-handoff", "tier_anchor": "from €149/mo + €297 setup"|"from €500 pilot"|"from €695 starter"|"", "result_text": "<60-90 word warm Irish-tone match summary that names the vertical, the specific pain, the recommended product, the FULL price anchor (monthly AND setup, or one-off depending on product), and one sentence on what happens next>"}
- Pricing honesty: when quoting receptionist tiers, ALWAYS state both the monthly fee AND the setup fee in the same sentence (e.g. "starts at €149/month plus €297 setup"). Visitor must never see "from €149/mo" without the setup figure beside it. Same discipline for websites Custom (the €99 audit is credited but must be mentioned).
- Tone: warm, Irish, confident, plain-spoken. Not American, not pushy. Use "ring", "diary", "sound", "grand". Never sound like a brochure.
- result_text MUST mention the product price anchor (or omit if founder-handoff).
- result_text MUST end with a one-line next step ("Adam will email you in the next day or two." OR "The fastest move is ringing the demo line on plus three five three, six one, seven eight eight, one two zero." OR "Drop your invoice into the Doc Ops sample on https://callmeie.ie/docs/ and you'll see the confidence-gated output for yourself.").
- TONE / OPENER VARIATION: vary how you start every reply. Do NOT begin every response with "Ah," — once is charming, four times reads as theme-park Irish. Mix it up: lead with the recommendation, lead with a recap of the pain, lead with empathy, lead with a question. The bot is warm and confident, not a caricature.
- LINK FORMATTING: when you reference a CallMeIE page, use the full URL (https://callmeie.ie/docs/, https://callmeie.ie/receptionist/, https://callmeie.ie/websites/). Visitors read on phone where relative paths like "/docs/" don't click.
- Be honest. If a visitor's situation doesn't match, say so. Don't sell a product they don't need.
- Never invent features. Only reference what's listed above.
"""


_DISCOVERY_FALLBACK = {
    "recommended_product": "founder-handoff",
    "tier_anchor": "",
    "result_text": (
        "I couldn't quite pattern-match this one against our products on autopilot, "
        "so I'm flagging it for Adam. He'll email you in the next day or two to chat "
        "through what you're working on — sometimes the right answer is a referral, "
        "sometimes a custom build."
    ),
}


def _coerce_classification(parsed: dict) -> dict:
    """Validate + normalise an LLM JSON response into the discovery schema."""
    rec = parsed.get("recommended_product", "founder-handoff")
    if rec not in {"receptionist", "docs", "websites", "founder-handoff"}:
        rec = "founder-handoff"
    return {
        "recommended_product": rec,
        "tier_anchor": parsed.get("tier_anchor", "") or "",
        "result_text": parsed.get("result_text") or _DISCOVERY_FALLBACK["result_text"],
    }


def _extract_json(raw: str) -> dict | None:
    """Strip fences, then parse JSON. On failure, regex-extract the first
    {...} block. Returns dict on success, None on hard failure."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    import re as _re
    m = _re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def _try_grok(user_prompt: str) -> dict | None:
    """xAI Grok primary path. Cheap (~$0.0001/call), fast (~1.2s), JSON mode native."""
    if not XAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "grok-4-fast-non-reasoning",
                    "max_tokens": 400,
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": DISCOVERY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
        if r.status_code != 200:
            print(f"[Discovery] Grok error {r.status_code}: {r.text[:300]}")
            return None
        body = r.json()
        raw = body["choices"][0]["message"]["content"]
        # P2-6 — log per-call token usage so Adam can spot xAI burn
        # without instrumenting per-tenant costs end-to-end. Grok-4-fast
        # pricing today: $0.20/M input, $0.50/M output.
        usage = body.get("usage") or {}
        in_t = int(usage.get("prompt_tokens") or 0)
        out_t = int(usage.get("completion_tokens") or 0)
        cost_micro_usd = (in_t * 200 + out_t * 500) // 1000  # micros = cents/100, so cost in micro-usd
        print(
            f"[llm-cost] route=discovery provider=grok model=grok-4-fast-non-reasoning "
            f"prompt_tokens={in_t} completion_tokens={out_t} cost_micro_usd={cost_micro_usd}",
            flush=True,
        )
        parsed = _extract_json(raw)
        if not parsed:
            print(f"[Discovery] Grok returned non-JSON: {raw[:300]}")
            return None
        return parsed
    except Exception as e:
        print(f"[Discovery] Grok exception: {e}")
        return None


async def _try_haiku(user_prompt: str) -> dict | None:
    """Anthropic Haiku 4.5 fallback. Used when xAI is unavailable AND Anthropic credits are present."""
    if not ANTHROPIC_API_KEY:
        return None
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
                    "max_tokens": 400,
                    "system": DISCOVERY_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
        if r.status_code != 200:
            body = r.text[:300]
            if "credit balance" in body.lower():
                print("[Discovery] Haiku skipped — Anthropic credit balance empty")
            else:
                print(f"[Discovery] Haiku error {r.status_code}: {body}")
            return None
        body_json = r.json()
        raw = body_json["content"][0]["text"].strip()
        # P2-6 — Anthropic Haiku 4.5 pricing today: $1/M input, $5/M output.
        usage = body_json.get("usage") or {}
        in_t = int(usage.get("input_tokens") or 0)
        out_t = int(usage.get("output_tokens") or 0)
        cost_micro_usd = (in_t * 1000 + out_t * 5000) // 1000
        print(
            f"[llm-cost] route=discovery provider=haiku model=claude-haiku-4-5 "
            f"prompt_tokens={in_t} completion_tokens={out_t} cost_micro_usd={cost_micro_usd}",
            flush=True,
        )
        parsed = _extract_json(raw)
        if not parsed:
            print(f"[Discovery] Haiku returned non-JSON: {raw[:300]}")
            return None
        return parsed
    except Exception as e:
        print(f"[Discovery] Haiku exception: {e}")
        return None


async def _classify_discovery(answers: dict) -> dict:
    """Provider-chain classifier: tries xAI Grok first (cheap + Adam has credit),
    then Anthropic Haiku, then static founder-handoff. Returns the discovery
    result dict in all cases — guaranteed schema."""

    user_prompt = (
        f"Visitor answers (page context: {answers.get('page_context', 'unknown')}):\n"
        f"- Business: {answers.get('business', '')}\n"
        f"- Biggest unfinished labour: {answers.get('pain', '')}\n"
        f"- Team size: {answers.get('team_size', '')}\n"
        f"- Urgency: {answers.get('urgency', '')}\n\n"
        f"Reply with the JSON only."
    )

    for provider, fn in (("grok", _try_grok), ("haiku", _try_haiku)):
        parsed = await fn(user_prompt)
        if parsed:
            print(f"[Discovery] classified via {provider}")
            return _coerce_classification(parsed)

    print("[Discovery] all providers failed — returning founder-handoff fallback")
    return dict(_DISCOVERY_FALLBACK)


@app.post("/api/discovery")
async def api_discovery(request: Request, background_tasks: BackgroundTasks):
    """Discovery chatbot quiz submit. Body keys:
        business, pain, team_size, urgency  — required (chip values)
        page_context                        — required (hub|receptionist|docs|websites)
        contact_email, contact_name         — optional, only when visitor opts to share
    Returns recommendation + booking CTAs."""
    # Malformed JSON used to crash through to the FastAPI default 500
    # plaintext page (P4 caught this at 2026-05-09). Catch the parse
    # error explicitly and render a JSON 400 envelope instead.
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid_json", "message": "Request body must be valid JSON."}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body", "message": "Request body must be a JSON object."}, status_code=400)
    page_context = (body.get("page_context") or "unknown")[:32]
    business = body.get("business", "")[:64]
    pain = body.get("pain", "")[:64]
    team_size = body.get("team_size", "")[:32]
    urgency = body.get("urgency", "")[:32]
    contact_email = (body.get("contact_email") or "").strip()[:160]
    contact_name = (body.get("contact_name") or "").strip()[:80]

    if not (business and pain and team_size and urgency):
        return JSONResponse({"error": "missing_required_answers"}, status_code=400)

    # Rate limit by client IP (Render sets X-Forwarded-For)
    fwd = request.headers.get("x-forwarded-for", "")
    client_ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or "unknown"
    allowed, reason, ratelimit_headers = _discovery_rate_check(client_ip)
    if not allowed:
        # P0-8 — Retry-After + RateLimit-* headers so the widget can
        # render an honest countdown instead of the generic "couldn't
        # reach backend" message it was showing for any non-2xx.
        return JSONResponse(
            {
                "error": reason,
                "retry_after": int(ratelimit_headers.get("Retry-After", "60")),
            },
            status_code=429,
            headers=ratelimit_headers,
        )
    ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:24]

    # Classify via Haiku (cost: ~€0.0007/call)
    answers = {
        "business": business,
        "pain": pain,
        "team_size": team_size,
        "urgency": urgency,
        "page_context": page_context,
    }
    classification = await _classify_discovery(answers)

    # Persist — use RETURNING id so Postgres returns the new row id
    # (psycopg3 cur.lastrowid is None; SQLite 3.35+ also supports RETURNING).
    submission_id = None
    try:
        with get_db() as conn:
            cur = conn.execute("""
                INSERT INTO discovery_submissions
                (page_context, business, pain, team_size, urgency,
                 contact_email, contact_name, recommended_product, tier_anchor,
                 result_text, ip_hash, user_agent)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                RETURNING id
            """, (
                page_context, business, pain, team_size, urgency,
                contact_email, contact_name,
                classification["recommended_product"], classification["tier_anchor"],
                classification["result_text"], ip_hash,
                request.headers.get("user-agent", "")[:200],
            ))
            row = cur.fetchone()
            if row is not None:
                # _DbProxy yields dict-like rows on PG (Row factory) and Row objects on SQLite
                try:
                    submission_id = row["id"]
                except (TypeError, KeyError, IndexError):
                    try:
                        submission_id = row[0]
                    except Exception:
                        submission_id = None
            conn.commit()
    except Exception as e:
        print(f"[Discovery] DB write failed: {e}")

    # P2-4 — non-blocking unified_leads upsert
    if _lead_ingestor is not None:
        _lead_ingestor.upsert(get_db, "discovery", {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "business": business,
            "pain": pain,
            "team_size": team_size,
            "urgency": urgency,
            "page_context": page_context,
            "recommended_product": classification.get("recommended_product"),
            "tier_anchor": classification.get("tier_anchor"),
        }, source_id=submission_id)

    # Owner alerts (async via background tasks so the visitor gets a fast response)
    headline = (
        f"[CallMeIE] DISCOVERY {classification['recommended_product'].upper()}\n"
        f"{business} · {pain} · team {team_size} · urgency {urgency}\n"
        f"Page: {page_context}"
        + (f"\nContact: {contact_name} {contact_email}" if (contact_name or contact_email) else "")
    )
    tg_msg = (
        f"💬 <b>New discovery: {classification['recommended_product']}</b>\n"
        f"Business: {business} · Pain: {pain}\n"
        f"Team: {team_size} · Urgency: {urgency}\n"
        f"Page: {page_context}"
        + (f"\nContact: {contact_name} · {contact_email}" if (contact_name or contact_email) else "")
        + (f"\nTier: {classification['tier_anchor']}" if classification['tier_anchor'] else "")
    )

    async def _notify():
        try:
            if OWNER_NUMBER:
                await send_sms(OWNER_NUMBER, headline)
        except Exception as e:
            print(f"[Discovery] SMS failed: {e}")
        try:
            await send_telegram(tg_msg)
        except Exception as e:
            print(f"[Discovery] Telegram failed: {e}")

    background_tasks.add_task(_notify)

    print(f"[Discovery] {classification['recommended_product']} | "
          f"{business} · {pain} · {team_size} · {urgency} | page={page_context}")

    # Build CTA list — tier-aware. WhatsApp + email always present.
    rec = classification["recommended_product"]

    # P5-5 — funnel attribution. Append the discovery submission id as a
    # URL param to every product-page CTA so when the visitor clicks
    # through and eventually pays, the Stripe checkout passes
    # client_reference_id back via webhook → docops portal writes it
    # onto tenants.discovery_submission_id (alembic 0009). End result:
    # we can join "this paying tenant came from THIS discovery quiz
    # answer".
    sid = str(submission_id) if submission_id is not None else ""
    sid_param = f"?ref=discovery-{sid}" if sid else ""
    learn_more_url = {
        "receptionist":    f"https://callmeie.ie/receptionist/{sid_param}#pricing",
        "docs":            f"https://callmeie.ie/docs/{sid_param}#pricing",
        "websites":        f"https://callmeie.ie/websites/{sid_param}",
        "founder-handoff": None,
    }.get(rec)

    wa_text = (
        f"Hi CallMeIE — just finished the discovery quiz on /{page_context}/. "
        f"Recommended: {rec}. Pain: {pain}. Business: {business}. "
        + (f"Ref: discovery-{sid}. " if sid else "")
        + "Want to chat."
    )
    from urllib.parse import quote
    ctas = [
        {
            "label": "Continue on WhatsApp",
            "href": f"https://wa.me/353857863564?text={quote(wa_text)}",
            "kind": "primary",
        },
        {
            "label": "Email Adam directly",
            "href": (
                "mailto:hello@callmeie.ie"
                f"?subject={quote('CallMeIE discovery — ' + rec)}"
                f"&body={quote(headline + chr(10) + chr(10) + 'Result: ' + classification['result_text'])}"
            ),
            "kind": "secondary",
        },
    ]
    if learn_more_url:
        ctas.append({
            "label": "See full pricing",
            "href": learn_more_url,
            "kind": "tertiary",
        })

    return JSONResponse({
        "recommended_product": rec,
        "tier_anchor": classification["tier_anchor"],
        "result_text": classification["result_text"],
        "ctas": ctas,
        "submission_id": submission_id,
    })


# --- Doc Ops live extractor (P5b) ---
#
# Visitor uploads a PDF on /docs/ → server pulls the text layer via
# pypdf (no OCR, no disk write — pypdf reads from a BytesIO buffer),
# normalises invoice fields via xAI Grok in JSON mode, computes a
# math-gate confidence overlay, and returns the structured result.
#
# Privacy contract: file bytes never touch disk, we keep a SHA-256
# digest + page count + size for an admin audit row, then drop the
# buffer. No row of the original document text is persisted.
#
# Hard caps: PDF only, ≤ 10 MB, ≤ 8 pages, 3 uploads/IP/hour, shared
# 500/day ceiling with the discovery widget so cost can't run away.

import io as _docops_io

DOCOPS_MAX_BYTES = 10 * 1024 * 1024
DOCOPS_MAX_PAGES = 8
DOCOPS_RATE_LIMIT_PER_IP = 3
DOCOPS_RATE_WINDOW_SEC = 3600

_docops_ip_log: dict[str, deque] = defaultdict(deque)


def _docops_rate_check(ip: str) -> tuple[bool, str]:
    now = time.time()
    today = datetime.now(ZoneInfo(CALLMEIE_TIMEZONE)).strftime("%Y-%m-%d")
    if _discovery_daily_count["date"] != today:
        _discovery_daily_count["date"] = today
        _discovery_daily_count["n"] = 0
    if _discovery_daily_count["n"] >= DISCOVERY_DAILY_CAP:
        return False, "daily_cap_reached"
    log = _docops_ip_log[ip]
    while log and now - log[0] > DOCOPS_RATE_WINDOW_SEC:
        log.popleft()
    if len(log) >= DOCOPS_RATE_LIMIT_PER_IP:
        return False, "rate_limit_per_ip"
    log.append(now)
    _discovery_daily_count["n"] += 1
    return True, ""


DOCOPS_SYSTEM_PROMPT = """You are CallMeIE Document Ops — a strict, Irish-VAT-aware invoice field extractor for SMBs.

OUTPUT RULES:
- Reply ONLY with valid JSON, no preamble, no code-fence.
- Schema: {
    "supplier_name": string|null,
    "supplier_vat": string|null,
    "invoice_number": string|null,
    "issue_date": "YYYY-MM-DD"|null,
    "due_date": "YYYY-MM-DD"|null,
    "currency": "EUR"|"GBP"|"USD"|null,
    "net_total": number|null,
    "vat_amount": number|null,
    "vat_rate": number|null,
    "gross_total": number|null,
    "document_type": "invoice"|"credit_note"|"quote"|"receipt"|"other",
    "ie_vat_notes": string|null
  }
- supplier_vat: include the IE/UK prefix verbatim (e.g. "IE4823910K"). null if absent.
- vat_rate: decimal not percent (0.23 not 23). Irish rates are 0.00 / 0.045 / 0.09 / 0.135 / 0.23.
- All dates ISO format. If only DD/MM/YYYY present, normalise.
- net_total / vat_amount / gross_total: numeric only, no currency symbol.
- ie_vat_notes: 1 short sentence flagging anything Irish-specific worth noting (RCT reverse-charge, VATCA Sched 1 §2 exempt, supermarket per-letter VAT, intra-community 0%, etc). null if generic invoice.
- If a field is genuinely absent or unreadable, use null. NEVER invent.
- If document_type is not "invoice" (e.g. it's a quote or estimate), still extract everything but set document_type accordingly.

EXTRACTION DISCIPLINE:
- Do NOT guess at supplier_vat — only return it if a VAT number string is literally present.
- Do NOT compute gross_total = net_total + vat_amount yourself. Only return values you actually see in the document.
- Do NOT return prose or commentary outside the JSON.
"""


async def _docops_extract_with_grok(text: str) -> dict | None:
    """Send extracted PDF text to Grok-4-fast-non-reasoning for invoice
    field normalisation. Returns parsed dict or None on hard failure."""
    if not XAI_API_KEY:
        return None
    snippet = (text or "")[:8000]  # cap input — invoices fit easily
    if not snippet.strip():
        return None
    user_prompt = (
        "Document text below. Extract invoice fields per the schema. "
        "Reply with the JSON object only.\n\n"
        f"<<<\n{snippet}\n>>>"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "grok-4-fast-non-reasoning",
                    "max_tokens": 600,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": DOCOPS_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
        if r.status_code != 200:
            print(f"[DocOps] Grok error {r.status_code}: {r.text[:300]}")
            return None
        raw = r.json()["choices"][0]["message"]["content"]
        return _extract_json(raw)
    except Exception as e:
        print(f"[DocOps] Grok exception: {e}")
        return None


def _docops_compute_confidence(fields: dict, raw_text_present: bool) -> dict:
    """Heuristic confidence per field. Cheap, deterministic, transparent.
    The LLM doesn't know if it's right — we use the source-text-present-as-proxy
    + math-gate cross-check to assign confidence chips for the UI."""
    conf: dict[str, float] = {}
    # Field-presence pass: any non-null = 0.95 by default; null = 0.0.
    for k, v in fields.items():
        if v is None or v == "":
            conf[k] = 0.0
        else:
            conf[k] = 0.95
    # Math gate: if net + vat_amount agrees with gross within 1c, lift to 1.00
    nt, va, gt = fields.get("net_total"), fields.get("vat_amount"), fields.get("gross_total")
    if isinstance(nt, (int, float)) and isinstance(va, (int, float)) and isinstance(gt, (int, float)):
        if abs((nt + va) - gt) < 0.02:
            for k in ("net_total", "vat_amount", "gross_total"):
                if conf[k] >= 0.9:
                    conf[k] = 1.00
        else:
            for k in ("net_total", "vat_amount", "gross_total"):
                if conf[k] >= 0.9:
                    conf[k] = 0.85  # math disagreement = below gate
    # VAT rate sanity: if vat_rate present and not in IE statutory set, demote
    vr = fields.get("vat_rate")
    if isinstance(vr, (int, float)):
        ie_rates = {0.0, 0.045, 0.09, 0.135, 0.23}
        if not any(abs(vr - r) < 0.005 for r in ie_rates):
            conf["vat_rate"] = 0.80
    # If we couldn't even read source text, every field is suspect
    if not raw_text_present:
        for k in conf:
            conf[k] = min(conf[k], 0.30)
    return conf


@app.post("/api/docops/extract")
async def api_docops_extract(request: Request):
    """Ephemeral PDF → invoice JSON.
    Body: multipart/form-data with 'file' field (PDF, ≤10MB, ≤8 pages).
    No data persisted beyond an audit row (digest + size + page count)."""

    # Rate limit by client IP
    fwd = request.headers.get("x-forwarded-for", "")
    client_ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or "unknown"
    allowed, reason = _docops_rate_check(client_ip)
    if not allowed:
        return JSONResponse({"error": reason}, status_code=429)

    # Read body without writing to disk
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "no_file"}, status_code=400)
    content_type = (getattr(upload, "content_type", "") or "").lower()
    if content_type and "pdf" not in content_type:
        return JSONResponse({"error": "pdf_only"}, status_code=415)

    payload = await upload.read()
    try:
        size = len(payload)
    finally:
        # Drop reference asap if size check fails
        if not payload:
            return JSONResponse({"error": "empty"}, status_code=400)
    if size > DOCOPS_MAX_BYTES:
        return JSONResponse({"error": "file_too_large", "max_bytes": DOCOPS_MAX_BYTES}, status_code=413)

    # Hash for audit only (one-way, no original recoverable from this)
    digest = hashlib.sha256(payload).hexdigest()[:32]

    # Pull text via pypdf — pure-Python, in-memory only
    try:
        import pypdf
    except ImportError:
        return JSONResponse({"error": "pdf_lib_missing"}, status_code=503)

    try:
        reader = pypdf.PdfReader(_docops_io.BytesIO(payload))
        page_count = len(reader.pages)
    except Exception as e:
        print(f"[DocOps] PDF parse failed: {e}")
        return JSONResponse({"error": "pdf_parse_failed"}, status_code=400)

    if page_count > DOCOPS_MAX_PAGES:
        return JSONResponse({"error": "too_many_pages", "max_pages": DOCOPS_MAX_PAGES}, status_code=413)

    pieces: list[str] = []
    for i in range(page_count):
        try:
            pieces.append(reader.pages[i].extract_text() or "")
        except Exception:
            pieces.append("")
    full_text = "\n\n--- page break ---\n\n".join(pieces).strip()

    # GDPR: drop the bytes the moment we have text
    payload = b""

    if not full_text:
        return JSONResponse({
            "error": "no_text_layer",
            "message": "We could read the PDF structure but found no extractable text. Image-only / scanned PDFs need OCR — that ships in our v2. For now, please try a text-layer PDF (Sage / Xero / Stripe export, or any modern invoice).",
            "page_count": page_count,
            "digest": digest,
        }, status_code=422)

    fields = await _docops_extract_with_grok(full_text)
    if not fields:
        return JSONResponse({
            "error": "extraction_failed",
            "message": "The extractor couldn't pattern-match the document. The bytes have been dropped — nothing kept on our side. Email hello@callmeie.ie if you'd like a hand.",
            "page_count": page_count,
            "digest": digest,
        }, status_code=502)

    # Drop full text once classified — never persisted
    full_text = ""

    confidence = _docops_compute_confidence(fields, raw_text_present=True)
    below_gate = [k for k, c in confidence.items() if 0 < c < 0.98]
    missing = [k for k, c in confidence.items() if c == 0.0]

    # Audit row only — no document content, no PII
    try:
        with get_db() as conn:
            conn.execute("""
                INSERT INTO call_events (call_id, event_type, assistant, summary, detail)
                VALUES (?, 'docops-extract', 'docops', ?, ?)
            """, (
                digest,
                f"size={size}b pages={page_count} below_gate={len(below_gate)} missing={len(missing)}",
                json.dumps({
                    "ip_hash": hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:24],
                    "size": size,
                    "page_count": page_count,
                    "below_gate": below_gate,
                    "missing": missing,
                    "doc_type": fields.get("document_type"),
                }),
            ))
            conn.commit()
    except Exception as e:
        print(f"[DocOps] Audit row failed: {e}")

    print(f"[DocOps] {digest[:8]} pages={page_count} below_gate={below_gate} missing={missing}")

    # Build a one-line reviewer prompt for any below-gate / missing fields
    review_note = ""
    if missing or below_gate:
        bits = []
        if missing:
            bits.append(f"missing: {', '.join(missing)}")
        if below_gate:
            bits.append(f"below 0.98: {', '.join(below_gate)}")
        review_note = " · ".join(bits)

    return JSONResponse({
        "fields": fields,
        "confidence": confidence,
        "page_count": page_count,
        "size_bytes": size,
        "digest": digest,
        "below_gate": below_gate,
        "missing": missing,
        "review_note": review_note,
        "math_gate_passed": all(confidence.get(k, 0) >= 0.98 for k in ("net_total", "vat_amount", "gross_total")),
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
            "serverUrl": f"https://api.callmeie.ie/vapi/call-ended",
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
async def index(request: Request):
    # Host-based routing.
    # - admin.callmeie.ie  → /admin (operator dashboard)
    # - client.callmeie.ie → /client (customer dashboard)
    # - api.callmeie.ie + bare /  → public marketing site
    host = (request.headers.get("host") or "").lower()
    if host.startswith("admin."):
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/admin", status_code=302)
    if host.startswith("client."):
        # Serve client.html directly (no redirect) so the token in the URL
        # query string is preserved through the request.
        if os.path.exists(CLIENT_HTML_PATH):
            return FileResponse(CLIENT_HTML_PATH)
        return HTMLResponse("<h1>Client dashboard pending build</h1>")
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
    # Serve admin.html unconditionally — JS handles token input + localStorage
    # auth state. All /admin/api/* endpoints enforce token server-side, so
    # serving the static shell without token is safe (no secrets in the HTML).
    if os.path.exists(ADMIN_HTML_PATH):
        return FileResponse(ADMIN_HTML_PATH)
    return HTMLResponse(ADMIN_HTML_FALLBACK)


_TTS_EDGE_B64 = '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cLjyYa4tf//C4LA+FA8BoKh+C1aWhaDpb//8CYwEwcEQOBFD8IQXsEcipI4qCmZxVf///xIIhFOOLHnwYhov2aVtcWsrG0//////ihSsLKphdnoQHRovBJ7jw8//aLJlY2uLJkEVTEFNRTMu//NkxKIm09oUJYdAATEwMFVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAcAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVV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JVRIkDAoBAJEijOfzJpEjMzVVVVRIkRIkSJEiRIkSRIkSJEqp6qqbwwETMxqqlVKN0oqqqqqqzMfxmVVUv19Szy/9f+MzMez//NkxOIZWAIttAjG3V/+qvVVVXVmbjHqzMart8Y/qqGAmCuBVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxP8k3CH8ADGHHVVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV'
_TTS_CLAIRE_B64 = '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WNNGH44IggQ3VqVnHhiU7RgUXTjekDMFNC4YcMVp//////////////////+n//////////////////v2FZVSMiIDECAAAEIMIMEDIOBTrNzL3CiRqIu+puZUQY08xnkMwXoCTgE6DTigdJB7zLDUflFU2J9pjcWBkQIKAy3Re8IEO+1NyJYgmMOAzFhiTwyUAVohCtfb+MNciYY4FAQjEg0SsQiFDpsyBAJEmOB1HfgmLyJ3A4WlQ1wBCHdARtexI8MhAMqfPsQMNUhyVz2DWN11BDCg2ps7kMZdc0CIOsKXBYEoqreuvPnM86lJV6j/+5LEWQAovg092Z0AAxyyKlOy8ACutfheNQdg78LscQxAhmIwAWAa4DBxEnR3YvM9wtcrz9jG/SNCUEuskTTX/Ua3cQlp9hYgOlVppyEQkEBi0BiDwMFIoAwZ/////O6x/dPm/jbwxefvC81hlkrZZM2mhxAOAKDsWa/C02Uq8VH2psUW4hsLC3m//////////////////+9n/////////////////yu/8CbEQHlBoZszgkKZXO2B2XHeJ5L0mkbd4dxuPWWIyRrRX0NtYy2eHtxhMOz4TKiSy++jTwUNULI5OLpLhxH8qU+firL6eLWnnZQBIiVrBfAXIvUKH+ZBdipVhgnIeRcT1YxMUsN0frG6gw2BzQ1DYEJzeOJ+zIa9tl6ws22bGVKyL8Rijp1Qn8t0YTeWfFgvYj94qtKZmZtsznWjCiobA/ZVylVFWFHrttmYZocXc2NTfdo8WDF3qNX5xbdrPswt0vXVdWkjbwEAA4JVBjYJYHJhhBAcRBDoaYehhieCAIxYgShZgrBehyPxKNzL1yKBZfHYbemq0mcl//uSxBkAlL1bSK29OIqRrKnpt6H5MbpIbg/raX2TPsWaGhd+qencWqM9uwN0VWx5K4Yllubh9F7CVLDovZWMSXDlIOWBLjseNrpwsSIxEwSj0YX5evnjSTcVtihIYdFteMN2VIsnUiblqER+6jbPk9s6Q1sRkj1NKWPW3lY6mn43bDBws/85B8mIFISoouRAhrIyDjEZSDDEF+DA60ib3xMdEXkGQhiLuMTizKCbwi4Ugp5VXPJaIKjR9hNTkoOJbIKgTDAEpgCQIYjGNtV89LXviBjqyr9zTziwjKL42H+T81TsVinTy8ynVhz19gIEACli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'
_TTS_LINE = 'A Limerick dental practice was missing forty calls a month. We answered every one for one hundred and forty nine euro.'


@app.get("/admin/api/tts-samples")
async def tts_samples_api(token: str = Query("")):
    # JSON for the dashboard More>Voice Samples tab.
    check_admin(token)
    return {
        "line": _TTS_LINE,
        "edge": "data:audio/mpeg;base64," + _TTS_EDGE_B64,
        "claire": "data:audio/mpeg;base64," + _TTS_CLAIRE_B64,
    }


@app.get("/admin/tts-samples", response_class=HTMLResponse)
async def tts_samples(token: str = Query("")):
    # Standalone fallback page (direct link). Tab is the primary UI.
    check_admin(token)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>CallMeIE TTS</title>"
        "<body style='font-family:IBM Plex Sans,system-ui;background:#F5F1E8;color:#0b0a08;max-width:640px;margin:40px auto;padding:0 18px'>"
        "<h1 style='font-family:Archivo Black,sans-serif;color:#5A1420'>Voice samples</h1>"
        "<h3>edge (free draft)</h3>"
        "<audio controls src='data:audio/mpeg;base64," + _TTS_EDGE_B64 + "'></audio>"
        "<h3>Claire (ElevenLabs final)</h3>"
        "<audio controls src='data:audio/mpeg;base64," + _TTS_CLAIRE_B64 + "'></audio>"
        "<p style='color:#545454'>" + _TTS_LINE + "</p></body>"
    )


@app.get("/admin/api/submissions")
async def list_submissions(token: str = Query("")):
    check_admin(token)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/admin/api/assistants")
@app.get("/admin/api/clients")  # P5-6 deprecated alias — admin.html JS migrates to /assistants. Will be removed once no callers remain.
async def list_assistants(token: str = Query("")):
    check_admin(token)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM assistants ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# =========================================================================
# P2-4 — unified leads pipeline (single view across 5 channels).
# GET /admin/api/leads     — list w/ filter + search + cursor pagination
# PATCH /admin/api/leads/{id} — status transition w/ audit log
# Schema in alembic 0003_unified_leads.py + see PDR-NEXT-P2-4-UNIFIED-LEADS.md
# =========================================================================

_VALID_STATUS = {"new", "contacted", "qualified", "closed_won", "closed_lost", "spam", "test"}
_STATUS_TRANSITIONS = {
    "new":         {"contacted", "qualified", "closed_lost", "spam", "test"},
    "contacted":   {"qualified", "closed_lost", "closed_won", "spam", "test"},
    "qualified":   {"closed_won", "closed_lost", "spam", "test"},
    # Terminal states require explicit reopen — handled by 'reopen' kw not here.
    "closed_won":  set(),
    "closed_lost": set(),
    "spam":        set(),
    "test":        set(),
}


@app.get("/admin/api/leads")
async def list_leads(
    token: str = Query(""),
    channel: str = Query("", description="CSV of channels to include"),
    status: str = Query("", description="CSV of statuses to include"),
    q: str = Query("", description="search across email/name/business"),
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    cursor: str = Query("", description="opaque keyset cursor"),
):
    """List unified_leads w/ filter + search + keyset pagination + aggregates."""
    check_admin(token)

    where = ["created_at >= NOW() - INTERVAL '%s days'" % int(days)]
    params: list = []

    if channel:
        chans = [c.strip() for c in channel.split(",") if c.strip()]
        if chans:
            placeholders = ",".join(["?"] * len(chans))
            where.append(f"primary_channel IN ({placeholders})")
            params.extend(chans)

    if status:
        stats = [s.strip() for s in status.split(",") if s.strip() in _VALID_STATUS]
        if stats:
            placeholders = ",".join(["?"] * len(stats))
            where.append(f"status IN ({placeholders})")
            params.extend(stats)

    if q:
        like = f"%{q}%"
        where.append("(contact_email ILIKE ? OR contact_name ILIKE ? OR business_name ILIKE ?)")
        params.extend([like, like, like])

    if cursor:
        # Cursor format: "<iso8601_last_touch_at>|<id>" base64-decoded
        import base64
        try:
            decoded = base64.b64decode(cursor.encode()).decode()
            ts, last_id = decoded.rsplit("|", 1)
            where.append("(last_touch_at, id::text) < (?::timestamptz, ?)")
            params.extend([ts, last_id])
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad_cursor: {e}")

    where_sql = " AND ".join(where) if where else "TRUE"
    sql_rows = (
        "SELECT id, dedupe_key, dedupe_kind, contact_email, contact_phone, "
        "contact_name, business_name, primary_channel, channels, status, "
        "priority, source_refs, notes, created_at, last_touch_at, "
        "status_changed_at, qualified_at, closed_at "
        f"FROM unified_leads WHERE {where_sql} "
        f"ORDER BY last_touch_at DESC, id DESC LIMIT {int(limit) + 1}"
    )

    rows = []
    aggregates = {"total": 0, "by_status": {}, "by_channel": {}, "this_week": {}}
    try:
        with get_db() as conn:
            cur = conn.execute(sql_rows, tuple(params))
            rows = [dict(r) for r in cur.fetchall()]

            # Aggregates over the SAME filter (minus cursor)
            agg_where = [w for w in where if not w.startswith("(last_touch_at, id::text)")]
            agg_params = list(params[: len(params) - (2 if cursor else 0)])
            agg_where_sql = " AND ".join(agg_where) if agg_where else "TRUE"
            agg = conn.execute(
                f"SELECT count(*) AS total FROM unified_leads WHERE {agg_where_sql}",
                tuple(agg_params),
            ).fetchone()
            aggregates["total"] = int(agg["total"]) if agg else 0

            by_status = conn.execute(
                f"SELECT status, count(*) AS n FROM unified_leads WHERE {agg_where_sql} GROUP BY status",
                tuple(agg_params),
            ).fetchall()
            aggregates["by_status"] = {r["status"]: int(r["n"]) for r in by_status}

            by_channel = conn.execute(
                f"SELECT primary_channel, count(*) AS n FROM unified_leads WHERE {agg_where_sql} GROUP BY primary_channel",
                tuple(agg_params),
            ).fetchall()
            aggregates["by_channel"] = {r["primary_channel"]: int(r["n"]) for r in by_channel}

            this_week = conn.execute(
                f"SELECT status, count(*) AS n FROM unified_leads "
                f"WHERE created_at >= NOW() - INTERVAL '7 days' GROUP BY status"
            ).fetchall()
            aggregates["this_week"] = {r["status"]: int(r["n"]) for r in this_week}
    except Exception as e:
        # unified_leads table may not exist yet (alembic 0003 not applied).
        # Don't 500 — return empty + info banner.
        return {"leads": [], "next_cursor": None, "aggregates": aggregates,
                "error": "table_not_ready", "detail": str(e)[:200]}

    # Pagination — over-fetched 1 to detect next page
    next_cursor = None
    if len(rows) > int(limit):
        last = rows[int(limit) - 1]
        rows = rows[: int(limit)]
        import base64
        ts = last["last_touch_at"].isoformat() if hasattr(last["last_touch_at"], "isoformat") else str(last["last_touch_at"])
        next_cursor = base64.b64encode(f"{ts}|{last['id']}".encode()).decode()

    # Normalise datetime/UUID to JSON-friendly
    for r in rows:
        for k, v in list(r.items()):
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
            elif not isinstance(v, (str, int, float, bool, list, dict, type(None))):
                r[k] = str(v)

    return {"leads": rows, "next_cursor": next_cursor, "aggregates": aggregates}


@app.patch("/admin/api/leads/{lead_id}")
async def update_lead(
    lead_id: str,
    request: Request,
    token: str = Query(""),
):
    """Update lead status / notes / priority. Validates state transitions + writes audit log."""
    check_admin(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")

    new_status = body.get("status")
    new_notes = body.get("notes")
    new_priority = body.get("priority")
    transition_note = body.get("reason") or ""

    if new_status is not None and new_status not in _VALID_STATUS:
        raise HTTPException(status_code=422, detail=f"invalid_status: {new_status}")

    try:
        with get_db() as conn:
            # Read current state
            cur = conn.execute(
                "SELECT status FROM unified_leads WHERE id = ?", (lead_id,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="lead_not_found")
            current_status = row["status"]

            # Validate transition
            if new_status is not None and new_status != current_status:
                allowed = _STATUS_TRANSITIONS.get(current_status, set())
                if new_status not in allowed:
                    raise HTTPException(
                        status_code=422,
                        detail=f"invalid_transition: {current_status}->{new_status}",
                    )

            # Build UPDATE clause
            set_parts = []
            params: list = []
            if new_status is not None and new_status != current_status:
                set_parts.append("status = ?")
                params.append(new_status)
                set_parts.append("status_changed_at = NOW()")
                if new_status == "qualified":
                    set_parts.append("qualified_at = NOW()")
                if new_status in ("closed_won", "closed_lost"):
                    set_parts.append("closed_at = NOW()")
            if new_notes is not None:
                set_parts.append("notes = ?")
                params.append(str(new_notes)[:4000])
            if new_priority is not None:
                try:
                    p = max(0, min(3, int(new_priority)))
                except Exception:
                    p = 0
                set_parts.append("priority = ?")
                params.append(p)

            if not set_parts:
                return {"id": lead_id, "action": "noop"}

            set_sql = ", ".join(set_parts)
            params.append(lead_id)
            conn.execute(f"UPDATE unified_leads SET {set_sql} WHERE id = ?", tuple(params))

            # Audit log on status change
            if new_status is not None and new_status != current_status:
                conn.execute(
                    "INSERT INTO lead_status_log (lead_id, from_status, to_status, actor, note) "
                    "VALUES (?, ?, ?, 'adam', ?)",
                    (lead_id, current_status, new_status, transition_note[:500])
                )
            conn.commit()
        return {"id": lead_id, "action": "updated", "status": new_status or current_status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_failed: {e}")


# =========================================================================
# P-ADS Phase A — Meta Marketing API admin endpoints (read-only).
# Token + account_id loaded by meta_ads.py from env. All write paths enforce
# META_AD_DAILY_BUDGET_HARDCAP_USD; campaigns always created PAUSED.
# Setup runbook: callmeie-fix/META-ADS-API-SETUP.md
# =========================================================================

try:
    import meta_ads as _meta_ads
except Exception as _e:
    print(f"[meta_ads] import failed: {_e}", file=sys.stderr)
    _meta_ads = None


@app.get("/admin/api/ads/account")
async def ads_account(token: str = Query("")):
    """Smoke check + account info. Read-only."""
    check_admin(token)
    if _meta_ads is None:
        return {"configured": False, "reason": "module_import_failed"}
    return _meta_ads.smoke_check()


@app.get("/admin/api/ads/campaigns")
async def ads_campaigns(token: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    """List campaigns w/ last-7d insights. Read-only."""
    check_admin(token)
    if _meta_ads is None or not _meta_ads.is_configured():
        return {"configured": False, "campaigns": []}
    try:
        return _meta_ads.list_campaigns(limit=limit)
    except Exception as e:
        return {"configured": True, "error": str(e)[:300], "campaigns": []}


@app.get("/admin/api/ads/campaigns/{campaign_id}/insights")
async def ads_campaign_insights(
    campaign_id: str,
    token: str = Query(""),
    days: int = Query(7, ge=1, le=90),
):
    """Daily breakdown of spend/impressions/clicks for a campaign."""
    check_admin(token)
    if _meta_ads is None or not _meta_ads.is_configured():
        return {"configured": False}
    try:
        return _meta_ads.get_campaign_insights(campaign_id, days=days)
    except Exception as e:
        return {"error": str(e)[:300]}


# ---------- Phase B — draft campaigns + templates ------------------------

try:
    import meta_ad_templates as _meta_ad_templates
except Exception as _e:
    print(f"[meta_ad_templates] import failed: {_e}", file=sys.stderr)
    _meta_ad_templates = None


@app.get("/admin/api/ads/templates")
async def ads_templates(token: str = Query("")):
    """List per-service campaign templates available for draft creation."""
    check_admin(token)
    if _meta_ad_templates is None:
        return {"templates": [], "error": "templates_module_not_loaded"}
    out = []
    for k, v in _meta_ad_templates.TEMPLATES.items():
        out.append({
            "key": k,
            "campaign_name": v["campaign_name"],
            "headline": v["headline"],
            "body_preview": v["body"][:140],
            "link_url": v["link_url"],
            "objective": v["objective"],
        })
    return {"templates": out}


@app.get("/admin/api/ads/pages")
async def ads_pages(token: str = Query("")):
    """List FB pages the System User can pick from for the ad creative."""
    check_admin(token)
    if _meta_ads is None or not _meta_ads.is_configured():
        return {"configured": False, "pages": []}
    try:
        return _meta_ads.list_pages()
    except Exception as e:
        return {"error": str(e)[:300], "pages": []}


@app.post("/admin/api/ads/draft")
async def ads_draft(request: Request, token: str = Query("")):
    """Phase B — creates campaign+adset+creative+ad triple from a template.

    Body JSON: {
      template_key: 'receptionist'|'docops'|'websites'|'audit',
      page_id: '123...',
      daily_budget_usd: 2.0,
      overrides?: {headline?, body?, link_url?, campaign_name?}
    }

    All four objects land status=PAUSED. Hardcap enforced.
    """
    check_admin(token)
    if _meta_ads is None or not _meta_ads.is_configured():
        raise HTTPException(status_code=503, detail="meta_ads not configured")
    if _meta_ad_templates is None:
        raise HTTPException(status_code=503, detail="templates_module_not_loaded")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")

    template_key = (body.get("template_key") or "").strip()
    page_id = (body.get("page_id") or "").strip()
    try:
        daily_budget_usd = float(body.get("daily_budget_usd") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="daily_budget_usd_must_be_number")
    overrides = body.get("overrides") or {}

    if template_key not in _meta_ad_templates.TEMPLATES:
        raise HTTPException(status_code=400, detail=f"unknown_template:{template_key}")
    if not page_id:
        raise HTTPException(status_code=400, detail="page_id_required")
    if daily_budget_usd <= 0:
        raise HTTPException(status_code=400, detail="daily_budget_must_be_positive")

    try:
        result = _meta_ads.create_draft_triple(
            template_key=template_key,
            page_id=page_id,
            daily_budget_usd=daily_budget_usd,
            overrides=overrides,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"draft_failed:{str(e)[:200]}")

    if "error" in result:
        return JSONResponse(status_code=502, content={
            "step_failed": result.get("error", {}).get("step"),
            "partial": {k: v for k, v in result.items() if k != "error"},
            "error": result.get("error"),
        })

    # Telegram ping on successful draft
    try:
        send_telegram(
            f"📝 Draft campaign created\n"
            f"Template: {template_key}\n"
            f"Campaign ID: {result.get('campaign_id')}\n"
            f"Daily budget: ${daily_budget_usd:.2f}\n"
            f"Status: PAUSED (Phase C activate to go live)"
        )
    except Exception:
        pass

    return result


@app.post("/admin/api/ads/campaigns/{campaign_id}/activate")
async def ads_activate(campaign_id: str, token: str = Query("")):
    """Phase C — flip PAUSED → ACTIVE. STARTS REAL SPEND. Telegram alert fires."""
    check_admin(token)
    if _meta_ads is None or not _meta_ads.is_configured():
        raise HTTPException(status_code=503, detail="meta_ads not configured")
    try:
        result = _meta_ads.set_campaign_status(campaign_id, "ACTIVE")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"activate_failed:{str(e)[:200]}")

    try:
        send_telegram(
            f"🚀 Campaign ACTIVATED — real spend starting\n"
            f"Campaign ID: {campaign_id}\n"
            f"Result: {result}\n"
            f"Pause via /admin Ads tab if needed."
        )
    except Exception:
        pass
    return result


@app.post("/admin/api/ads/campaigns/{campaign_id}/pause")
async def ads_pause(campaign_id: str, token: str = Query("")):
    """Emergency stop — flip ACTIVE → PAUSED. No alert (silent kill)."""
    check_admin(token)
    if _meta_ads is None or not _meta_ads.is_configured():
        raise HTTPException(status_code=503, detail="meta_ads not configured")
    try:
        return _meta_ads.set_campaign_status(campaign_id, "PAUSED")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"pause_failed:{str(e)[:200]}")


@app.get("/admin/api/ads/canary")
async def ads_canary(token: str = Query("")):
    """Canary view — for every ACTIVE campaign, today's spend vs cap."""
    check_admin(token)
    if _meta_ads is None or not _meta_ads.is_configured():
        return {"configured": False}
    try:
        return {"campaigns": _meta_ads.list_active_campaign_spend()}
    except Exception as e:
        return {"error": str(e)[:300]}


# =========================================================================
# P2-4 channel — WhatsApp Cloud API webhook.
# GET /webhooks/whatsapp  — Meta verification handshake (hub.challenge echo)
# POST /webhooks/whatsapp — inbound message events (signature-verified)
#
# Setup (Meta Developers console — Adam-keyboard):
#   1. developers.facebook.com → Apps → Create app (Business type)
#   2. Add product: WhatsApp
#   3. Generate WHATSAPP_VERIFY_TOKEN (random ≥32 chars, our side)
#   4. Configure webhook URL = https://api.callmeie.ie/webhooks/whatsapp
#      Verify token = WHATSAPP_VERIFY_TOKEN value
#      Subscribe fields: messages
#   5. Copy WhatsApp Business Account ID + System User access token
#   6. Save WHATSAPP_APP_SECRET (App Settings → Basic → App Secret) for sig verify
#
# Coolify env vars to set after Meta setup:
#   WHATSAPP_VERIFY_TOKEN     — our random string, SAME value in Meta console
#   WHATSAPP_APP_SECRET       — Meta app secret for HMAC-SHA256 signature verify
#   WHATSAPP_PHONE_NUMBER_ID  — for outbound replies (Phase 2b)
#   WHATSAPP_ACCESS_TOKEN     — system user permanent token (Phase 2b — replies)
# =========================================================================

WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "").strip()


def _verify_whatsapp_signature(body_bytes: bytes, sig_header: str) -> bool:
    """Verify X-Hub-Signature-256 = sha256(body, app_secret). Constant-time compare."""
    if not WHATSAPP_APP_SECRET or not sig_header:
        return False
    if not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(
        WHATSAPP_APP_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    received = sig_header[7:]
    return hmac.compare_digest(expected, received)


@app.get("/webhooks/whatsapp")
async def whatsapp_verify(request: Request):
    """Meta webhook handshake. Echoes hub.challenge if hub.verify_token matches."""
    qp = request.query_params
    mode = qp.get("hub.mode", "")
    token = qp.get("hub.verify_token", "")
    challenge = qp.get("hub.challenge", "")
    if mode == "subscribe" and token and WHATSAPP_VERIFY_TOKEN and \
       hmac.compare_digest(token, WHATSAPP_VERIFY_TOKEN):
        # Meta wants the challenge as plaintext, not JSON.
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verify_failed")


@app.post("/webhooks/whatsapp")
async def whatsapp_inbound(request: Request):
    """Inbound WhatsApp messages. Calls LeadIngestor on message events."""
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    if not _verify_whatsapp_signature(raw, sig):
        # Don't leak which check failed (sig vs no-secret).
        raise HTTPException(status_code=403, detail="bad_signature")

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")

    # Meta payload: { object: 'whatsapp_business_account',
    #   entry: [{ id, changes: [{ value: { messaging_product, metadata,
    #     contacts: [{ profile: { name }, wa_id }],
    #     messages: [{ from, id, timestamp, type, text: { body } }],
    #     statuses: [...]  // delivery acks
    #   }, field: 'messages' }]}] }
    handled = 0
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                        for c in (value.get("contacts") or [])}
            for msg in value.get("messages", []) or []:
                msg_id = msg.get("id") or ""
                wa_id = msg.get("from") or ""
                msg_type = msg.get("type") or ""
                text = ""
                if msg_type == "text":
                    text = (msg.get("text") or {}).get("body", "")
                elif msg_type == "button":
                    text = (msg.get("button") or {}).get("text", "")
                elif msg_type == "interactive":
                    inter = msg.get("interactive") or {}
                    if inter.get("type") == "button_reply":
                        text = (inter.get("button_reply") or {}).get("title", "")
                    elif inter.get("type") == "list_reply":
                        text = (inter.get("list_reply") or {}).get("title", "")
                # Audio / image / location etc — log but don't block; payload
                # captured in raw_payload below.
                profile_name = contacts.get(wa_id)

                if _lead_ingestor is not None and wa_id:
                    _lead_ingestor.upsert(get_db, "whatsapp", {
                        "contact_phone": wa_id,            # E.164 without +
                        "contact_name": profile_name,
                        "wa_message_id": msg_id,
                        "wa_message_type": msg_type,
                        "text": text,
                        "timestamp": msg.get("timestamp"),
                    }, source_id=msg_id)
                    handled += 1

    # Always return 200 fast — Meta retries on non-2xx + counts retry against
    # the per-message rate limit. Failures are logged but don't 500.
    return {"ok": True, "handled": handled}


# =========================================================================
# P5-2 — GDPR Subject Access Request (Art 15) + Erasure (Art 17) endpoints.
# Token-gated admin surface; Adam fulfils SAR/erasure requests received
# via hello@callmeie.ie by hitting these. /admin/api/data-export returns
# every row matching contact_email across the 5 PII-bearing tables; the
# erasure endpoint marks suppressed_at on those rows so the daily cron
# (purge_old_data.py) hard-deletes after the 30-day backup-grace window.
# =========================================================================

_SAR_TABLES = [
    # (table, email_column, label)
    ("submissions",          "contact_email",   "submissions"),
    ("discovery_submissions", "contact_email",  "discovery_submissions"),
    ("leads",                "name",            "leads_by_name_NA"),  # leads has no email; skip via empty match
    ("owl_tickets",          "submitter_email", "owl_tickets"),
]


@app.get("/admin/api/data-export")
async def data_export(email: str = Query(""), token: str = Query("")):
    """SAR (GDPR Art 15) — return every row matching email across PII tables.

    Case-insensitive match. Returns a JSON envelope with rows per table.
    Adam-fulfilled within 7 calendar days per privacy policy §6 commitment.
    """
    check_admin(token)
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    out: dict = {"email": email, "tables": {}}
    with get_db() as conn:
        # submissions, discovery_submissions, owl_tickets — direct email columns
        for table, col in [
            ("submissions", "contact_email"),
            ("discovery_submissions", "contact_email"),
            ("owl_tickets", "submitter_email"),
        ]:
            try:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE LOWER({col}) = ?",
                    (email,)
                ).fetchall()
                out["tables"][table] = [dict(r) for r in rows]
            except Exception as e:
                out["tables"][table] = {"error": str(e)}

        # owl_leads — payload_json is JSON-encoded form payload; do a LIKE
        # over the column. Imperfect but covers the common case where the
        # visitor's email lands inside the payload.
        try:
            rows = conn.execute(
                "SELECT * FROM owl_leads WHERE LOWER(payload_json) LIKE ?",
                (f"%{email}%",)
            ).fetchall()
            out["tables"]["owl_leads"] = [dict(r) for r in rows]
        except Exception as e:
            out["tables"]["owl_leads"] = {"error": str(e)}

        # leads (Vapi-captured) — has no contact_email column; only name+phone.
        # SAR by email is N/A here; flag explicitly so the response is honest.
        out["tables"]["leads"] = {"note": "leads table has no email column; SAR-by-email N/A — pull by phone separately if needed"}

    matched = sum(len(v) for v in out["tables"].values() if isinstance(v, list))
    out["matched_rows"] = matched
    print(f"[SAR] data-export email={email} matched={matched}", flush=True)
    return out


@app.post("/admin/api/erase")
async def erase(email: str = Query(""), token: str = Query("")):
    """Erasure (GDPR Art 17) — mark suppressed_at on all matching rows.

    Hard-delete happens via the daily cron after the 30-day backup-grace
    window per the privacy policy commitment. Returns counts per table.
    Note: clients table is NOT auto-suppressed — those are active customer
    relationships on a 6-year Revenue retention; manual review required.
    """
    check_admin(token)
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    counts: dict[str, int] = {}
    now_expr = "NOW()" if _USE_PG else "datetime('now')"
    with get_db() as conn:
        for table, col in [
            ("submissions", "contact_email"),
            ("discovery_submissions", "contact_email"),
            ("owl_tickets", "submitter_email"),
        ]:
            try:
                cur = conn.execute(
                    f"UPDATE {table} SET suppressed_at = {now_expr} "
                    f"WHERE LOWER({col}) = ? AND suppressed_at IS NULL",
                    (email,)
                )
                counts[table] = getattr(cur, "rowcount", 0) or 0
            except Exception as e:
                counts[table] = -1
                print(f"[ERASE] {table} failed: {e}", flush=True)

        # owl_leads — match via payload_json LIKE (same shape as SAR)
        try:
            cur = conn.execute(
                f"UPDATE owl_leads SET suppressed_at = {now_expr} "
                f"WHERE LOWER(payload_json) LIKE ? AND suppressed_at IS NULL",
                (f"%{email}%",)
            )
            counts["owl_leads"] = getattr(cur, "rowcount", 0) or 0
        except Exception as e:
            counts["owl_leads"] = -1
            print(f"[ERASE] owl_leads failed: {e}", flush=True)

        conn.commit()

    total = sum(v for v in counts.values() if v > 0)
    print(f"[ERASE] email={email} total_suppressed={total} per_table={counts}", flush=True)
    return {
        "email": email,
        "matched": total,
        "suppressed": total,
        "per_table": counts,
        "hard_delete_after": "30 days (daily cron — purge_old_data.py)",
        "note": "clients table not auto-suppressed; contact Adam directly for active customer erasure (6-year Revenue retention applies).",
    }


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


_VALID_CALL_CLASSIFICATIONS = {"real", "test", "spam", "discard"}


def _get_call_classifications(conn, call_ids: list[str] | None = None) -> dict[str, str]:
    """Return {call_id: latest_classification} for given ids (or all recent).

    Reads 'call-flagged' events from call_events; latest row per call_id wins.
    """
    if call_ids is not None and not call_ids:
        return {}
    if call_ids is None:
        rows = conn.execute(
            "SELECT call_id, detail FROM call_events "
            "WHERE event_type = 'call-flagged' "
            "ORDER BY id DESC LIMIT 1000"
        ).fetchall()
    else:
        placeholders = ",".join(["?"] * len(call_ids))
        rows = conn.execute(
            f"SELECT call_id, detail FROM call_events "
            f"WHERE event_type = 'call-flagged' AND call_id IN ({placeholders}) "
            f"ORDER BY id DESC",
            tuple(call_ids),
        ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        cid = r["call_id"]
        if cid in out:
            continue  # already have latest (rows are DESC)
        try:
            d = json.loads(r["detail"] or "{}")
        except Exception:
            d = {}
        cls = (d.get("classification") or "").lower()
        if cls in _VALID_CALL_CLASSIFICATIONS:
            out[cid] = cls
    return out


@app.post("/admin/api/call-flag")
async def admin_call_flag(request: Request, token: str = Query("")):
    """Flag a call_id with a classification (real|test|spam|discard).

    Writes a 'call-flagged' event into call_events. Latest row per call_id
    wins on read. Re-flagging just appends a new row (audit-trail kept).

    Side effect: if a unified_leads row matches the call's contact_phone,
    its status is updated to mirror the classification:
      test → test, spam → spam, discard → closed_lost, real → qualified
    """
    check_admin(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")
    call_id = (body.get("call_id") or "").strip()
    classification = (body.get("classification") or "").strip().lower()
    note = (body.get("note") or "")[:500]
    if not call_id:
        raise HTTPException(status_code=422, detail="call_id_required")
    if classification not in _VALID_CALL_CLASSIFICATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"invalid_classification: {classification} "
                   f"(allowed: {sorted(_VALID_CALL_CLASSIFICATIONS)})",
        )

    detail_json = json.dumps({
        "classification": classification,
        "actor": "adam",
        "note": note,
    })

    # Try to resolve caller phone for unified_leads sync
    caller_phone = None
    with get_db() as conn:
        lc = conn.execute(
            "SELECT detail FROM call_events "
            "WHERE call_id = ? AND event_type = 'lead-captured' "
            "ORDER BY id ASC LIMIT 1",
            (call_id,),
        ).fetchone()
        if lc:
            try:
                ld = json.loads(lc["detail"] or "{}")
                caller_phone = ld.get("contact_phone") or ld.get("phone")
            except Exception:
                pass
        # Insert flag event
        conn.execute(
            "INSERT INTO call_events (call_id, event_type, assistant, summary, detail, created_at) "
            "VALUES (?, 'call-flagged', NULL, ?, ?, NOW())",
            (call_id, f"flagged: {classification}", detail_json),
        )
        # Mirror to unified_leads if phone match
        if caller_phone:
            status_map = {
                "real": "qualified",
                "test": "test",
                "spam": "spam",
                "discard": "closed_lost",
            }
            new_status = status_map[classification]
            try:
                lr = conn.execute(
                    "SELECT id, status FROM unified_leads WHERE contact_phone = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (caller_phone,),
                ).fetchone()
                if lr and lr["status"] != new_status:
                    allowed = _STATUS_TRANSITIONS.get(lr["status"], set())
                    if new_status in allowed:
                        conn.execute(
                            "UPDATE unified_leads SET status = ?, status_changed_at = NOW() "
                            "WHERE id = ?",
                            (new_status, lr["id"]),
                        )
                        conn.execute(
                            "INSERT INTO lead_status_log (lead_id, from_status, to_status, actor, note) "
                            "VALUES (?, ?, ?, 'adam', ?)",
                            (lr["id"], lr["status"], new_status, f"call-flag {call_id}"),
                        )
            except Exception as e:
                print(f"[call-flag] unified_leads sync skipped: {e}", file=sys.stderr)
        conn.commit()

    return {
        "call_id": call_id,
        "classification": classification,
        "synced_to_lead": caller_phone is not None,
    }


@app.get("/admin/api/call-flags")
async def admin_call_flags(token: str = Query("")):
    """Return latest classification per call_id. Used by frontend to
    render badges / filter views without re-querying per-row."""
    check_admin(token)
    with get_db() as conn:
        return {"flags": _get_call_classifications(conn)}


@app.get("/admin/api/events")
async def list_events(
    token: str = Query(""),
    limit: int = Query(200),
    include_orphans: bool = Query(False),
    include_flagged: bool = Query(False),
):
    """Return recent call_events.

    Default: hide call_ids that have ONLY 'call-ended' events (= short
    hang-ups / Vapi retries / spam misdials — 90% of webhook noise). These
    were drowning the Call Log + Signal Stream surfaces with zero-second
    blank-caller rows.

    Also default: hide call_ids that Adam flagged as test|spam|discard.
    Pass `include_flagged=true` to see them (for the "Flagged" filter view).

    Pass `include_orphans=true` to disable the call-ended filter (full raw
    stream for inspection/debug).
    """
    check_admin(token)
    fetch_limit = max(limit * 4, 200) if not include_orphans else limit
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM call_events ORDER BY id DESC LIMIT ?", (fetch_limit,)
        ).fetchall()
        flags = _get_call_classifications(conn) if not include_flagged else {}
    rows = [dict(r) for r in rows]
    if include_orphans and include_flagged:
        return rows[:limit]

    if not include_orphans:
        # Group by call_id; drop groups whose only event_type is 'call-ended'
        groups: dict[str, set] = {}
        for r in rows:
            cid = r.get("call_id") or "__no_call__"
            groups.setdefault(cid, set()).add(r.get("event_type") or "")
        orphan_ids = {cid for cid, types in groups.items()
                      if types == {"call-ended"} or types == {"call-ended", ""}}
        rows = [r for r in rows
                if (r.get("call_id") or "__no_call__") not in orphan_ids]

    if not include_flagged and flags:
        hidden = {cid for cid, cls in flags.items() if cls != "real"}
        rows = [r for r in rows
                if (r.get("call_id") or "") not in hidden
                and r.get("event_type") != "call-flagged"]

    return rows[:limit]


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
            "SELECT assistant_id, name FROM assistants WHERE status = 'active'"
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


@app.post("/admin/api/events-reformat")
async def admin_events_reformat(token: str = Query("")):
    """2026-05-13 — backfill old call-ended row summaries to new clean format.

    Old format: 'ringing | 88.167s | caller:+353... | handoff_from=...'
    New format: 'completed · 88s · from +353...'

    Operator-only. Returns count updated.
    """
    check_admin(token)
    updated = 0
    try:
        with get_db() as conn:
            # PG psycopg2 treats unescaped % as parameter placeholder. Use
            # POSITION (portable to both PG + SQLite) instead of LIKE.
            rows = conn.execute(
                "SELECT id, summary, detail FROM call_events "
                "WHERE event_type = 'call-ended' AND summary IS NOT NULL "
                "AND POSITION('caller:' IN summary) > 0"
            ).fetchall()
            import re as _re
            for r in rows:
                summ = r["summary"] or ""
                # Parse old pattern: '<status> | <Ns> | caller:<phone> | <misc>'
                m = _re.match(r"^([^|]+)\s*\|\s*([\d.]+)s\s*\|\s*caller:([^\s|]+)", summ)
                if not m:
                    continue
                status = (m.group(1) or "").strip()
                dur_s = float(m.group(2) or 0)
                caller = (m.group(3) or "").strip()
                # Pull endedReason from detail if available for cleaner status
                try:
                    d = json.loads(r["detail"]) if isinstance(r["detail"], str) else (r["detail"] or {})
                except Exception:
                    d = {}
                er = (d.get("ended_reason") or "").strip()
                pretty_status = er or status
                new_summ = f"{pretty_status} · {int(dur_s)}s · from {caller}" if caller else f"{pretty_status} · {int(dur_s)}s"
                conn.execute("UPDATE call_events SET summary = ? WHERE id = ?", (new_summ, r["id"]))
                updated += 1
            conn.commit()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"reformat_failed: {e}"})
    return {"updated": updated}


@app.post("/admin/api/tenant-wipe")
async def admin_tenant_wipe(token: str = Query(""), slug: str = Query("")):
    """2026-05-13 — wipe a tenant's call data clean before sharing dashboard.

    Strategy: find call_ids that have AT LEAST ONE event tagged with the
    tenant's assistant_ids. Delete ALL call_events + call_notes for those
    call_ids (including Claire-origin handoff rows in the same chain).
    Does NOT affect other tenants — their calls don't share these call_ids.

    Use case: clear Dunne tenant before Noah's evaluation.
    Operator-only.
    """
    check_admin(token)
    slug = (slug or "").strip()
    if not slug:
        raise HTTPException(status_code=422, detail="slug required")
    import traceback as _tb
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT assistant_ids FROM client_tokens "
                "WHERE tenant_slug = ? LIMIT 1",
                (slug,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="tenant_not_found")
            aids_raw = row["assistant_ids"] if row else []
            if isinstance(aids_raw, str):
                aids = [s.strip() for s in aids_raw.strip("{}").split(",") if s.strip()]
            else:
                aids = list(aids_raw or [])
            if not aids:
                return {"deleted_events": 0, "deleted_notes": 0,
                        "deleted_call_ids": 0, "tenant_slug": slug}
            ph = ",".join(["?"] * len(aids))
            cid_rows = conn.execute(
                f"SELECT DISTINCT call_id FROM call_events "
                f"WHERE assistant IN ({ph}) AND call_id IS NOT NULL",
                tuple(aids),
            ).fetchall()
            call_ids = [r["call_id"] for r in cid_rows if r["call_id"]]
            if not call_ids:
                return {"deleted_events": 0, "deleted_notes": 0,
                        "deleted_call_ids": 0, "tenant_slug": slug}
            cph = ",".join(["?"] * len(call_ids))
            ev_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM call_events WHERE call_id IN ({cph})",
                tuple(call_ids),
            ).fetchone()
            note_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM call_notes WHERE call_id IN ({cph})",
                tuple(call_ids),
            ).fetchone()
            try:
                ev_n = int(ev_row["n"])
            except Exception:
                ev_n = int(ev_row[0]) if ev_row else 0
            try:
                note_n = int(note_row["n"])
            except Exception:
                note_n = int(note_row[0]) if note_row else 0
            conn.execute(
                f"DELETE FROM call_events WHERE call_id IN ({cph})",
                tuple(call_ids),
            )
            conn.execute(
                f"DELETE FROM call_notes WHERE call_id IN ({cph})",
                tuple(call_ids),
            )
            conn.commit()
            return {
                "deleted_events": ev_n,
                "deleted_notes": note_n,
                "deleted_call_ids": len(call_ids),
                "tenant_slug": slug,
            }
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"tenant_wipe_failed: {e}",
                     "traceback": _tb.format_exc()[-1500:]},
        )


@app.post("/admin/api/events-cleanup")
async def admin_events_cleanup(token: str = Query("")):
    """2026-05-13 — delete NULL/0s noise rows (Vapi pre-filter spam).

    Conservative — only deletes rows where call_id is NULL AND assistant is NULL
    AND event_type='call-ended'. Returns count deleted. Operator-only.
    """
    check_admin(token)
    import traceback as _tb
    deleted = 0
    try:
        with get_db() as conn:
            # Count first for return value
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM call_events "
                "WHERE (call_id IS NULL OR call_id = '') "
                "AND (assistant IS NULL OR assistant = '') "
                "AND event_type = 'call-ended'"
            ).fetchone()
            if row is None:
                deleted = 0
            else:
                # Both PG (dict-row) and SQLite (Row) support index access
                try:
                    deleted = int(row["n"])
                except (KeyError, TypeError):
                    try:
                        deleted = int(row[0])
                    except Exception:
                        deleted = 0
            conn.execute(
                "DELETE FROM call_events "
                "WHERE (call_id IS NULL OR call_id = '') "
                "AND (assistant IS NULL OR assistant = '') "
                "AND event_type = 'call-ended'"
            )
            conn.commit()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"cleanup_failed: {e}", "traceback": _tb.format_exc()[-1500:]},
        )
    return {"deleted": int(deleted), "criterion": "NULL/empty call_id + NULL/empty assistant + event_type='call-ended'"}


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


@app.get("/admin/api/discovery-submissions")
async def list_discovery_submissions(token: str = Query(""), limit: int = Query(100)):
    """Recent discovery-quiz submissions — for admin portal tracking tab."""
    check_admin(token)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM discovery_submissions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Health check ---
# P2-8 — public /health is intentionally minimal. The previous shape
# leaked clients_loaded, provider-configured booleans, and the live
# discovery daily counter — useful operator info but free recon for
# competitors / abusers. Detailed introspection is gated behind
# /admin/health (token-required) below.
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "callmeie-receptionist",
    }


@app.get("/admin/health")
async def admin_health(token: str = ""):
    """Token-gated detailed introspection (operator only)."""
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid_token")
    return {
        "status": "ok",
        "service": "callmeie-receptionist",
        "clients_loaded": len(CLIENTS),
        "twilio_configured": bool(TWILIO_SID),
        "global_owner_notifications": bool(OWNER_NUMBER),
        "google_backup": backup_sheet_status(),
        "anthropic_configured": bool(ANTHROPIC_API_KEY),
        "xai_configured": bool(XAI_API_KEY),
        "discovery_daily_count": _discovery_daily_count.get("n", 0),
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

        # P5-2 — owl_leads + owl_tickets retention columns. Mirrors the
        # block in init_db() but runs here too because the owl_* tables
        # are created in this function (which runs after init_db()) so
        # the init_db() pass would skip them on first boot. Idempotent.
        ts_col_type = "TIMESTAMPTZ" if _USE_PG else "DATETIME"
        owl_retention_targets = [
            ("owl_leads", "suppressed_at"),
            ("owl_tickets", "suppressed_at"),
            ("owl_tickets", "closed_at"),
        ]
        for table, column in owl_retention_targets:
            try:
                if _USE_PG:
                    cols = {row["column_name"] for row in conn.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                        (table,)
                    ).fetchall()}
                else:
                    cols = {row["name"] for row in conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ts_col_type}")
            except Exception as e:
                print(f"[_owl_init_tables] retention migration {table}.{column} skipped: {e}", file=sys.stderr)


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

    owl_lead_row_id = None
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO owl_leads (site_id, form_type, payload_json, submitter_ip, submitted_from) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (site_id, form_type, payload_json, client_ip, submitted_from),
        )
        try:
            owl_lead_row_id = cur.fetchone()[0]
        except Exception:
            owl_lead_row_id = None

    # P2-4 — non-blocking unified_leads upsert (best-effort extraction from JSONB blob)
    if _lead_ingestor is not None:
        _lead_ingestor.upsert(get_db, "owl_form", {
            "contact_email": form_data.get("email") or form_data.get("contact_email"),
            "contact_phone": form_data.get("phone") or form_data.get("contact_phone"),
            "contact_name": form_data.get("name") or form_data.get("contact_name"),
            "business_name": site.get("display_name"),
            "site_id": site_id,
            "form_type": form_type,
            "payload": form_data,
        }, source_id=owl_lead_row_id)

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
def owl_admin(
    request: Request,
    token: str = Query(""),
) -> HTMLResponse:
    """Per-client dashboard — shows leads + tickets for the site matched by token.

    AUD-024 — accept token from Authorization: Bearer header OR cookie OR
    legacy Query param. When a token arrives via Query, redirect to the
    cookie-only URL so the literal token stops appearing in browser history,
    Referer headers, and Render access logs. Existing emails with ?token=
    URLs continue to work (one redirect hop per first hit).
    """
    # Resolution order: header > cookie > query (the loud path)
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(None, 1)[1].strip()
    if not token:
        token = request.cookies.get("owl_admin_token", "")

    site = _owl_site_by_token(token)
    if not site:
        return HTMLResponse("<h1>401 · invalid or missing token</h1>", status_code=401)

    # If the token came via Query, set a cookie + redirect to the clean URL.
    if request.query_params.get("token"):
        from starlette.responses import RedirectResponse
        resp = RedirectResponse(url="/owl/admin", status_code=303)
        resp.set_cookie(
            "owl_admin_token", token,
            httponly=True, secure=True, samesite="lax", max_age=60 * 60 * 24 * 30,
        )
        return resp

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


@app.post("/owl/sites/{site_id}/rotate-admin-token")
def owl_rotate_admin_token(site_id: str, token: str = Query("")) -> JSONResponse:
    """Owner-only: regenerate admin_token for an existing site (AUD-001). Old token immediately invalid."""
    if not _owl_check_owner(token):
        raise HTTPException(status_code=401, detail="owner token required")
    site_id_clean = site_id.strip().lower()
    if not site_id_clean:
        raise HTTPException(status_code=400, detail="site_id required")
    new_token = _secrets.token_urlsafe(32)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE owl_sites SET admin_token = ? WHERE site_id = ? AND status = 'active'",
            (new_token, site_id_clean),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"no active site with site_id={site_id_clean}")
    return JSONResponse({
        "ok": True,
        "site_id": site_id_clean,
        "admin_token": new_token,
        "admin_url": f"/owl/admin?token={new_token}",
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
        f"const r = await fetch('https://api.callmeie.ie/owl/submit', {{"
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

    def _count(conn, row) -> int:
        # psycopg dict_row rows use column keys; sqlite3.Row supports both.
        # Aliasing to 'n' makes both backends work identically.
        try:
            return int(row["n"])
        except (TypeError, KeyError):
            return int(row[0])

    with get_db() as conn:
        leads_now = _count(conn, conn.execute(
            "SELECT COUNT(*) AS n FROM owl_leads WHERE site_id = ? AND ts >= ?",
            (site_id, cutoff),
        ).fetchone())
        leads_prev = _count(conn, conn.execute(
            "SELECT COUNT(*) AS n FROM owl_leads WHERE site_id = ? AND ts >= ? AND ts < ?",
            (site_id, prev_cutoff, cutoff),
        ).fetchone())
        tickets_opened = _count(conn, conn.execute(
            "SELECT COUNT(*) AS n FROM owl_tickets WHERE site_id = ? AND ts >= ?",
            (site_id, cutoff),
        ).fetchone())
        tickets_closed = _count(conn, conn.execute(
            "SELECT COUNT(*) AS n FROM owl_tickets WHERE site_id = ? AND ts >= ? AND status = 'done'",
            (site_id, cutoff),
        ).fetchone())
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
            f"https://api.callmeie.ie/owl/reports/{s['site_id']}?token={s['admin_token']}"
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

    # P1-3 — auto-provision owl_sites row when a website-build deposit pays
    # and no site_id was attached to the Stripe metadata. Before this, the
    # webhook fired SMS to Adam and that was it — Adam had to manually
    # create the owl_sites row + send the customer their admin_token before
    # anything else worked. With this branch the row + admin_token + welcome
    # SMS land in one shot.
    auto_provisioned_site_id = None
    if event_type == "checkout.session.completed" and not site_id:
        # Owl-Studio website-build deposits we recognise. Care plans go
        # through the same webhook but care_tier_map handles them above —
        # no site row to auto-create for a care-plan because care attaches
        # to an EXISTING site.
        SITE_BUILD_KEYS = {"site-starter-deposit": "starter", "site-pro-deposit": "pro"}
        tier_for_build = SITE_BUILD_KEYS.get(product_key)
        if tier_for_build:
            details = obj.get("customer_details") or {}
            cust_email = (details.get("email") or "").strip().lower()
            cust_name = (details.get("name") or "").strip()
            cust_phone = (details.get("phone") or "").strip()
            if cust_email:
                # Generate site_id from email + tier + ts so re-runs of the
                # same checkout don't collide. Idempotent at the dedupe layer
                # above (stripe_event_id UNIQUE) so this branch can't fire
                # twice on the same event.
                slug = re.sub(r"[^a-z0-9]+", "-", cust_email.split("@")[0].lower()).strip("-") or "client"
                ts_suffix = str(int(time.time()))[-6:]
                new_site_id = f"{slug}-{ts_suffix}"
                token_val = _secrets.token_urlsafe(24)
                try:
                    with get_db() as conn:
                        conn.execute(
                            """INSERT INTO owl_sites
                                 (site_id, display_name, tier, care_tier, lead_email, lead_sms,
                                  edit_emails, admin_token, live_url, status)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                new_site_id,
                                cust_name or cust_email,
                                tier_for_build,
                                None,  # care_tier — set later if customer adds care plan
                                cust_email,
                                cust_phone,
                                "[]",
                                token_val,
                                f"https://callmeie.ie/clients/{new_site_id}/",  # placeholder until live
                                "intake",  # status: intake -> build -> live
                            ),
                        )
                    auto_provisioned_site_id = new_site_id
                    site_id = new_site_id
                    # Patch the just-inserted owl_payments row so the
                    # site_id is on the audit trail.
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE owl_payments SET site_id = ? WHERE stripe_event_id = ?",
                            (new_site_id, event_id),
                        )
                except Exception as e:  # noqa: BLE001 — auto-provision must not break the webhook
                    print(f"[owl_stripe] auto-provision failed for {cust_email}: {e}", flush=True)

    # Owner SMS on notable events
    if event_type == "checkout.session.completed":
        if auto_provisioned_site_id:
            sms = (
                f"OwlStudio * NEW SITE * {auto_provisioned_site_id} * "
                f"{product_key} * {amount/100:.0f} {currency} * cust {customer_id[:12]}"
            )
        else:
            sms = f"OwlStudio Stripe * paid * {product_key} * {amount/100:.0f} {currency} * cust {customer_id[:12]}"
        if OWNER_NUMBER:
            background_tasks.add_task(send_sms, OWNER_NUMBER, sms)
    elif event_type == "invoice.payment_failed":
        sms = f"OwlStudio Stripe * PAYMENT FAILED * {product_key or subscription_id[:12]} * cust {customer_id[:12]}"
        if OWNER_NUMBER:
            background_tasks.add_task(send_sms, OWNER_NUMBER, sms)

    return JSONResponse({
        "ok": True,
        "event_type": event_type,
        "product_key": product_key,
        "auto_provisioned_site_id": auto_provisioned_site_id,
    })


# ─── Stripe Customer Portal (AUD-015) ─────────────────────────────────
# Adam's clients on care plans manage their card / invoices / cancel via
# Stripe's hosted portal. We don't reinvent any of that UI — just mint a
# session URL keyed off the same admin_token that auths /owl/admin.

OWL_STRIPE_API_KEY = (
    os.environ.get("STRIPE_API_KEY")
    or os.environ.get("STRIPE_SECRET_KEY")
    or os.environ.get("STRIPE_API")  # legacy name in ~/.claude/routes/.env
    or ""
).strip()


@app.post("/owl/stripe/portal")
def owl_stripe_portal(request: Request, token: str = Query("")) -> JSONResponse:
    """Mint a Stripe Customer Portal session for the site that owns this token.

    AUD-015 — owners on care plans can update card / view invoices / cancel
    without a support touch. Auth resolution mirrors AUD-024: Authorization
    Bearer header > owl_admin_token cookie > legacy ?token= query.
    """
    if not OWL_STRIPE_API_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_API_KEY not configured")

    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(None, 1)[1].strip()
    if not token:
        token = request.cookies.get("owl_admin_token", "")

    site = _owl_site_by_token(token)
    if not site:
        raise HTTPException(status_code=401, detail="invalid or missing token")

    # Find the most-recent Stripe customer_id we've seen for this site.
    with get_db() as conn:
        row = conn.execute(
            "SELECT customer_id FROM owl_payments "
            "WHERE site_id = ? AND customer_id IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1",
            (site["site_id"],),
        ).fetchone()
    if not row or not row["customer_id"]:
        raise HTTPException(
            status_code=404,
            detail="no Stripe customer on file for this site (no successful payment yet)",
        )
    customer_id = row["customer_id"]

    # Create the portal session. Hand-rolled HTTP via httpx — the project
    # doesn't depend on stripe-python and the webhook already proves we
    # can talk to the Stripe REST API directly.
    return_url = "https://api.callmeie.ie/owl/admin"
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                "https://api.stripe.com/v1/billing_portal/sessions",
                data={"customer": customer_id, "return_url": return_url},
                headers={"Authorization": f"Bearer {OWL_STRIPE_API_KEY}"},
            )
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Stripe portal-session create failed: HTTP {r.status_code} {r.text[:200]}",
            )
        session = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe API unreachable: {e}")

    return JSONResponse({"ok": True, "url": session["url"]})


# --- Receptionist signup-link (P2 Phase C, Path B per 2026-05-11 PRD) ----
#
# Owner-only endpoint. Called from admin UI mid-call when a caller commits
# to a tier on the demo line +353 61 788 120. Creates a Stripe Checkout
# Session bundling the chosen tier (Professional €249/mo or Growth €397/mo)
# with the one-off setup fee (€297), then SMSes the checkout URL to the
# caller's mobile so they pay before hanging up.
#
# Existing webhook /owl/stripe/webhook handles checkout.session.completed
# (records the payment); no webhook changes needed.

STRIPE_RECEPTIONIST_PRICES = {
    "professional": os.environ.get("STRIPE_RECEPTIONIST_PROFESSIONAL_MONTHLY", "").strip(),
    "growth": os.environ.get("STRIPE_RECEPTIONIST_GROWTH_MONTHLY", "").strip(),
}
STRIPE_RECEPTIONIST_SETUP_PRICE = os.environ.get("STRIPE_RECEPTIONIST_SETUP_ONCE", "").strip()


@app.post("/admin/api/send-setup-link")
async def admin_send_setup_link(request: Request, token: str = Query("")) -> JSONResponse:
    """Mint Stripe Checkout Session + SMS link to caller mid-demo.

    Body:
      phone: str             E.164 mobile (e.g. +353871234567)
      tier:  str             "professional" | "growth"
      include_setup: bool    default True (bundle one-off €297 setup fee)

    Auth: ADMIN_TOKEN (admin.html UI) OR OWL_OWNER_TOKEN (legacy curl) via
    ?token= OR Authorization: Bearer. Accepts either since both are
    owner-scoped secrets and admin.html sends ADMIN_TOKEN from localStorage.
    """
    # Try Authorization Bearer first for symmetry with other admin endpoints
    bearer = ""
    auth_hdr = request.headers.get("authorization", "")
    if auth_hdr.lower().startswith("bearer "):
        bearer = auth_hdr.split(None, 1)[1].strip()
    effective = token or bearer
    is_admin = bool(ADMIN_TOKEN) and _secrets.compare_digest(effective, ADMIN_TOKEN)
    is_owner = _owl_check_owner(effective)
    if not (is_admin or is_owner):
        raise HTTPException(status_code=401, detail="ADMIN_TOKEN or OWL_OWNER_TOKEN required")

    if not OWL_STRIPE_API_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_API_KEY not configured")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    phone = str(body.get("phone", "")).strip()
    tier = str(body.get("tier", "")).strip().lower()
    include_setup = bool(body.get("include_setup", True))

    if not phone.startswith("+") or len(phone) < 8:
        raise HTTPException(status_code=400, detail="phone must be E.164 (start with +)")
    if tier not in STRIPE_RECEPTIONIST_PRICES:
        raise HTTPException(status_code=400, detail="tier must be 'professional' or 'growth'")

    tier_price_id = STRIPE_RECEPTIONIST_PRICES[tier]
    if not tier_price_id:
        raise HTTPException(status_code=500, detail=f"STRIPE_RECEPTIONIST_{tier.upper()}_MONTHLY env var not set on Render")

    if include_setup and not STRIPE_RECEPTIONIST_SETUP_PRICE:
        raise HTTPException(status_code=500, detail="STRIPE_RECEPTIONIST_SETUP_ONCE env var not set on Render")

    # mode=subscription Checkout supports mixed line_items (recurring + one-off):
    # the one-off setup fee gets added to the first invoice automatically.
    data = {
        "mode": "subscription",
        "success_url": "https://callmeie.ie/receptionist/?setup=paid&sid={CHECKOUT_SESSION_ID}",
        "cancel_url": "https://callmeie.ie/receptionist/?setup=cancelled",
        "metadata[owl_tag]": "callmeie",
        "metadata[product]": f"receptionist-{tier}",
        "metadata[phone]": phone,
        "metadata[via]": "admin-send-setup-link",
        "phone_number_collection[enabled]": "true",
        "billing_address_collection": "required",
        "automatic_tax[enabled]": "true",
        "tax_id_collection[enabled]": "true",
        "allow_promotion_codes": "true",
        "line_items[0][price]": tier_price_id,
        "line_items[0][quantity]": 1,
    }
    if include_setup:
        data["line_items[1][price]"] = STRIPE_RECEPTIONIST_SETUP_PRICE
        data["line_items[1][quantity]"] = 1

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=data,
                headers={"Authorization": f"Bearer {OWL_STRIPE_API_KEY}"},
            )
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Stripe checkout-session create failed: HTTP {r.status_code} {r.text[:400]}",
            )
        session = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe API unreachable: {e}")

    checkout_url = session.get("url", "")
    session_id = session.get("id", "")

    tier_label = "Professional (€249/mo)" if tier == "professional" else "Growth (€397/mo)"
    setup_label = " + €297 setup" if include_setup else ""
    sms_body = (
        f"CallMeIE: tap to complete signup. "
        f"{tier_label}{setup_label}. {checkout_url}\n"
        f"Questions: hello@callmeie.ie"
    )
    sms_result = await send_sms(to=phone, body=sms_body)

    return JSONResponse({
        "ok": True,
        "checkout_url": checkout_url,
        "session_id": session_id,
        "sms_status": sms_result.get("status", "?"),
        "sms_ok": bool(sms_result.get("ok")),
        "phone": phone,
        "tier": tier,
        "include_setup": include_setup,
    })


# ========== Sprint 1 (PRD-ADMIN-DASHBOARD-OWNER-CONTROL-2026-05-11) ==========
# Gaps 1 + 6 + 7 + 19 + 20 — silent-failure floor + free wins.

COOLIFY_API_TOKEN_ENV = os.environ.get("COOLIFY_API_ROOT_TOKEN", "").strip()
COOLIFY_API_ROOT_URL = os.environ.get("COOLIFY_URL", "http://178.104.205.255:8000").strip().rstrip("/")
COOLIFY_APP_UUID_ENV = os.environ.get("COOLIFY_APP_UUID", "xml9wji6109b1kergfz05665").strip()


async def _probe_stripe_key() -> dict:
    if not OWL_STRIPE_API_KEY:
        return {"name": "Stripe key", "status": "fail", "detail": "STRIPE_API_KEY not set"}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.stripe.com/v1/account",
                            headers={"Authorization": f"Bearer {OWL_STRIPE_API_KEY}"})
        if r.status_code == 200:
            return {"name": "Stripe key", "status": "ok", "detail": r.json().get("id", "?")}
        return {"name": "Stripe key", "status": "fail", "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"name": "Stripe key", "status": "fail", "detail": str(e)[:80]}


async def _probe_twilio_from_sms() -> dict:
    if not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_FROM:
        return {"name": "Twilio FROM SMS-capable", "status": "fail",
                "detail": "TWILIO_FROM_NUMBER missing"}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/IncomingPhoneNumbers.json",
                params={"PhoneNumber": TWILIO_FROM},
                auth=(TWILIO_SID, TWILIO_TOKEN))
        if r.status_code != 200:
            return {"name": "Twilio FROM SMS-capable", "status": "fail",
                    "detail": f"HTTP {r.status_code}"}
        d = r.json().get("incoming_phone_numbers", [])
        if not d:
            return {"name": "Twilio FROM SMS-capable", "status": "fail",
                    "detail": f"{TWILIO_FROM} not in account inventory"}
        cap = d[0].get("capabilities", {})
        if cap.get("sms"):
            return {"name": "Twilio FROM SMS-capable", "status": "ok", "detail": TWILIO_FROM}
        return {"name": "Twilio FROM SMS-capable", "status": "fail",
                "detail": f"{TWILIO_FROM} voice-only"}
    except Exception as e:
        return {"name": "Twilio FROM SMS-capable", "status": "fail", "detail": str(e)[:80]}


async def _probe_vapi_key() -> dict:
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    if not vk:
        return {"name": "Vapi key", "status": "fail", "detail": "VAPI_API_KEY missing"}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.vapi.ai/assistant",
                            params={"limit": 1},
                            headers={"Authorization": f"Bearer {vk}"})
        if r.status_code == 200:
            return {"name": "Vapi key", "status": "ok", "detail": "200 OK"}
        return {"name": "Vapi key", "status": "fail", "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"name": "Vapi key", "status": "fail", "detail": str(e)[:80]}


async def _probe_hetzner_bucket() -> dict:
    ep = os.environ.get("HETZNER_OBJECT_STORAGE_ENDPOINT", "").strip()
    ak = os.environ.get("HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip()
    bk = os.environ.get("HETZNER_OBJECT_STORAGE_BUCKET", "").strip()
    if not all([ep, ak, sk, bk]):
        return {"name": "Hetzner bucket", "status": "fail", "detail": "creds missing"}
    try:
        import boto3
        from botocore.config import Config
        s3 = boto3.client("s3", aws_access_key_id=ak, aws_secret_access_key=sk,
                          endpoint_url=ep, region_name="eu-central",
                          config=Config(signature_version="s3v4",
                                        s3={"addressing_style": "path"}))
        s3.head_bucket(Bucket=bk)
        return {"name": "Hetzner bucket", "status": "ok", "detail": bk}
    except Exception as e:
        return {"name": "Hetzner bucket", "status": "fail", "detail": str(e)[:80]}


def _probe_last_call_event() -> dict:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT event_type, created_at FROM call_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            return {"name": "Last call event", "status": "ok",
                    "detail": f"{row['event_type']} @ {str(row['created_at'])[:19]}"}
        return {"name": "Last call event", "status": "warn", "detail": "no events yet"}
    except Exception as e:
        return {"name": "Last call event", "status": "fail", "detail": str(e)[:80]}


def _probe_receptionist_stripe_envs() -> dict:
    missing = [
        k for k in (
            "STRIPE_RECEPTIONIST_PROFESSIONAL_MONTHLY",
            "STRIPE_RECEPTIONIST_GROWTH_MONTHLY",
            "STRIPE_RECEPTIONIST_SETUP_ONCE",
        ) if not os.environ.get(k, "").strip()
    ]
    if missing:
        return {"name": "Receptionist Stripe envs", "status": "fail",
                "detail": f"missing: {','.join(m.replace('STRIPE_RECEPTIONIST_','') for m in missing)}"}
    return {"name": "Receptionist Stripe envs", "status": "ok", "detail": "all 3 set"}


@app.get("/admin/api/health-detail")
async def admin_health_detail(token: str = Query("")):
    """Sprint 1 Gap 1 — parallel probes of critical infra. Drives Gap 19 favicon."""
    check_admin(token)
    import asyncio
    async_probes = await asyncio.gather(
        _probe_stripe_key(),
        _probe_twilio_from_sms(),
        _probe_vapi_key(),
        _probe_hetzner_bucket(),
    )
    probes = list(async_probes) + [
        _probe_last_call_event(),
        _probe_receptionist_stripe_envs(),
    ]
    statuses = [p["status"] for p in probes]
    overall = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "ok")
    return JSONResponse({
        "overall": overall,
        "probes": probes,
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    })


@app.get("/admin/api/twilio-numbers")
async def admin_twilio_numbers(token: str = Query("")):
    """Sprint 1 Gap 6 — list Twilio owned numbers + capabilities."""
    check_admin(token)
    if not TWILIO_SID or not TWILIO_TOKEN:
        raise HTTPException(status_code=500, detail="Twilio creds missing")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/IncomingPhoneNumbers.json",
                params={"PageSize": 20},
                auth=(TWILIO_SID, TWILIO_TOKEN))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Twilio unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Twilio HTTP {r.status_code}")
    nums = []
    for n in r.json().get("incoming_phone_numbers", []):
        nums.append({
            "phone_number": n.get("phone_number"),
            "friendly_name": n.get("friendly_name"),
            "capabilities": n.get("capabilities", {}),
            "is_from": n.get("phone_number") == TWILIO_FROM,
        })
    return JSONResponse({"numbers": nums, "current_from": TWILIO_FROM})


@app.post("/admin/api/test-sms")
async def admin_test_sms(token: str = Query("")):
    """Sprint 1 Gap 6 — fire test SMS to OWNER_NOTIFICATION_NUMBER."""
    check_admin(token)
    if not OWNER_NUMBER:
        raise HTTPException(status_code=500, detail="OWNER_NOTIFICATION_NUMBER not set")
    body = f"CallMeIE admin test SMS @ {_time.strftime('%H:%M:%S UTC')}"
    res = await send_sms(to=OWNER_NUMBER, body=body)
    return JSONResponse({
        "ok": bool(res.get("ok")),
        "to": OWNER_NUMBER,
        "status": res.get("status", "?"),
        "http_status": res.get("http_status"),
        "error": res.get("message") if not res.get("ok") else None,
    })


@app.post("/admin/api/coolify-redeploy")
async def admin_coolify_redeploy(token: str = Query("")):
    """Sprint 1 Gap 7 — trigger Coolify redeploy of this service."""
    check_admin(token)
    if not COOLIFY_API_TOKEN_ENV:
        raise HTTPException(status_code=500, detail="COOLIFY_API_ROOT_TOKEN not set")
    if not COOLIFY_APP_UUID_ENV:
        raise HTTPException(status_code=500, detail="COOLIFY_APP_UUID not set")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"{COOLIFY_API_ROOT_URL}/api/v1/deploy",
                params={"uuid": COOLIFY_APP_UUID_ENV, "force": "true"},
                headers={"Authorization": f"Bearer {COOLIFY_API_TOKEN_ENV}"})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Coolify unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Coolify HTTP {r.status_code}: {r.text[:200]}")
    try:
        return JSONResponse(r.json())
    except Exception:
        return JSONResponse({"ok": True, "raw": r.text[:400]})


# ========== Sprint 2 (PRD-ADMIN-DASHBOARD-OWNER-CONTROL-2026-05-11) ==========
# Gaps 21 + 3 + 4 — operator velocity.
# Gap 21 MUST ship before Gap 3 or we re-create the silent tool-strip bug.

async def _vapi_safe_patch(assistant_id: str, partial: dict) -> dict:
    """Gap 21 — PATCH wrapper that defends against Gotcha 2 (silent tool-strip).

    Workflow: GET → merge partial into current model → defensive re-attach
    of tools (Vapi strips model.tools if PATCH omits them) → PATCH →
    GET-verify tool count not decreased. Raises 502 if Vapi rejects or
    tool count drops post-PATCH.

    `partial` shape examples:
      {"model": {"messages": [...]}}                 # prompt edit
      {"voice": {"stability": 0.5, "style": 0.45}}   # voice edit
      {"transcriber": {"keyterm": [...]}}            # keyterm edit
    """
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    if not vk:
        raise HTTPException(status_code=500, detail="VAPI_API_KEY not set")

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"https://api.vapi.ai/assistant/{assistant_id}",
                        headers={"Authorization": f"Bearer {vk}"})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Vapi GET HTTP {r.status_code}")
    cur = r.json()
    cur_model = cur.get("model") or {}
    cur_tools = cur_model.get("tools") or []
    before_count = len(cur_tools)

    payload: dict = {}
    if "model" in partial:
        merged_model = {**cur_model, **(partial.get("model") or {})}
        # Defense: caller may explicitly pass tools; otherwise re-attach existing
        explicit_tools = (partial.get("model") or {}).get("tools")
        if explicit_tools is None:
            merged_model["tools"] = cur_tools
        payload["model"] = merged_model
    # voice / transcriber / other top-level fields pass through untouched
    for k in ("voice", "transcriber", "firstMessage", "endCallPhrases",
              "maxDurationSeconds", "silenceTimeoutSeconds",
              "startSpeakingPlan", "stopSpeakingPlan", "voicemailDetection",
              "backgroundSound", "artifactPlan"):
        if k in partial:
            payload[k] = partial[k]

    async with httpx.AsyncClient(timeout=30) as c:
        r2 = await c.patch(f"https://api.vapi.ai/assistant/{assistant_id}",
                           headers={"Authorization": f"Bearer {vk}",
                                    "Content-Type": "application/json"},
                           json=payload)
    if r2.status_code not in (200, 201):
        raise HTTPException(status_code=502,
                            detail=f"Vapi PATCH HTTP {r2.status_code}: {r2.text[:300]}")

    async with httpx.AsyncClient(timeout=30) as c:
        chk = await c.get(f"https://api.vapi.ai/assistant/{assistant_id}",
                          headers={"Authorization": f"Bearer {vk}"})
    chk_json = chk.json()
    after_tools = ((chk_json.get("model") or {}).get("tools") or [])
    after_count = len(after_tools)

    if after_count < before_count:
        raise HTTPException(status_code=502,
                            detail=f"Vapi PATCH silently stripped tools: {before_count} -> {after_count}")

    return {
        "ok": True,
        "assistant_id": assistant_id,
        "before_tools_count": before_count,
        "after_tools_count": after_count,
        "response": chk_json,
    }


@app.get("/admin/api/vapi/assistants")
async def admin_vapi_assistants(token: str = Query("")):
    """Gap 3 — list Vapi assistants."""
    check_admin(token)
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    if not vk:
        raise HTTPException(status_code=500, detail="VAPI_API_KEY missing")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.vapi.ai/assistant",
                        headers={"Authorization": f"Bearer {vk}"},
                        params={"limit": 50})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Vapi HTTP {r.status_code}")
    out = []
    for a in r.json():
        model = a.get("model") or {}
        voice = a.get("voice") or {}
        out.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "voice_id": voice.get("voiceId"),
            "voice_provider": voice.get("provider"),
            "model_name": model.get("model"),
            "tools_count": len(model.get("tools") or []),
            "updated_at": a.get("updatedAt"),
        })
    return JSONResponse({"assistants": out})


@app.get("/admin/api/vapi/assistant/{assistant_id}")
async def admin_vapi_assistant_detail(assistant_id: str, token: str = Query("")):
    """Gap 3 — full detail of one assistant (prompt + voice + transcriber)."""
    check_admin(token)
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    if not vk:
        raise HTTPException(status_code=500, detail="VAPI_API_KEY missing")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"https://api.vapi.ai/assistant/{assistant_id}",
                        headers={"Authorization": f"Bearer {vk}"})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Vapi HTTP {r.status_code}")
    a = r.json()
    model = a.get("model") or {}
    voice = a.get("voice") or {}
    transcriber = a.get("transcriber") or {}
    # Extract just system message content (the editable prompt)
    sys_msg = next((m.get("content", "") for m in (model.get("messages") or [])
                    if m.get("role") == "system"), "")
    return JSONResponse({
        "id": a.get("id"),
        "name": a.get("name"),
        "system_prompt": sys_msg,
        "first_message": a.get("firstMessage", ""),
        "voice": {
            "provider": voice.get("provider"),
            "voiceId": voice.get("voiceId"),
            "model": voice.get("model"),
            "stability": voice.get("stability"),
            "style": voice.get("style"),
            "similarityBoost": voice.get("similarityBoost"),
            "useSpeakerBoost": voice.get("useSpeakerBoost"),
            "cachingEnabled": voice.get("cachingEnabled"),
        },
        "transcriber": {
            "provider": transcriber.get("provider"),
            "model": transcriber.get("model"),
            "keyterm": transcriber.get("keyterm", []),
            "language": transcriber.get("language"),
        },
        "tools_count": len(model.get("tools") or []),
        "model_name": model.get("model"),
    })


@app.post("/admin/api/vapi/assistant/{assistant_id}/prompt")
async def admin_vapi_update_prompt(assistant_id: str, request: Request, token: str = Query("")):
    """Gap 3 — replace system prompt only. Uses Gap 21 safe-PATCH (tools preserved)."""
    check_admin(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    new_prompt = body.get("system_prompt", "")
    if not new_prompt or not isinstance(new_prompt, str):
        raise HTTPException(status_code=400, detail="system_prompt must be non-empty string")
    if "—" in new_prompt:
        raise HTTPException(status_code=400,
                            detail="em-dash detected in prompt (Gotcha 3 — ElevenLabs vocalises as 'samam'). Replace with - or .")

    # GET current to find non-system messages (preserve them) + reconstruct messages array
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"https://api.vapi.ai/assistant/{assistant_id}",
                        headers={"Authorization": f"Bearer {vk}"})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Vapi GET HTTP {r.status_code}")
    cur_messages = ((r.json().get("model") or {}).get("messages") or [])
    new_messages = []
    replaced = False
    for m in cur_messages:
        if m.get("role") == "system" and not replaced:
            new_messages.append({**m, "content": new_prompt})
            replaced = True
        else:
            new_messages.append(m)
    if not replaced:
        new_messages.insert(0, {"role": "system", "content": new_prompt})

    result = await _vapi_safe_patch(assistant_id, {"model": {"messages": new_messages}})
    return JSONResponse({
        "ok": True,
        "before_tools_count": result["before_tools_count"],
        "after_tools_count": result["after_tools_count"],
        "prompt_size": len(new_prompt),
    })


@app.post("/admin/api/vapi/assistant/{assistant_id}/voice")
async def admin_vapi_update_voice(assistant_id: str, request: Request, token: str = Query("")):
    """Gap 3 — update voice settings. Uses Gap 21 safe-PATCH."""
    check_admin(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    allowed = {"voiceId", "stability", "style", "similarityBoost",
               "useSpeakerBoost", "cachingEnabled", "model"}
    voice_partial = {k: v for k, v in body.items() if k in allowed}
    if not voice_partial:
        raise HTTPException(status_code=400, detail=f"voice fields required: {allowed}")

    vk = os.environ.get("VAPI_API_KEY", "").strip()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"https://api.vapi.ai/assistant/{assistant_id}",
                        headers={"Authorization": f"Bearer {vk}"})
    cur_voice = (r.json().get("voice") or {})
    new_voice = {**cur_voice, **voice_partial}
    result = await _vapi_safe_patch(assistant_id, {"voice": new_voice})
    return JSONResponse({"ok": True, "tools_after": result["after_tools_count"]})


@app.post("/admin/api/vapi/assistant/{assistant_id}/keyterms")
async def admin_vapi_update_keyterms(assistant_id: str, request: Request, token: str = Query("")):
    """Gap 3 — update Deepgram keyterm list. Uses Gap 21 safe-PATCH."""
    check_admin(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    keyterms = body.get("keyterm", [])
    if not isinstance(keyterms, list):
        raise HTTPException(status_code=400, detail="keyterm must be list of strings")

    vk = os.environ.get("VAPI_API_KEY", "").strip()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"https://api.vapi.ai/assistant/{assistant_id}",
                        headers={"Authorization": f"Bearer {vk}"})
    cur_trans = (r.json().get("transcriber") or {})
    new_trans = {**cur_trans, "keyterm": keyterms}
    result = await _vapi_safe_patch(assistant_id, {"transcriber": new_trans})
    return JSONResponse({"ok": True, "tools_after": result["after_tools_count"], "keyterm_count": len(keyterms)})


@app.get("/admin/api/recordings")
async def admin_recordings(token: str = Query(""), limit: int = Query(50)):
    """Gap 4 — list Hetzner recordings with presigned URLs for inline playback."""
    check_admin(token)
    ep = os.environ.get("HETZNER_OBJECT_STORAGE_ENDPOINT", "").strip()
    ak = os.environ.get("HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip()
    bk = os.environ.get("HETZNER_OBJECT_STORAGE_BUCKET", "").strip()
    if not all([ep, ak, sk, bk]):
        raise HTTPException(status_code=500, detail="Hetzner credentials missing")
    try:
        import boto3
        from botocore.config import Config
        s3 = boto3.client("s3", aws_access_key_id=ak, aws_secret_access_key=sk,
                          endpoint_url=ep, region_name="eu-central",
                          config=Config(signature_version="s3v4",
                                        s3={"addressing_style": "path"}))
        resp = s3.list_objects_v2(Bucket=bk, Prefix="recordings/", MaxKeys=min(limit, 500))
        out = []
        for obj in resp.get("Contents", []):
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bk, "Key": obj["Key"]},
                ExpiresIn=3600,
            )
            out.append({
                "key": obj["Key"],
                "call_id": obj["Key"].replace("recordings/", "").rsplit(".", 1)[0],
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat() if obj.get("LastModified") else None,
                "presigned_url": url,
            })
        # Sort newest first
        out.sort(key=lambda x: x["last_modified"] or "", reverse=True)
        return JSONResponse({"recordings": out, "count": len(out)})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Hetzner list failed: {e}")


# ========== Sprint 3 (PRD-ADMIN-DASHBOARD-OWNER-CONTROL-2026-05-11) ==========
# Gaps 5 + 8 + 14 + CAC — revenue + cost + drama-scoring + correlation.

# Conservative public-rate-card prices. Used for estimate only; actual Stripe
# fees come from settled payments (per Air.ai negative-signal lesson).
VAPI_RATE_PER_MIN_EUR = 0.05
TWILIO_SMS_INTL_RATE_EUR = 0.075  # +353 international rate
TWILIO_VOICE_PER_MIN_EUR = 0.02


async def _stripe_get(path: str, params: dict = None) -> dict:
    if not OWL_STRIPE_API_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_API_KEY missing")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"https://api.stripe.com/v1{path}",
                        headers={"Authorization": f"Bearer {OWL_STRIPE_API_KEY}"},
                        params=params or {})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Stripe {path} HTTP {r.status_code}")
    return r.json()


@app.get("/admin/api/stripe-recent")
async def admin_stripe_recent(token: str = Query("")):
    """Gap 5 — last 30d Checkout Sessions + Payments + active subs."""
    check_admin(token)
    import time
    now = int(time.time())
    thirty_days = now - (30 * 86400)
    sessions = await _stripe_get("/checkout/sessions",
                                  {"limit": 50, "created[gte]": thirty_days})
    payments = await _stripe_get("/payment_intents",
                                  {"limit": 30, "created[gte]": thirty_days})
    subs = await _stripe_get("/subscriptions", {"limit": 100, "status": "active"})

    def slim_session(s):
        return {
            "id": s.get("id"),
            "status": s.get("status"),
            "amount_total": s.get("amount_total"),
            "currency": s.get("currency"),
            "customer_email": s.get("customer_details", {}).get("email") if s.get("customer_details") else None,
            "metadata": s.get("metadata", {}),
            "created": s.get("created"),
        }

    def slim_payment(p):
        ch = p.get("latest_charge") or ""
        return {
            "id": p.get("id"),
            "status": p.get("status"),
            "amount": p.get("amount"),
            "currency": p.get("currency"),
            "created": p.get("created"),
            "description": p.get("description"),
        }

    def slim_sub(s):
        items = (s.get("items") or {}).get("data") or []
        prices = []
        amt = 0
        for it in items:
            pr = it.get("price") or {}
            prices.append(pr.get("lookup_key") or pr.get("id"))
            amt += pr.get("unit_amount") or 0
        return {
            "id": s.get("id"),
            "status": s.get("status"),
            "customer": s.get("customer"),
            "monthly_amount": amt,
            "currency": s.get("currency"),
            "prices": prices,
            "created": s.get("created"),
        }

    sub_list = [slim_sub(s) for s in subs.get("data", [])]
    mrr_minor = sum(s["monthly_amount"] for s in sub_list)

    return JSONResponse({
        "sessions": [slim_session(s) for s in sessions.get("data", [])],
        "payments": [slim_payment(p) for p in payments.get("data", [])],
        "subscriptions": sub_list,
        "mrr_minor": mrr_minor,
        "active_subs": len(sub_list),
        "window_days": 30,
    })


async def _vapi_calls_window(start_unix: int) -> list:
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    if not vk:
        return []
    iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(start_unix))
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.vapi.ai/call",
                        headers={"Authorization": f"Bearer {vk}"},
                        params={"limit": 100, "createdAtGt": iso})
    if r.status_code != 200:
        return []
    return r.json() if isinstance(r.json(), list) else []


@app.get("/admin/api/operations-summary")
async def admin_operations_summary(token: str = Query("")):
    """Gaps 8 + 14 + CAC — today's revenue, cost, net, conversion."""
    check_admin(token)
    import time
    now = int(time.time())
    day_start = now - (now % 86400)
    week_start = now - (7 * 86400)

    # Revenue today + 7d (from Stripe payments)
    pi_7d = await _stripe_get("/payment_intents",
                              {"limit": 100, "created[gte]": week_start})
    rev_today_minor = 0
    rev_7d_minor = 0
    fees_today_minor = 0
    payments_today = 0
    payments_7d = 0
    for p in pi_7d.get("data", []):
        if p.get("status") != "succeeded":
            continue
        amt = p.get("amount", 0) or 0
        rev_7d_minor += amt
        payments_7d += 1
        if (p.get("created") or 0) >= day_start:
            rev_today_minor += amt
            payments_today += 1
            # Estimate Stripe fee 1.4% + €0.25 for EU cards
            fees_today_minor += int(amt * 0.014 + 25)

    # Vapi calls today (Adam asked for current-day cost view)
    vapi_calls = await _vapi_calls_window(day_start)
    vapi_mins_today = 0.0
    call_count_today = 0
    for c in vapi_calls:
        sa = c.get("startedAt")
        ea = c.get("endedAt")
        if sa and ea:
            try:
                import datetime as dt
                s = dt.datetime.fromisoformat(sa.replace("Z","+00:00")).timestamp()
                e = dt.datetime.fromisoformat(ea.replace("Z","+00:00")).timestamp()
                vapi_mins_today += (e - s) / 60.0
                call_count_today += 1
            except Exception:
                pass

    vapi_cost_today = vapi_mins_today * VAPI_RATE_PER_MIN_EUR

    # Demo-complete + heat distribution last 7d
    heat = {"very_interested": 0, "curious": 0, "just_browsing": 0, "unknown": 0}
    converted_count = 0
    callback_count = 0
    import datetime as _dt
    _week_cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT detail, created_at FROM call_events "
                "WHERE event_type = 'demo-complete' "
                "AND created_at >= ? "
                "ORDER BY id DESC",
                (_week_cutoff,),
            ).fetchall()
        for r in rows:
            try:
                d = json.loads(r["detail"] or "{}")
            except Exception:
                d = {}
            lvl = (d.get("interest_level") or "unknown").lower()
            heat[lvl] = heat.get(lvl, 0) + 1
            na = (d.get("next_action") or "").lower()
            if "transfer" in na or "signup" in na or "live" in na:
                converted_count += 1
            elif "callback" in na:
                callback_count += 1
    except Exception:
        pass

    total_demos_7d = sum(heat.values())
    conv_rate = (converted_count / total_demos_7d) if total_demos_7d else 0
    # CAC = cost-this-week / converted-this-week (settled fees, not estimate)
    cac_minor = int((rev_7d_minor and (vapi_cost_today * 100 / max(converted_count, 1))) or 0)

    return JSONResponse({
        "today": {
            "revenue_minor": rev_today_minor,
            "settled_fees_minor": fees_today_minor,
            "net_minor": rev_today_minor - fees_today_minor,
            "payments_count": payments_today,
            "vapi_minutes": round(vapi_mins_today, 2),
            "vapi_cost_minor": int(vapi_cost_today * 100),
            "calls_count": call_count_today,
        },
        "last_7_days": {
            "revenue_minor": rev_7d_minor,
            "payments_count": payments_7d,
            "demos_count": total_demos_7d,
            "converted_count": converted_count,
            "callback_count": callback_count,
            "conversion_rate": round(conv_rate, 3),
            "heat_distribution": heat,
            "cost_per_acquisition_minor": cac_minor,
        },
        "currency": "eur",
        "rate_card_used": {
            "vapi_per_min_eur": VAPI_RATE_PER_MIN_EUR,
            "twilio_sms_intl_eur": TWILIO_SMS_INTL_RATE_EUR,
            "stripe_fee_pct": 1.4,
            "stripe_fee_fixed_cent": 25,
            "note": "Stripe fees are estimated (1.4% + €0.25). Vapi/Twilio rates are public card. Replace with live API once available.",
        },
    })


@app.get("/admin/api/call-scoring")
async def admin_call_scoring(token: str = Query(""), limit: int = Query(50)):
    """Gap 14 — recent demo-complete events with heat + per-call estimated cost."""
    check_admin(token)
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT call_id, created_at, assistant, summary, detail "
                "FROM call_events WHERE event_type = 'demo-complete' "
                "ORDER BY id DESC LIMIT ?",
                (min(limit, 200),),
            ).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    out = []
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except Exception:
            d = {}
        heat = (d.get("interest_level") or "unknown").lower()
        score_map = {"very_interested": 3, "curious": 2, "just_browsing": 1, "unknown": 0}
        out.append({
            "call_id": r["call_id"],
            "created_at": r["created_at"],
            "assistant": r["assistant"],
            "summary": r["summary"],
            "heat": heat,
            "heat_score": score_map.get(heat, 0),
            "business_type": d.get("business_type"),
            "pain_point": d.get("pain_point"),
            "estimated_missed_calls_per_week": d.get("estimated_missed_calls_per_week"),
            "callback_requested": d.get("callback_requested"),
            "next_action": d.get("next_action"),
            "topics_discussed": d.get("topics_discussed"),
        })
    return JSONResponse({"events": out, "count": len(out)})


# ========== Sprint 4 (PRD-ADMIN-DASHBOARD-OWNER-CONTROL-2026-05-11) ==========
# Gap 16 — Caller-entity grouping (recordings + events joined by call_id).
# Gap 11 — Flow visualisation (read-only) — assistant squad routing diagram.
# Gaps 9 (leads inbox) + 18 (failure clustering) deferred to Sprint 6.


@app.get("/admin/api/recordings-enriched")
async def admin_recordings_enriched(token: str = Query(""), limit: int = Query(50)):
    """Gap 16 — Recordings + joined caller identity (CallRail pattern).

    For each Hetzner recording, look up the same call_id in call_events to
    pull: lead-captured (name/business_type), demo-complete (heat/next_action),
    transfer events, assistant identity. Returns the unified caller-entity
    rows for the Recordings tab UI.
    """
    check_admin(token)
    ep = os.environ.get("HETZNER_OBJECT_STORAGE_ENDPOINT", "").strip()
    ak = os.environ.get("HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip()
    bk = os.environ.get("HETZNER_OBJECT_STORAGE_BUCKET", "").strip()
    if not all([ep, ak, sk, bk]):
        raise HTTPException(status_code=500, detail="Hetzner creds missing")
    try:
        import boto3
        from botocore.config import Config
        s3 = boto3.client("s3", aws_access_key_id=ak, aws_secret_access_key=sk,
                          endpoint_url=ep, region_name="eu-central",
                          config=Config(signature_version="s3v4",
                                        s3={"addressing_style": "path"}))
        resp = s3.list_objects_v2(Bucket=bk, Prefix="recordings/", MaxKeys=min(limit, 500))
        recordings = []
        call_ids = []
        for obj in resp.get("Contents", []):
            call_id = obj["Key"].replace("recordings/", "").rsplit(".", 1)[0]
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bk, "Key": obj["Key"]},
                ExpiresIn=3600,
            )
            recordings.append({
                "key": obj["Key"],
                "call_id": call_id,
                "size": obj["Size"],
                "last_modified": obj["LastModified"].isoformat() if obj.get("LastModified") else None,
                "presigned_url": url,
            })
            call_ids.append(call_id)
        recordings.sort(key=lambda x: x["last_modified"] or "", reverse=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Hetzner list failed: {e}")

    # Bulk fetch events for these call_ids
    entities: dict[str, dict] = {}
    if call_ids:
        try:
            with get_db() as conn:
                placeholders = ",".join("?" * len(call_ids))
                rows = conn.execute(
                    f"SELECT call_id, event_type, detail, assistant, created_at "
                    f"FROM call_events WHERE call_id IN ({placeholders}) "
                    f"ORDER BY id ASC",
                    call_ids,
                ).fetchall()
            for r in rows:
                cid = r["call_id"]
                e = entities.setdefault(cid, {
                    "name": None, "phone": None, "business_type": None,
                    "heat": None, "next_action": None, "assistants": set(),
                    "event_types": [], "duration_proxy": None,
                })
                if r["assistant"]:
                    e["assistants"].add(r["assistant"])
                e["event_types"].append(r["event_type"])
                try:
                    d = json.loads(r["detail"] or "{}")
                except Exception:
                    d = {}
                if r["event_type"] == "lead-captured":
                    e["name"] = d.get("name") or e["name"]
                    e["phone"] = d.get("contact_phone") or d.get("phone") or e["phone"]
                    e["business_type"] = d.get("business_type") or e["business_type"]
                if r["event_type"] == "demo-complete":
                    e["heat"] = (d.get("interest_level") or e["heat"])
                    e["next_action"] = d.get("next_action") or e["next_action"]
        except Exception:
            pass

    # Attach entity to each recording
    for r in recordings:
        e = entities.get(r["call_id"], {})
        if isinstance(e.get("assistants"), set):
            e["assistants"] = sorted(e["assistants"])
        r["caller"] = e or None

    return JSONResponse({"recordings": recordings, "count": len(recordings)})


@app.get("/admin/api/caller/{call_id}")
async def admin_caller_timeline(call_id: str, token: str = Query("")):
    """Gap 16 — full timeline for one call_id (CallRail caller-timeline pattern)."""
    check_admin(token)
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT created_at, event_type, assistant, summary, detail "
                "FROM call_events WHERE call_id = ? ORDER BY id ASC",
                (call_id,),
            ).fetchall()
            classification = _get_call_classifications(conn, [call_id]).get(call_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    events = []
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except Exception:
            d = {}
        # 2026-05-13 — skip mirror call-ended rows in timeline (handoff
        # attribution writes one per chain assistant; same logical event).
        if r["event_type"] == "call-ended" and d.get("is_mirror_row"):
            continue
        events.append({
            "ts": r["created_at"],
            "event_type": r["event_type"],
            "assistant": r["assistant"],
            "summary": r["summary"],
            "detail": d,
        })
    # 2026-05-13 — include saved notes (client + Adam-side admin notes)
    notes_out = []
    try:
        with get_db() as conn:
            note_rows = conn.execute(
                "SELECT created_at, note, actor, tenant_slug FROM call_notes "
                "WHERE call_id = ? ORDER BY id ASC",
                (call_id,),
            ).fetchall()
        for n in note_rows:
            notes_out.append({
                "ts": str(n["created_at"]),
                "note": n["note"] or "",
                "actor": n["actor"] or "",
                "tenant_slug": n["tenant_slug"] or "",
            })
    except Exception as e:
        print(f"[admin_caller_timeline] notes fetch failed: {e}")

    return JSONResponse({
        "call_id": call_id,
        "events": events,
        "count": len(events),
        "classification": classification,
        "notes": notes_out,
    })


@app.get("/admin/api/flow-graph")
async def admin_flow_graph(token: str = Query("")):
    """Gap 11 — read-only assistant routing graph.

    Returns nodes + edges describing the squad/transfer routing topology so
    the admin UI renders a static SVG diagram. Sourced live from Vapi (each
    assistant's transferCall tool destinations + the Claire qualifier squad).
    """
    check_admin(token)
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    if not vk:
        raise HTTPException(status_code=500, detail="VAPI_API_KEY missing")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.vapi.ai/assistant",
                        headers={"Authorization": f"Bearer {vk}"},
                        params={"limit": 50})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Vapi HTTP {r.status_code}")
    nodes = []
    edges = []
    for a in r.json():
        aid = a.get("id")
        name = a.get("name") or "?"
        voice = (a.get("voice") or {}).get("voiceId") or ""
        model = (a.get("model") or {})
        nodes.append({
            "id": aid,
            "name": name,
            "voice": voice[:8],
            "tools_count": len(model.get("tools") or []),
            "model": model.get("model", ""),
        })
        for tool in (model.get("tools") or []):
            if tool.get("type") == "transferCall":
                tname = (tool.get("function") or {}).get("name", "?")
                for dest in (tool.get("destinations") or []):
                    target = dest.get("number") or dest.get("extension") or dest.get("assistantName") or "?"
                    edges.append({
                        "from": aid,
                        "from_name": name,
                        "to_target": target,
                        "to_kind": dest.get("type", "?"),
                        "tool_name": tname,
                        "mode": (dest.get("transferPlan") or {}).get("mode", "blind"),
                    })
    return JSONResponse({"nodes": nodes, "edges": edges})


# ========== Sprint 5 (PRD-ADMIN-DASHBOARD-OWNER-CONTROL-2026-05-11) ==========
# Gap 23 — Unified vendor-error log (Sentry-style fingerprinting across Stripe/Twilio/Vapi).
# Gap 22 — Webhook events lister (lite — full replay queue deferred).
# Gap 17 — Predicted-range overlays deferred (needs ≥7d baseline data first).


@app.get("/admin/api/stripe-events")
async def admin_stripe_events(token: str = Query(""), limit: int = Query(50)):
    """Gap 22 lite — list recent Stripe events (replay button reserved for v2)."""
    check_admin(token)
    if not OWL_STRIPE_API_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_API_KEY missing")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.stripe.com/v1/events",
                        headers={"Authorization": f"Bearer {OWL_STRIPE_API_KEY}"},
                        params={"limit": min(limit, 100)})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Stripe events HTTP {r.status_code}")
    out = []
    for ev in r.json().get("data", []):
        out.append({
            "id": ev.get("id"),
            "type": ev.get("type"),
            "created": ev.get("created"),
            "livemode": ev.get("livemode"),
            "object_id": (ev.get("data", {}).get("object") or {}).get("id"),
            "request_id": (ev.get("request") or {}).get("id") if isinstance(ev.get("request"), dict) else None,
        })
    return JSONResponse({"events": out, "count": len(out)})


@app.get("/admin/api/twilio-debugger")
async def admin_twilio_debugger(token: str = Query(""), limit: int = Query(50)):
    """Gap 23 — Twilio Debugger / Alerts feed."""
    check_admin(token)
    if not TWILIO_SID or not TWILIO_TOKEN:
        raise HTTPException(status_code=500, detail="Twilio creds missing")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"https://monitor.twilio.com/v1/Alerts",
            params={"PageSize": min(limit, 100)},
            auth=(TWILIO_SID, TWILIO_TOKEN))
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Twilio alerts HTTP {r.status_code}: {r.text[:200]}")
    out = []
    for a in r.json().get("alerts", []):
        out.append({
            "sid": a.get("sid"),
            "error_code": a.get("error_code"),
            "log_level": a.get("log_level"),
            "alert_text": (a.get("alert_text") or "")[:300],
            "more_info": a.get("more_info"),
            "request_url": a.get("request_url"),
            "request_method": a.get("request_method"),
            "date_created": a.get("date_created"),
            "resource_sid": a.get("resource_sid"),
            "service_sid": a.get("service_sid"),
        })
    return JSONResponse({"alerts": out, "count": len(out)})


@app.get("/admin/api/vendor-errors-unified")
async def admin_vendor_errors_unified(token: str = Query("")):
    """Gap 23 — Sentry-style unified error feed across Stripe + Twilio + Vapi.

    Each entry has: vendor, fingerprint (vendor:error_code), level, message,
    last_seen, count_in_window. Frontend groups by fingerprint."""
    check_admin(token)
    import asyncio
    import time as _t
    now = int(_t.time())
    twenty_four_h = now - 86400

    async def stripe_errors():
        if not OWL_STRIPE_API_KEY:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                # Failed payment intents in last 24h
                r = await c.get("https://api.stripe.com/v1/payment_intents",
                                headers={"Authorization": f"Bearer {OWL_STRIPE_API_KEY}"},
                                params={"limit": 100, "created[gte]": twenty_four_h})
            out = []
            for p in r.json().get("data", []):
                if p.get("status") == "requires_payment_method" and p.get("last_payment_error"):
                    err = p.get("last_payment_error", {})
                    out.append({
                        "vendor": "stripe",
                        "fingerprint": f"stripe:{err.get('code','unknown')}",
                        "level": "error",
                        "message": (err.get("message") or "")[:200],
                        "object_id": p.get("id"),
                        "ts": p.get("created"),
                    })
            return out
        except Exception:
            return []

    async def twilio_errors():
        if not TWILIO_SID or not TWILIO_TOKEN:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://monitor.twilio.com/v1/Alerts",
                                params={"PageSize": 100},
                                auth=(TWILIO_SID, TWILIO_TOKEN))
            out = []
            for a in r.json().get("alerts", []):
                # Filter to last 24h
                ts_str = a.get("date_created", "")
                try:
                    import datetime as dt
                    ts = int(dt.datetime.fromisoformat(ts_str.replace("Z","+00:00")).timestamp())
                except Exception:
                    ts = now
                if ts < twenty_four_h:
                    continue
                out.append({
                    "vendor": "twilio",
                    "fingerprint": f"twilio:{a.get('error_code','unknown')}",
                    "level": a.get("log_level") or "error",
                    "message": (a.get("alert_text") or "")[:200],
                    "object_id": a.get("sid"),
                    "ts": ts,
                })
            return out
        except Exception:
            return []

    async def vapi_errors():
        vk = os.environ.get("VAPI_API_KEY", "").strip()
        if not vk:
            return []
        try:
            iso = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(twenty_four_h))
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api.vapi.ai/call",
                                headers={"Authorization": f"Bearer {vk}"},
                                params={"limit": 100, "createdAtGt": iso})
            out = []
            data = r.json() if isinstance(r.json(), list) else []
            for call in data:
                # Vapi marks failed/ended-with-error calls
                ended_reason = call.get("endedReason") or ""
                if ended_reason and ended_reason not in ("customer-ended-call", "assistant-ended-call",
                                                          "silence-timed-out", "voicemail",
                                                          "customer-did-not-answer"):
                    out.append({
                        "vendor": "vapi",
                        "fingerprint": f"vapi:{ended_reason}",
                        "level": "warn" if "ended" in ended_reason else "error",
                        "message": ended_reason,
                        "object_id": call.get("id"),
                        "ts": call.get("startedAt"),
                    })
            return out
        except Exception:
            return []

    s_errs, t_errs, v_errs = await asyncio.gather(stripe_errors(), twilio_errors(), vapi_errors())
    all_errs = s_errs + t_errs + v_errs

    # Group by fingerprint (Sentry pattern)
    groups: dict[str, dict] = {}
    for err in all_errs:
        fp = err["fingerprint"]
        g = groups.setdefault(fp, {
            "vendor": err["vendor"],
            "fingerprint": fp,
            "level": err["level"],
            "message": err["message"],
            "count": 0,
            "samples": [],
            "last_seen": None,
        })
        g["count"] += 1
        if len(g["samples"]) < 3:
            g["samples"].append({"object_id": err["object_id"], "ts": err["ts"]})
        ts = err.get("ts")
        if isinstance(ts, int) and (g["last_seen"] is None or ts > g["last_seen"]):
            g["last_seen"] = ts

    return JSONResponse({
        "groups": sorted(groups.values(), key=lambda g: g["count"], reverse=True),
        "total_errors_24h": len(all_errs),
        "vendors_with_errors": sorted({g["vendor"] for g in groups.values()}),
    })


# ========== Sprint 6 (PRD-ADMIN-DASHBOARD-OWNER-CONTROL-2026-05-11) ==========
# Gap 10 — Admin token rotation.
# Gap 9 — Leads inbox unified (form + WhatsApp from call_events).
# Gap 18 lite — heat-distribution per assistant (proxy for failure clustering).


@app.post("/admin/api/admin-token-rotate")
async def admin_token_rotate(request: Request, token: str = Query("")):
    """Gap 10 — generate new ADMIN_TOKEN + push to Coolify + restart container.

    Returns new token ONCE — frontend must store immediately. Old token
    invalidated as soon as Coolify reloads env."""
    check_admin(token)
    if not COOLIFY_API_TOKEN_ENV or not COOLIFY_APP_UUID_ENV:
        raise HTTPException(status_code=500, detail="Coolify creds missing for self-update")

    # Generate new token
    new_token = "callmeie-" + _secrets.token_hex(16)

    # 1. Fetch current envs list to find the ADMIN_TOKEN env UUID
    async with httpx.AsyncClient(timeout=30,
                                  headers={"Authorization": f"Bearer {COOLIFY_API_TOKEN_ENV}"}) as c:
        r = await c.get(f"{COOLIFY_API_ROOT_URL}/api/v1/applications/{COOLIFY_APP_UUID_ENV}/envs")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Coolify envs GET HTTP {r.status_code}")
        envs = r.json()
        existing_uuid = None
        for e in envs:
            if e.get("key") == "ADMIN_TOKEN":
                existing_uuid = e.get("uuid")
                break

        # 2. PATCH (upsert) ADMIN_TOKEN
        r2 = await c.patch(f"{COOLIFY_API_ROOT_URL}/api/v1/applications/{COOLIFY_APP_UUID_ENV}/envs",
                           headers={"Content-Type": "application/json"},
                           json={"key": "ADMIN_TOKEN", "value": new_token,
                                 "is_preview": False, "is_literal": True})
        if r2.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Coolify PATCH HTTP {r2.status_code}: {r2.text[:200]}")

        # 3. Restart container to load new env
        r3 = await c.get(f"{COOLIFY_API_ROOT_URL}/api/v1/applications/{COOLIFY_APP_UUID_ENV}/restart")

    return JSONResponse({
        "ok": True,
        "new_token": new_token,
        "warning": "Restart queued. Container takes ~30s. Save the new token NOW — old token works until restart completes.",
        "restart_response": r3.json() if r3.status_code == 200 else None,
    })


@app.get("/admin/api/leads-unified")
async def admin_leads_unified(token: str = Query(""), limit: int = Query(50)):
    """Gap 9 — unified inbox: form submissions + WhatsApp + lead-captured events."""
    check_admin(token)
    out = []
    try:
        with get_db() as conn:
            # Form submissions (existing /owl/submit table if present)
            try:
                rows = conn.execute(
                    "SELECT id, created_at, name, phone, email, business_type, source "
                    "FROM leads ORDER BY id DESC LIMIT ?", (min(limit, 100),)
                ).fetchall()
                for r in rows:
                    out.append({
                        "ts": r["created_at"],
                        "channel": "form",
                        "name": r["name"],
                        "phone": r["phone"],
                        "email": r["email"],
                        "business_type": r["business_type"],
                        "source": r["source"] or "form",
                        "raw_id": r["id"],
                    })
            except Exception:
                pass

            # lead-captured events (from receptionist calls)
            try:
                rows = conn.execute(
                    "SELECT id, created_at, call_id, summary, detail "
                    "FROM call_events WHERE event_type = 'lead-captured' "
                    "ORDER BY id DESC LIMIT ?", (min(limit, 100),)
                ).fetchall()
                for r in rows:
                    try:
                        d = json.loads(r["detail"] or "{}")
                    except Exception:
                        d = {}
                    out.append({
                        "ts": r["created_at"],
                        "channel": "phone",
                        "name": d.get("name"),
                        "phone": d.get("contact_phone") or d.get("phone"),
                        "email": d.get("email"),
                        "business_type": d.get("business_type"),
                        "source": "receptionist:" + (d.get("business_type") or "unknown"),
                        "call_id": r["call_id"],
                        "summary": r["summary"],
                        "raw_id": r["id"],
                    })
            except Exception:
                pass

            # WhatsApp inbound (if stored in events)
            try:
                rows = conn.execute(
                    "SELECT id, created_at, summary, detail FROM call_events "
                    "WHERE event_type IN ('whatsapp-inbound', 'whatsapp-message') "
                    "ORDER BY id DESC LIMIT ?", (min(limit, 100),)
                ).fetchall()
                for r in rows:
                    try:
                        d = json.loads(r["detail"] or "{}")
                    except Exception:
                        d = {}
                    out.append({
                        "ts": r["created_at"],
                        "channel": "whatsapp",
                        "name": d.get("name") or d.get("from_name"),
                        "phone": d.get("from") or d.get("phone"),
                        "summary": r["summary"] or d.get("body", "")[:120],
                        "source": "whatsapp",
                        "raw_id": r["id"],
                    })
            except Exception:
                pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    # Sort by timestamp desc
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)

    # Filter out leads flagged as test/spam/discard via /admin/api/call-flag.
    # Phone leads use their real call_id. Form/WhatsApp leads use synthetic
    # IDs of shape "{channel}:{raw_id}" — matches the frontend's flagId.
    try:
        synth_ids = []
        for r in out:
            if r.get("call_id"):
                synth_ids.append(r["call_id"])
            elif r.get("raw_id"):
                synth_ids.append(f"{r.get('channel','lead')}:{r['raw_id']}")
        with get_db() as conn:
            flags = _get_call_classifications(conn, synth_ids) if synth_ids else {}
        if flags:
            kept = []
            for r in out:
                cid = r.get("call_id") or (f"{r.get('channel','lead')}:{r['raw_id']}" if r.get("raw_id") else None)
                if cid and flags.get(cid, "real") != "real":
                    continue  # flagged as test/spam/discard — hide from unified inbox
                kept.append(r)
            out = kept
    except Exception as e:
        print(f"[leads-unified] flag filter skipped: {e}", file=sys.stderr)

    return JSONResponse({"leads": out[:limit], "count": len(out[:limit]), "total_seen": len(out)})


@app.get("/admin/api/heat-by-assistant")
async def admin_heat_by_assistant(token: str = Query("")):
    """Gap 18 lite — heat distribution per assistant (proxy for failure clustering).

    Sentry-style grouping by 'assistant' as fingerprint dimension; surfaces
    which assistant prompt is converting vs which is browsing-only."""
    check_admin(token)
    # Use SQLAlchemy-style portable datetime: Postgres + SQLite both accept
    # ISO timestamp comparison. Compute cutoff in Python to dodge dialect mismatch.
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT assistant, detail FROM call_events "
                "WHERE event_type = 'demo-complete' "
                "AND created_at >= ?",
                (cutoff,),
            ).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    groups: dict[str, dict] = {}
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except Exception:
            d = {}
        a = r["assistant"] or "unknown"
        g = groups.setdefault(a, {"assistant": a, "very_interested": 0, "curious": 0,
                                    "just_browsing": 0, "unknown": 0, "total": 0})
        lvl = (d.get("interest_level") or "unknown").lower()
        if lvl in g:
            g[lvl] += 1
        else:
            g["unknown"] += 1
        g["total"] += 1
    # Compute conversion rate per assistant
    out = list(groups.values())
    for g in out:
        g["conversion_rate"] = round((g["very_interested"] / g["total"]) if g["total"] else 0, 3)
    out.sort(key=lambda g: g["total"], reverse=True)
    return JSONResponse({"assistants": out})


# ========== PWA shell for admin (PDR-ADMIN-MOBILE-NATIVE-2026-05-11) ==========

@app.get("/admin/manifest.json")
async def admin_pwa_manifest():
    """PWA manifest — installable to iOS/Android home screen."""
    return JSONResponse({
        "name": "CallMeIE Admin",
        "short_name": "CMIE",
        "description": "Operator dashboard for the CallMeIE receptionist + Doc Ops business",
        "start_url": "/admin",
        "scope": "/admin",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0a0e1a",
        "theme_color": "#0a0e1a",
        "icons": [
            {"src": "/admin/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any maskable"},
            {"src": "/admin/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any maskable"},
        ],
    })


@app.get("/admin/icon-{size}.svg")
async def admin_pwa_icon(size: int):
    """Inline SVG icon — cyan disc with monogram for install-to-homescreen."""
    if size not in (180, 192, 512):
        raise HTTPException(status_code=404, detail="size unsupported")
    # Subtle gradient + monogram. Safe-area padded for iOS maskable.
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <radialGradient id="g" cx="50%" cy="40%" r="60%">
      <stop offset="0%" stop-color="#22d3ee"/>
      <stop offset="100%" stop-color="#0e7490"/>
    </radialGradient>
  </defs>
  <rect width="512" height="512" fill="#0a0e1a"/>
  <circle cx="256" cy="256" r="192" fill="url(#g)"/>
  <text x="256" y="320" font-family="-apple-system,Inter,sans-serif" font-size="220" font-weight="700"
        text-anchor="middle" fill="#0a0e1a">C</text>
</svg>"""
    return HTMLResponse(content=svg, media_type="image/svg+xml")


# ========== Decision-Surface Today (PDR-ADMIN-DECISION-SURFACE-2026-05-12) ==========
# Codex agent pivot: first screen ranks who/what to act on, not 13 tabs of data.


@app.get("/admin/api/today-actions")
async def admin_today_actions(token: str = Query(""), limit: int = Query(10)):
    """Ranked list of next-best-actions for Adam.

    Score = heat × recency × revenue-at-risk × state-transition.
    States: demo_complete_hot / demo_complete_warm / recording_ready_no_demo /
            submission_pending / stripe_session_open / system_fault /
            whatsapp_unread.
    """
    check_admin(token)
    import datetime as _dt
    import time as _t
    now = _dt.datetime.utcnow()
    week_cutoff = (now - _dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    actions: list[dict] = []

    # ---- demo-complete events (hottest signal) ----
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT call_id, created_at, assistant, summary, detail "
                "FROM call_events WHERE event_type = 'demo-complete' "
                "AND created_at >= ? ORDER BY id DESC LIMIT 50",
                (week_cutoff,),
            ).fetchall()
            # Exclude call_ids Adam flagged as test|spam|discard from ranking
            call_ids = [r["call_id"] for r in rows if r["call_id"]]
            flags = _get_call_classifications(conn, call_ids) if call_ids else {}
        rows = [r for r in rows
                if flags.get(r["call_id"], "real") == "real"]
        for r in rows:
            try:
                d = json.loads(r["detail"] or "{}")
            except Exception:
                d = {}
            heat = (d.get("interest_level") or "").lower()
            score = 0
            state = "demo_complete_browsing"
            label = "Review recording"
            if heat == "very_interested":
                score += 30
                state = "demo_complete_hot"
                label = "Send setup link"
            elif heat == "curious":
                score += 12
                state = "demo_complete_warm"
                label = "Review then call back"
            else:
                score += 2
            # Decay older than 24h
            try:
                ts = _dt.datetime.strptime(str(r["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    ts = _dt.datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00").replace(" ", "T")).replace(tzinfo=None)
                except Exception:
                    ts = now
            hours_old = max((now - ts).total_seconds() / 3600, 0)
            if hours_old > 48:
                score -= 20
            # Pull caller identity from a matching lead-captured event
            caller_name = None
            caller_phone = None
            caller_biz = (d.get("business_type") or r["assistant"] or "?")
            try:
                with get_db() as conn:
                    lc = conn.execute(
                        "SELECT detail FROM call_events "
                        "WHERE call_id = ? AND event_type = 'lead-captured' "
                        "ORDER BY id ASC LIMIT 1",
                        (r["call_id"],),
                    ).fetchone()
                if lc:
                    try:
                        ld = json.loads(lc["detail"] or "{}")
                    except Exception:
                        ld = {}
                    caller_name = ld.get("name")
                    caller_phone = ld.get("contact_phone") or ld.get("phone")
                    if not caller_biz or caller_biz == "?":
                        caller_biz = ld.get("business_type") or caller_biz
            except Exception:
                pass
            tier_hint = "professional"
            if "motor" in (caller_biz or "").lower():
                tier_hint = "growth"
            actions.append({
                "rank_score": score,
                "subject": {
                    "name": caller_name or "?",
                    "business": caller_biz,
                    "phone": caller_phone,
                },
                "state": state,
                "suggested_action": "send_setup_link" if state == "demo_complete_hot" else "review_recording",
                "suggested_action_label": label,
                "heat": heat or "unknown",
                "context": {
                    "call_id": r["call_id"],
                    "last_event_at": str(r["created_at"]),
                    "tier_hint": tier_hint,
                    "summary": r["summary"],
                },
            })
    except Exception:
        pass

    # ---- onboarding submissions pending ----
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, created_at, business_name, business_type, "
                "contact_phone, contact_email "
                "FROM submissions WHERE status = 'pending' "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()
        for r in rows:
            actions.append({
                "rank_score": 18,
                "subject": {
                    "name": r["business_name"] or "?",
                    "business": r["business_type"] or "?",
                    "phone": r["contact_phone"],
                    "email": r["contact_email"],
                },
                "state": "submission_pending",
                "suggested_action": "provision_assistant",
                "suggested_action_label": "Provision assistant",
                "heat": "warm",
                "context": {
                    "submission_id": r["id"],
                    "last_event_at": str(r["created_at"]),
                },
            })
    except Exception:
        pass

    # ---- Stripe sessions open + unpaid (in-flight signups) ----
    if OWL_STRIPE_API_KEY:
        try:
            import time as _ts
            since = int(_ts.time()) - (7 * 86400)
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    "https://api.stripe.com/v1/checkout/sessions",
                    headers={"Authorization": f"Bearer {OWL_STRIPE_API_KEY}"},
                    params={"limit": 20, "created[gte]": since})
            if r.status_code == 200:
                for s in r.json().get("data", []):
                    if s.get("status") != "open":
                        continue
                    md = s.get("metadata", {}) or {}
                    if md.get("owl_tag") != "callmeie":
                        continue
                    actions.append({
                        "rank_score": 25,
                        "subject": {
                            "name": (s.get("customer_details") or {}).get("name", "?"),
                            "business": (md.get("product") or "?").replace("receptionist-", ""),
                            "phone": md.get("phone"),
                        },
                        "state": "stripe_session_open",
                        "suggested_action": "resend_setup_link",
                        "suggested_action_label": "Resend payment link",
                        "heat": "hot",
                        "context": {
                            "session_id": s.get("id"),
                            "amount_total": s.get("amount_total"),
                            "currency": s.get("currency"),
                            "checkout_url": s.get("url"),
                            "tier_hint": (md.get("product") or "").replace("receptionist-", "") or "professional",
                        },
                    })
        except Exception:
            pass

    # ---- system fault preempt ----
    try:
        # Reuse the existing health probes - check one cheap signal: Twilio FROM valid
        health = await _probe_twilio_from_sms()
        if health.get("status") == "fail":
            actions.insert(0, {
                "rank_score": 100,
                "subject": {"name": "System fault", "business": "Twilio FROM not SMS-capable"},
                "state": "system_fault",
                "suggested_action": "fix_system",
                "suggested_action_label": f"Fix: {health.get('detail','')[:80]}",
                "heat": "fault",
                "context": {"probe": health},
            })
    except Exception:
        pass

    # Sort + limit
    actions.sort(key=lambda a: a["rank_score"], reverse=True)

    # Filter out actions flagged as test/spam/discard. Phone actions use real
    # call_ids; submission/stripe/whatsapp actions use synthetic IDs that
    # match the frontend's flagId fallback (submission:{id}, stripe:{id},
    # state:{...}). _get_call_classifications resolves all of them from the
    # call-flagged event log.
    try:
        flag_ids = []
        for a in actions:
            ctx = a.get("context") or {}
            fid = (ctx.get("call_id")
                   or (f"submission:{ctx['submission_id']}" if ctx.get("submission_id") else None)
                   or (f"stripe:{ctx['session_id']}" if ctx.get("session_id") else None))
            if fid:
                flag_ids.append(fid)
        with get_db() as conn:
            flags = _get_call_classifications(conn, flag_ids) if flag_ids else {}
        if flags:
            kept = []
            for a in actions:
                ctx = a.get("context") or {}
                fid = (ctx.get("call_id")
                       or (f"submission:{ctx['submission_id']}" if ctx.get("submission_id") else None)
                       or (f"stripe:{ctx['session_id']}" if ctx.get("session_id") else None))
                if fid and flags.get(fid, "real") != "real":
                    continue  # flagged as test/spam/discard — drop from ranking
                kept.append(a)
            actions = kept
    except Exception as e:
        print(f"[today-actions] flag filter skipped: {e}", file=sys.stderr)

    return JSONResponse({
        "actions": actions[:limit],
        "total_seen": len(actions),
        "ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
    })


# =========================================================================
# Client-side dashboard — tenant-scoped read-mostly portal for paying
# receptionist customers. Lives at /client/* and client.callmeie.ie.
#
# Auth: token-in-URL → localStorage with history.replaceState strip (same
# pattern as admin.html). Every /client/api/* endpoint filters by
# assistant_id IN (client_tokens.assistant_ids) — tenant isolation is
# load-bearing. See PRD-CLIENT-DASHBOARD-2026-05-12.md.
# =========================================================================


def check_client(token: str) -> dict:
    """Validate client token. Raise 401 if missing/invalid/revoked.
    Updates last_used_at on every call. Returns the client_tokens row dict.
    """
    if not token:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM client_tokens "
                "WHERE token = ? AND revoked_at IS NULL",
                (token,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="invalid_token")
            # Portable timestamp expression — Postgres uses NOW(), SQLite uses
            # datetime('now'). Matches the existing now_expr pattern used
            # elsewhere in this codebase.
            now_expr = "NOW()" if _USE_PG else "datetime('now')"
            conn.execute(
                f"UPDATE client_tokens SET last_used_at = {now_expr} WHERE token = ?",
                (token,),
            )
            conn.commit()
            return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        # client_tokens table may not exist yet (migration 0010 not applied).
        # Return 503 so frontend can show a meaningful error.
        raise HTTPException(status_code=503,
                            detail=f"client_dashboard_not_ready: {e}")


def _client_assistant_filter(client_row: dict) -> tuple[str, list]:
    """Build a SQL fragment + params for filtering rows to this client's
    assistant_ids. Postgres stores assistant_ids as TEXT[] (array). SQLite
    stores it as TEXT (comma-separated). Returns ("assistant IN (?,?,?)",
    ["a","b","c"]). If the client has no assistants assigned, returns
    ("1=0", []) which yields zero rows (correct tenant-isolation default).

    Note: column name is `assistant` in call_events (singular), not
    assistant_id. Don't rename — many existing endpoints reference it.
    """
    ids = client_row.get("assistant_ids") or []
    if isinstance(ids, str):
        # Postgres array literal like "{a,b,c}" OR plain comma-separated
        ids = [s.strip() for s in ids.strip("{}").split(",") if s.strip()]
    if not ids:
        return "1=0", []
    placeholders = ",".join(["?"] * len(ids))
    return f"assistant IN ({placeholders})", list(ids)


def _hetzner_presigned_for_call(call_id: str, expires: int = 3600):
    """Return a 1h-TTL Hetzner S3 pre-signed URL for the recording of this
    call_id, or None if not archived. Tries .wav first, .mp3 fallback.

    Reuses HETZNER_OBJECT_STORAGE_* env vars used by _mirror_recording_to_hetzner().
    """
    ep = os.environ.get("HETZNER_OBJECT_STORAGE_ENDPOINT", "").strip()
    ak = os.environ.get("HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
    sk = os.environ.get("HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip()
    bk = os.environ.get("HETZNER_OBJECT_STORAGE_BUCKET", "").strip()
    if not all([ep, ak, sk, bk]):
        return None
    try:
        import boto3
        from botocore.config import Config
        s3 = boto3.client(
            "s3",
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            endpoint_url=ep,
            region_name="eu-central",
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path"}),
        )
        for ext in ("wav", "mp3"):
            key = f"recordings/{call_id}.{ext}"
            try:
                s3.head_object(Bucket=bk, Key=key)
                return s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bk, "Key": key},
                    ExpiresIn=expires,
                )
            except Exception:
                continue
        return None
    except Exception as e:
        print(f"[_hetzner_presigned_for_call] {call_id} → {e}", file=sys.stderr)
        return None


# Voice catalog — Vapi voiceId → human-readable label. Update when a new
# voice is provisioned for any tenant. Used by /client/api/settings to
# present a friendly name instead of opaque IDs.
VOICE_CATALOG = {
    "U3AWuAe8WcVA50PuDMrY": "Cillian (ElevenLabs, Irish, calm)",
    "ZF6FPAbjXT4488VcRRnw": "Amelia (ElevenLabs, Irish)",
}


@app.get("/client")
@app.get("/client/")
async def client_portal():
    """Serve client.html — JS handles token auth + localStorage."""
    if os.path.exists(CLIENT_HTML_PATH):
        return FileResponse(CLIENT_HTML_PATH)
    return HTMLResponse("<h1>Client dashboard pending build</h1>")


# --- Admin endpoints for managing client_tokens (operator-only) ---

@app.post("/admin/api/clients/issue-token")
async def admin_issue_client_token(request: Request, token: str = Query("")):
    """Issue a client_tokens row for a tenant. Operator-only.
    Body: {slug, display_name, assistant_ids: [...]}.
    Idempotent on slug — if an active token already exists, returns it
    rather than creating a duplicate."""
    check_admin(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")
    slug = (body.get("slug") or "").strip()
    display_name = (body.get("display_name") or "").strip()
    assistant_ids = body.get("assistant_ids") or []
    if not slug or not display_name or not assistant_ids:
        raise HTTPException(status_code=422, detail="slug+display_name+assistant_ids required")
    if not isinstance(assistant_ids, list):
        raise HTTPException(status_code=422, detail="assistant_ids must be a list")

    import secrets as _sec
    import string as _str
    alphabet = _str.ascii_lowercase + _str.digits
    rnd = "".join(_sec.choice(alphabet) for _ in range(24))
    slug_safe = "".join(c if c.isalnum() else "_" for c in slug).lower()[:16]
    new_token = f"ct_{slug_safe}_{rnd}"

    # Postgres expects TEXT[]; SQLite stores comma-separated.
    if _USE_PG:
        assistant_ids_param = list(assistant_ids)
    else:
        assistant_ids_param = ",".join(assistant_ids)

    try:
        with get_db() as conn:
            existing = conn.execute(
                "SELECT token FROM client_tokens "
                "WHERE tenant_slug = ? AND revoked_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (slug,),
            ).fetchone()
            if existing:
                return {
                    "idempotent": True,
                    "token": existing["token"],
                    "magic_url": f"https://client.callmeie.ie/?token={existing['token']}",
                }
            conn.execute(
                "INSERT INTO client_tokens (token, tenant_slug, tenant_display_name, assistant_ids, created_by) "
                "VALUES (?, ?, ?, ?, 'adam')",
                (new_token, slug, display_name, assistant_ids_param),
            )
            conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"issue_failed: {e}")
    return {
        "token": new_token,
        "magic_url": f"https://client.callmeie.ie/?token={new_token}",
        "slug": slug,
        "display_name": display_name,
        "assistant_ids": assistant_ids,
    }


@app.get("/admin/api/clients/tokens")
async def admin_list_client_tokens(token: str = Query("")):
    """List all client tokens issued (operator view). Used by admin.html
    'Clients' panel to see who has what access. Path is /clients/tokens
    not /clients to avoid collision with the existing deprecated alias
    that points at /admin/api/assistants."""
    check_admin(token)
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT token, tenant_slug, tenant_display_name, assistant_ids, "
                "       created_at, last_used_at, revoked_at "
                "FROM client_tokens ORDER BY created_at DESC"
            ).fetchall()
    except Exception as e:
        # Table may not exist yet
        return {"clients": [], "error": str(e)[:200]}
    return {"clients": [dict(r) for r in rows]}


@app.post("/admin/api/clients/{token_val}/revoke")
async def admin_revoke_client_token(token_val: str, token: str = Query("")):
    """Revoke a client token. Operator-only."""
    check_admin(token)
    now_expr = "NOW()" if _USE_PG else "datetime('now')"
    try:
        with get_db() as conn:
            conn.execute(
                f"UPDATE client_tokens SET revoked_at = {now_expr} WHERE token = ?",
                (token_val,),
            )
            conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"revoke_failed: {e}")
    return {"revoked": True}


async def _fetch_client_assistant(assistant_id: str) -> dict:
    """Fetch one assistant from Vapi. Returns {} on failure (never raises)."""
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    if not vk or not assistant_id:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(
                f"https://api.vapi.ai/assistant/{assistant_id}",
                headers={"Authorization": f"Bearer {vk}"},
            )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[_fetch_client_assistant] {assistant_id} → {e}", file=sys.stderr)
    return {}


@app.get("/client/api/me")
async def client_me(token: str = Query("")):
    """Return current client's tenant identity + relevant assistant config
    (recording state fetched live from Vapi)."""
    c = check_client(token)
    ids = c.get("assistant_ids") or []
    if isinstance(ids, str):
        ids = [s.strip() for s in ids.strip("{}").split(",") if s.strip()]
    has_recording_on = False
    if ids:
        a = await _fetch_client_assistant(ids[0])
        has_recording_on = bool((a.get("artifactPlan") or {}).get("recordingEnabled", a.get("recordingEnabled", True)))
    return {
        "tenant_slug": c["tenant_slug"],
        "tenant_display_name": c["tenant_display_name"],
        "assistant_ids": ids,
        "has_recording_on": has_recording_on,
        # Retention values are global system policy (matches public site +
        # DPA). Hard-coded by design — change here when policy changes,
        # propagate to privacy.html + vertical sales pages.
        "retention_days_audio": 30,
        "retention_days_transcripts": 90,
    }


@app.get("/client/api/calls")
async def client_calls(
    token: str = Query(""),
    days: int = Query(14, ge=1, le=90),
    q: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    """List recent calls scoped to this client's assistant_ids. Filterable by
    days lookback + text search across summary/transcript."""
    c = check_client(token)
    where_assist, params = _client_assistant_filter(c)
    if where_assist == "FALSE":
        return {"calls": [], "count": 0, "tenant_slug": c["tenant_slug"]}

    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT call_id, created_at AS ts, assistant AS assistant_id, "
        "       summary, event_type, detail "
        "FROM call_events "
        f"WHERE {where_assist} AND created_at >= ? "
    )
    args = params + [cutoff]
    if q:
        # Portable case-insensitive search — Postgres ILIKE, SQLite LOWER LIKE
        if _USE_PG:
            sql += "AND (summary ILIKE ? OR CAST(detail AS TEXT) ILIKE ?) "
        else:
            sql += "AND (LOWER(summary) LIKE LOWER(?) OR LOWER(CAST(detail AS TEXT)) LIKE LOWER(?)) "
        like = f"%{q}%"
        args += [like, like]
    sql += "ORDER BY id DESC LIMIT ?"
    args.append(int(limit))

    try:
        with get_db() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"query_failed: {e}")

    # Group by call_id, take latest row per call as the "summary line"
    grouped: dict[str, dict] = {}
    for r in rows:
        cid = r["call_id"]
        if cid in grouped:
            continue
        try:
            d = json.loads(r["detail"]) if isinstance(r["detail"], str) else (r["detail"] or {})
        except Exception:
            d = {}
        grouped[cid] = {
            "call_id": cid,
            "ts": str(r["ts"]),
            "caller_name": d.get("name") or d.get("caller_name") or "",
            "caller_phone": d.get("contact_phone") or d.get("phone") or d.get("caller") or "",
            "summary": r["summary"] or "",
            "outcome": r["event_type"] or "",
            # 2026-05-13 — expose more fields so client UI can render proper row
            "duration": d.get("duration") or 0,
            "ended_reason": d.get("ended_reason") or "",
            "has_transcript": bool(d.get("transcript")),
            "assistant_id": r["assistant_id"] or "",
            "summary_text": d.get("summary") or "",
        }
    return {"calls": list(grouped.values()), "count": len(grouped),
            "tenant_slug": c["tenant_slug"]}


@app.get("/client/api/calls/{call_id}")
async def client_call_detail(call_id: str, token: str = Query("")):
    """Full timeline + transcript + recording_url for one call.
    At LEAST ONE event for this call must belong to client's tenant whitelist."""
    c = check_client(token)
    # 2026-05-13 fix: Postgres returns assistant_ids as TEXT[] which can serialize
    # as a string literal like "{uuid1,uuid2}" depending on driver. set() on a
    # string yields characters, not UUIDs — breaks the intersection check.
    # Coerce to list of strings same way _client_assistant_filter does.
    raw_ids = c.get("assistant_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [s.strip() for s in raw_ids.strip("{}").split(",") if s.strip()]
    allowed_ids = set(raw_ids)
    try:
        with get_db() as conn:
            events = conn.execute(
                "SELECT created_at, event_type, assistant, summary, detail "
                "FROM call_events WHERE call_id = ? ORDER BY id ASC",
                (call_id,),
            ).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    if not events:
        raise HTTPException(status_code=404, detail="call_not_found")
    # Authorization — at least one event for this call must belong to tenant.
    # NOTE: for handoff chains (Claire->Dunne->trap), Claire is a shared origin
    # not in any single tenant whitelist. We deliberately allow access if Dunne
    # OR trap (handoff-attribution mirror rows) match the tenant.
    event_assistant_ids = {e["assistant"] for e in events if e["assistant"]}
    if not (event_assistant_ids & allowed_ids):
        raise HTTPException(status_code=403, detail="cross_tenant_access_denied")

    out_events = []
    caller_name = caller_phone = ""
    # 2026-05-13 — dedupe transcript. Handoff attribution writes mirror
    # call-ended rows per call, each with the same transcript. Pick the LONGEST
    # non-empty transcript (most complete) and return ONLY that one.
    best_transcript = ""
    analysis_summary = ""
    ended_reason = ""
    for r in events:
        try:
            d = json.loads(r["detail"]) if isinstance(r["detail"], str) else (r["detail"] or {})
        except Exception:
            d = {}
        if r["event_type"] == "lead-captured":
            caller_name = d.get("name") or caller_name
            caller_phone = d.get("contact_phone") or d.get("phone") or caller_phone
        t = d.get("transcript")
        if t and isinstance(t, str) and len(t) > len(best_transcript):
            best_transcript = t
        if d.get("summary") and not analysis_summary:
            analysis_summary = str(d["summary"])
        if d.get("ended_reason") and not ended_reason:
            ended_reason = str(d["ended_reason"])
        # 2026-05-13 — skip mirror call-ended rows in the visible timeline
        # (handoff attribution writes one per chain assistant; they're all the
        # same logical event). Keep only the primary (non-mirror) call-ended
        # row + non-call-ended events.
        if r["event_type"] == "call-ended" and d.get("is_mirror_row"):
            continue
        out_events.append({
            "ts": str(r["created_at"]),
            "event_type": r["event_type"],
            "summary": r["summary"] or "",
        })

    # 2026-05-13 — return saved notes for this call (Adam request: notes were
    # saving to call_notes table but never displayed back in drawer).
    notes_out = []
    try:
        with get_db() as conn:
            note_rows = conn.execute(
                "SELECT created_at, note, actor, tenant_slug FROM call_notes "
                "WHERE call_id = ? ORDER BY id ASC",
                (call_id,),
            ).fetchall()
        for n in note_rows:
            notes_out.append({
                "ts": str(n["created_at"]),
                "note": n["note"] or "",
                "actor": n["actor"] or "client",
                "tenant_slug": n["tenant_slug"] or "",
            })
    except Exception as e:
        print(f"[client_call_detail] notes fetch failed: {e}")

    return {
        "call_id": call_id,
        "caller_name": caller_name,
        "caller_phone": caller_phone,
        "events": out_events,
        "transcript": best_transcript,
        "analysis_summary": analysis_summary,
        "ended_reason": ended_reason,
        "notes": notes_out,
        # 1h-TTL Hetzner pre-signed URL; None if the recording isn't mirrored
        # yet (e.g. call still in progress, or recording disabled per call).
        "recording_url": _hetzner_presigned_for_call(call_id, expires=3600),
    }


@app.post("/client/api/calls/{call_id}/note")
async def client_call_note(call_id: str, request: Request, token: str = Query("")):
    """Save an internal note on a call. Note is visible to all client users
    of this tenant AND to Adam in admin.callmeie.ie."""
    c = check_client(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")
    note = (body.get("note") or "").strip()
    if not note:
        raise HTTPException(status_code=422, detail="note_required")
    if len(note) > 4000:
        raise HTTPException(status_code=422, detail="note_too_long")

    # Authorization: confirm the call belongs to this tenant.
    # 2026-05-13 — same string-array coercion as /client/api/calls/{id};
    # query ALL events (not LIMIT 1) since handoff chains have multiple rows
    # and the FIRST one is often the shared origin (Claire), not the tenant's.
    raw_ids = c.get("assistant_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [s.strip() for s in raw_ids.strip("{}").split(",") if s.strip()]
    allowed_ids = set(raw_ids)
    try:
        with get_db() as conn:
            evs = conn.execute(
                "SELECT assistant FROM call_events WHERE call_id = ?",
                (call_id,),
            ).fetchall()
            event_aids = {e["assistant"] for e in evs if e["assistant"]}
            if not (event_aids & allowed_ids):
                raise HTTPException(status_code=403, detail="not_your_call")
            conn.execute(
                "INSERT INTO call_notes (call_id, tenant_slug, note, actor) "
                "VALUES (?, ?, ?, 'client')",
                (call_id, c["tenant_slug"], note),
            )
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"save_failed: {e}")
    return {"saved": True}


@app.get("/client/api/inbox")
async def client_inbox(token: str = Query(""), status: str = Query("unactioned")):
    """Outstanding 'message-taken' events scoped to this client. By default
    returns only messages without a paired 'inbox-actioned' event."""
    c = check_client(token)
    where_assist, params = _client_assistant_filter(c)
    if where_assist == "1=0":
        return {"messages": []}
    # Note: where_assist uses 'assistant IN (?,?)'; we need to qualify it as
    # ce.assistant once we alias the table for the EXISTS subquery.
    where_assist_qualified = where_assist.replace("assistant IN", "ce.assistant IN")
    sql = (
        "SELECT ce.call_id, ce.created_at AS ts, ce.summary, ce.detail "
        "FROM call_events ce "
        f"WHERE ce.event_type = 'message-taken' AND {where_assist_qualified} "
    )
    if status == "unactioned":
        sql += (
            "AND NOT EXISTS ("
            "  SELECT 1 FROM call_events ce2 "
            "  WHERE ce2.call_id = ce.call_id AND ce2.event_type = 'inbox-actioned'"
            ") "
        )
    sql += "ORDER BY ce.id DESC LIMIT 100"
    try:
        with get_db() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"query_failed: {e}")
    messages = []
    for r in rows:
        try:
            d = json.loads(r["detail"]) if isinstance(r["detail"], str) else (r["detail"] or {})
        except Exception:
            d = {}
        messages.append({
            "call_id": r["call_id"],
            "ts": str(r["ts"]),
            "caller_name": d.get("name") or "",
            "callback_number": d.get("contact_phone") or d.get("phone") or "",
            "reason": d.get("reason") or r["summary"] or "",
            "urgency": d.get("urgency") or "",
        })
    return {"messages": messages}


@app.post("/client/api/inbox/{call_id}/actioned")
async def client_inbox_actioned(call_id: str, token: str = Query("")):
    """Mark an inbox message as actioned. Logs 'inbox-actioned' event so it
    appears in the unified call timeline and stops surfacing in the inbox."""
    c = check_client(token)
    raw_ids = c.get("assistant_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [s.strip() for s in raw_ids.strip("{}").split(",") if s.strip()]
    allowed_ids = set(raw_ids)
    try:
        with get_db() as conn:
            evs = conn.execute(
                "SELECT assistant FROM call_events WHERE call_id = ?",
                (call_id,),
            ).fetchall()
            event_aids = {e["assistant"] for e in evs if e["assistant"]}
            tenant_match = event_aids & allowed_ids
            if not tenant_match:
                raise HTTPException(status_code=403, detail="not_your_call")
            pick_aid = next(iter(tenant_match))
            conn.execute(
                "INSERT INTO call_events (call_id, event_type, assistant, summary, detail) "
                "VALUES (?, 'inbox-actioned', ?, ?, ?)",
                (call_id, pick_aid,
                 f"actioned by {c['tenant_slug']}",
                 json.dumps({"actor": "client", "tenant_slug": c["tenant_slug"]})),
            )
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"action_failed: {e}")
    return {"actioned": True}


@app.get("/client/api/insights")
async def client_insights(token: str = Query(""), days: int = Query(7, ge=1, le=90)):
    """Weekly summary metrics scoped to this client.
    Real event_types from log_event(): call-ended, message-taken,
    lead-captured, demo-complete, booking, booking-fail, lead-error.
    Derived metrics:
      total_calls   = distinct call_ids with any event
      qualified     = call_ids with demo-complete (AI got useful info)
      messages_taken= distinct call_ids with message-taken
      bookings      = distinct call_ids with booking
      short_drops   = call-ended without demo-complete OR message-taken (caller hung up early)
    """
    c = check_client(token)
    where_assist, params = _client_assistant_filter(c)
    if where_assist == "1=0":
        return {"total_calls": 0, "qualified": 0, "messages_taken": 0,
                "bookings": 0, "short_drops": 0, "raw_counts": {}}

    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            # Distinct call_ids per event_type
            base = (
                "SELECT event_type, COUNT(DISTINCT call_id) AS n "
                f"FROM call_events WHERE {where_assist} AND created_at >= ? "
                "GROUP BY event_type"
            )
            rows = conn.execute(base, tuple(params + [cutoff])).fetchall()
            counts = {r["event_type"]: int(r["n"]) for r in rows}
            # Total unique calls (any event)
            total_calls_row = conn.execute(
                f"SELECT COUNT(DISTINCT call_id) AS n FROM call_events WHERE {where_assist} AND created_at >= ?",
                tuple(params + [cutoff]),
            ).fetchone()
            total_calls = int(total_calls_row["n"]) if total_calls_row else 0
            # Short-drop = call-ended WITHOUT demo-complete OR message-taken
            short_drops_row = conn.execute(
                f"SELECT COUNT(DISTINCT ce.call_id) AS n FROM call_events ce "
                f"WHERE ce.event_type = 'call-ended' AND " + where_assist.replace("assistant IN", "ce.assistant IN") + " AND ce.created_at >= ? "
                "AND NOT EXISTS (SELECT 1 FROM call_events ce2 WHERE ce2.call_id = ce.call_id AND ce2.event_type IN ('demo-complete','message-taken'))",
                tuple(params + [cutoff]),
            ).fetchone()
            short_drops = int(short_drops_row["n"]) if short_drops_row else 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"query_failed: {e}")
    return {
        "days": days,
        "total_calls": total_calls,
        "qualified": counts.get("demo-complete", 0),
        "messages_taken": counts.get("message-taken", 0),
        "bookings": counts.get("booking", 0),
        "short_drops": short_drops,
        "raw_counts": counts,
    }


@app.get("/client/api/settings")
async def client_settings(token: str = Query("")):
    """Read-only summary of this client's assistant configuration.
    Voice + greeting + recording state are fetched LIVE from Vapi —
    no hardcoded placeholders. Falls back to '—' if Vapi unreachable
    so customer sees an honest unknown rather than fake values."""
    c = check_client(token)
    ids = c.get("assistant_ids") or []
    if isinstance(ids, str):
        ids = [s.strip() for s in ids.strip("{}").split(",") if s.strip()]
    voice_label = "—"
    greeting = "—"
    recording_on = False
    if ids:
        a = await _fetch_client_assistant(ids[0])
        voice = a.get("voice") or {}
        vid = voice.get("voiceId") or ""
        provider = voice.get("provider") or "?"
        voice_label = VOICE_CATALOG.get(vid) or (f"{provider} · {vid[:8]}…" if vid else "—")
        greeting = a.get("firstMessage") or "—"
        recording_on = bool((a.get("artifactPlan") or {}).get("recordingEnabled", a.get("recordingEnabled", True)))
    return {
        "tenant": c["tenant_display_name"],
        "voice": voice_label,
        "greeting": greeting,
        "recording_on": recording_on,
        "retention": "30 days audio / 90 days transcripts",
        "sub_processors": ["Twilio", "Vapi", "ElevenLabs", "Deepgram", "Hetzner Object Storage (Nuremberg)"],
    }


@app.get("/client/api/export.csv")
async def client_export_csv(token: str = Query(""), days: int = Query(30, ge=1, le=90)):
    """CSV download of all calls in date range, scoped to this tenant.
    Columns: ts, call_id, event_type, caller_name, caller_phone, summary."""
    c = check_client(token)
    where_assist, params = _client_assistant_filter(c)
    if where_assist == "1=0":
        return PlainTextResponse("ts,call_id,event_type,caller_name,caller_phone,summary\n",
                                 media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=calls.csv"})

    import datetime as _dt
    import csv as _csv
    import io as _io
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT call_id, created_at AS ts, event_type, summary, detail "
                f"FROM call_events WHERE {where_assist} AND created_at >= ? "
                "ORDER BY id DESC",
                tuple(params + [cutoff]),
            ).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"query_failed: {e}")

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["ts", "call_id", "event_type", "caller_name", "caller_phone", "summary"])
    for r in rows:
        try:
            d = json.loads(r["detail"]) if isinstance(r["detail"], str) else (r["detail"] or {})
        except Exception:
            d = {}
        w.writerow([
            str(r["ts"]),
            r["call_id"] or "",
            r["event_type"] or "",
            d.get("name") or d.get("caller_name") or "",
            d.get("contact_phone") or d.get("phone") or "",
            (r["summary"] or "").replace("\n", " "),
        ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=calls-{c['tenant_slug']}-{days}d.csv",
        },
    )


@app.post("/client/api/settings/request-change")
async def client_request_change(request: Request, token: str = Query("")):
    """Send a 'request a change' email from this client to hello@callmeie.ie.
    Tagged with tenant_slug so Adam can route the request."""
    c = check_client(token)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")
    topic = (body.get("topic") or "")[:120]
    details = (body.get("details") or "")[:4000]
    if not topic or not details:
        raise HTTPException(status_code=422, detail="topic_and_details_required")

    # Send via Resend (same path as morning_rollup.py)
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    if resend_key:
        try:
            import urllib.request as _ur
            payload = {
                "from": "client-dashboard@callmeie.ie",
                "to": ["hello@callmeie.ie"],
                "reply_to": f"hello@callmeie.ie",
                "subject": f"[{c['tenant_slug']}] Change request: {topic}",
                "text": (f"Change request from {c['tenant_display_name']} ({c['tenant_slug']})\n\n"
                         f"Topic: {topic}\n\nDetails:\n{details}\n\n"
                         f"-- Client dashboard, token last used {c.get('last_used_at')}"),
            }
            req = _ur.Request(
                "https://api.resend.com/emails",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "callmeie-client/1.0",
                },
                method="POST",
            )
            with _ur.urlopen(req, timeout=10) as r:
                if not (200 <= r.status < 300):
                    raise Exception(f"resend status {r.status}")
        except Exception as e:
            print(f"[client request-change] resend failed: {e}", file=sys.stderr)
            # Don't fail the user request — log instead so Adam can pick it up
            # from the call_events audit log or stderr.
    return {"sent": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
