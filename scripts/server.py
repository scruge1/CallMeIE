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

    # P2 — mirror Vapi recording to durable Hetzner archive (90d retention promise).
    # Vapi includes recordingUrl + stereoRecordingUrl on the call object once
    # the recording is available; schedule a background task so the webhook
    # returns 200 immediately. Skip if no recording (e.g. silent failed call).
    artifact = call.get("artifact") or {}
    recording_url = artifact.get("recordingUrl") or call.get("recordingUrl") or ""
    stereo_url = artifact.get("stereoRecordingUrl") or call.get("stereoRecordingUrl") or ""
    if call_id and (recording_url or stereo_url):
        background_tasks.add_task(
            _mirror_recording_to_hetzner,
            call_id, assistant_id, recording_url, stereo_url,
        )

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

_VALID_STATUS = {"new", "contacted", "qualified", "closed_won", "closed_lost", "spam"}
_STATUS_TRANSITIONS = {
    "new":         {"contacted", "qualified", "closed_lost", "spam"},
    "contacted":   {"qualified", "closed_lost", "closed_won"},
    "qualified":   {"closed_won", "closed_lost"},
    # Terminal states require explicit reopen — handled by 'reopen' kw not here.
    "closed_won":  set(),
    "closed_lost": set(),
    "spam":        set(),
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
    """Owner-only: mint Stripe Checkout Session + SMS link to caller mid-demo.

    Body:
      phone: str             E.164 mobile (e.g. +353871234567)
      tier:  str             "professional" | "growth"
      include_setup: bool    default True (bundle one-off €297 setup fee)

    Auth: ?token=<OWL_OWNER_TOKEN> OR Authorization: Bearer <OWL_OWNER_TOKEN>

    Returns: { ok, checkout_url, session_id, sms_status, sms_ok, phone, tier }
    """
    if not _owl_check_owner(token):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(None, 1)[1].strip()
        if not _owl_check_owner(token):
            raise HTTPException(status_code=401, detail="owner token required")

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
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT detail, created_at FROM call_events "
                "WHERE event_type = 'demo-complete' "
                "AND created_at >= datetime('now', '-7 days') "
                "ORDER BY id DESC"
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    events = []
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except Exception:
            d = {}
        events.append({
            "ts": r["created_at"],
            "event_type": r["event_type"],
            "assistant": r["assistant"],
            "summary": r["summary"],
            "detail": d,
        })
    return JSONResponse({"call_id": call_id, "events": events, "count": len(events)})


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
    return JSONResponse({"leads": out[:limit], "count": len(out[:limit]), "total_seen": len(out)})


@app.get("/admin/api/heat-by-assistant")
async def admin_heat_by_assistant(token: str = Query("")):
    """Gap 18 lite — heat distribution per assistant (proxy for failure clustering).

    Sentry-style grouping by 'assistant' as fingerprint dimension; surfaces
    which assistant prompt is converting vs which is browsing-only."""
    check_admin(token)
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT assistant, detail FROM call_events "
                "WHERE event_type = 'demo-complete' "
                "AND created_at >= datetime('now', '-30 days')"
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
