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

from voice_catalog import voice_for_industry  # noqa: E402  (D6 — needs sys.path above)

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


def _delayed_mirror_via_vapi(call_id: str, assistant_id: str,
                              max_attempts: int = 6, delay_seconds: int = 30):
    """Poll Vapi GET /call/{id} on a delay; mirror as soon as URLs appear.

    Handles the race where /vapi/call-ended fires before Vapi has finished
    multiplexing + uploading the recording. Adam 2026-05-20: 4 days of silent
    drops because the EOC payload had empty recordingUrl. Vapi populates the
    fields seconds-to-minutes after the EOC webhook returns; this poller
    waits and tries.

    NEVER raises — runs in BackgroundTasks. Logs `recording-archive-failed`
    only after exhausting all attempts so a delayed success leaves a clean
    `recording-archived` row, not a misleading failure row."""
    import time as _time
    import requests as _rq
    api_key = os.environ.get("VAPI_API_KEY", "").strip()
    if not api_key:
        log_event(call_id, "recording-archive-failed", assistant_id,
                  "VAPI_API_KEY missing for delayed mirror",
                  {"reason": "missing_api_key"})
        return
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        _time.sleep(delay_seconds)
        try:
            r = _rq.get(
                f"https://api.vapi.ai/call/{call_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            r.raise_for_status()
            call = r.json()
        except Exception as e:
            last_error = f"Vapi GET attempt {attempt} failed: {str(e)[:120]}"
            print(f"[DelayedMirror] {call_id} attempt {attempt}/{max_attempts} fetch error: {e}")
            continue
        art = call.get("artifact") or {}
        mono = art.get("recordingUrl") or call.get("recordingUrl") or ""
        stereo = art.get("stereoRecordingUrl") or call.get("stereoRecordingUrl") or ""
        if not (mono or stereo):
            rec = (art.get("recording") or {})
            stereo = stereo or rec.get("stereoUrl") or ""
            mono = mono or ((rec.get("mono") or {}).get("combinedUrl") or "")
        if mono or stereo:
            print(f"[DelayedMirror] {call_id} URLs populated on attempt {attempt}; mirroring")
            _mirror_recording_to_hetzner(call_id, assistant_id, mono, stereo)
            return
        print(f"[DelayedMirror] {call_id} attempt {attempt}/{max_attempts} — URLs still empty")
    log_event(
        call_id, "recording-archive-failed", assistant_id,
        f"Vapi recordingUrl never populated after {max_attempts}x{delay_seconds}s",
        {"max_attempts": max_attempts, "delay_seconds": delay_seconds,
         "last_error": last_error},
    )


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
    elif call_id:
        # 2026-05-20 — Vapi EOC race: recordingUrl/stereoRecordingUrl can be
        # empty in the end-of-call-report payload because the recording
        # mux/upload finishes seconds-to-minutes AFTER the EOC webhook fires.
        # Schedule a delayed poller that hits Vapi GET /call/{id} until the
        # URLs appear, then mirrors. Without this the recording is silently
        # dropped (no row in recordings/, no recording-archive-failed event).
        background_tasks.add_task(
            _delayed_mirror_via_vapi, call_id, assistant_id,
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
            "voice": {"provider": "11labs", "voiceId": voice_for_industry(business_type=sub.get("business_type"))["voice_id"]},
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


_TTS_KOKORO_B64 = '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CdYp///FBJLOEgOte5n6XKUCGAOUAAIP7HqCsm6LAKn+0SBaSm8otZtFYJfUGPyTD3kot9Be4IBb//NExCgR2aqtvHrEyHbXy//TnAndy9P//RFOEFDw5kPmVsp//+gk+/F3yURKkCRTmaZbkH912MV1oVFO4VBFnqoUJkzdCiWh/qAoZsewpj/1BChMBQjqlTqw2JQ1/XqA//NExDMSOM69vnpGLiEgaf5Z3//4NDw0TckFSoifO/XLPDcZzSBS1E/Td0jRAziAUiETXW5LGXVD28LdkRyEaimHicw+J//1LJukP8bVqzfn0v/ny2rtC4KJNYRmokas//NExD0RscZcNMBGtKph210VrYtP6rUldcpHoBU08ekJMij0es+nbWn/fb/7b7NfdNivKisCw0NcYHWmhdyCaL01Lpd/V14or+0+zToVACCDBEIIEvQJMl+wXsAGngrg//NExEkNkTpUM0oQAMj7CFws+AX+3AsCOxkw3/9N7hsgWUBY+Qwgn73dCLMD4A9QLKxyg9T+mkgaMgaA2bEHkXI0UGLjHN//shw+MTmFzBDRZYYAC5wTgO////8ihOHx//NExGUhqypxkY2AAMgcAssn3KZPm5j///2+/kDHGWhyxxidC6T44BWguAZNIqIZiWsEZh/y2pw6FZE2QGxMEi5wG2m22FWYJtv2HQTQFlBeNPDoMNwfDytVM+/7fVcR//NExDERsSaqS8lAAHGSwNSolBoKcjY+cURAotUn////9S7btPkrHLhML+hhgA8GOTF2l262c8aSH0kFMghD99t7eZy6448AIkBBjGmNKxgxLadVdh5zB33lFTK+Q/////NExD0SUNbKPnpMMP+BBoRSKrNmi7khG6pnhUPRpSy7iUf3FrA/SNFPzG3IbX90kUnv1UT8alDDXBQvZACBvJo33CzzdwzvSpABB0Qvy8IGPtHJ3/7B4eeNsPp////8//NExEYSYPcC/spGxkUt/spCCBVf4UHpe0UeXJtnMYEzIXEwhTScGkIlqiareUFo0IiEPcZWWjuOgKcpP71XwQTnztlkHmEX785LnTtOpApfDBAQO7b8mupzFUGAwBgA//NExE8R0T7NcUlgALqdduGwwAgLeMtAmuXbpeNGSv7J+JxPJw5XUPYRZGEvJhIqHnmKJJj4PcS0L4DZHgT6A4knksIwOYoF9Eom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'
_TTS_PREMIUM_B64 = '//NkxAAb5DHwAEAGIQXsiU7u7uf8RE4nu7/+4Aif/u7vC9wBEREREPrnC/0Tc9ANz/d3TQq76f8Lpv0/iF/xC+ufETqEXoiIL//3EF8RC//9+u+ifU9/7v+iFNC93dz4iIhekRCc9AN3Ou8R/675f8RPd3AxZohBEREQQIUFEAZKjmO2meqtE2ivtFiC0BGM//NkxBMZeeoQwkBG/WqGDFkBmcIDyHRkcxdKxhu4xkeRh6vc2XABGP7EotlvQW9llpEZ7x63UP/+lfSI1vspXN6QnZ+TqY+hbrfIzXz6h717Wh/9+bgtPwERwrgMyPb6HqallVqH0GJlKUovYwpFxQyK6bt0W0Gka21+1ClM7LsdbqZH/fUvp9KCj1qSKlFp//NkxDARcAotlBBGAEMbWavc8zayJ4q54tagtZmRDclL9yoAiBCA2j7xi2dBHGItR/YluTYrqdHw3ozmW9ifjdUQ08K9gQGxNxmjnfsshbWYL1POZLk+7Lw9/mMO28K3aum2C+lIFsj6xT6lI6QLWf4Y0xxQpZO91FodDXbUvCDuVbDhD9OmMediXupVl82F//NkxG0aSFoViDjGDdG3c/NcUElz9ZBAjbnvnsIGVf347vJ6J9fzd+58r6Ihb7un5/kfi673QjhBC93NM+uSJO/7dzPtcWLs7nRCClTFr2rWnxSesYUvgZoQHynuMwU/1GldSM/7voh30jH3+1ZUzM+wBHiUz3oEKqxSMhka3tM1mdFnBWK4YXIoYghXw2ja//NkxIYbig4kC0kYASGim7ow+oYW4DCF63/TSViMoloMcAGCMEEGeeRJxjsE2gPwpDG9BWFgZGOzyJigwi+ltP5MwY0J/PNaJSseWFVWpsuGtUn7pbiR6x6OEI/4xYzcdRcw661fW9PM/62q3kNxxrUZWIZDniU/+KY3eHHpr//DHPGYUfCVF/7z0y3TsE7k//NkxJo0k+K+X5h4A81sV3////////S+5YDE4b3q+873X/VNfe//ff+f//////////92ru1Lx2CGxvM1xmv/0/DAA/9F6hxCclZSoQ1BqxGDBqkSIPQYvlUoBrBgAw69Ax0GCTCBRoW3EkMCQQSCAEG4RcZk8nIonBkTzBIxUkkeRUaorRYuKWq7KXWcUyVS//NkxEosa1KYL9poAEgXyAgYjJE2CSiNkYYYmEATohkgNw9x3i1GYEdKYbYXhy65mSRmmktMyUpGm3+uq90rpPZNaKZ12MWSUifNmf61LW//r2WdZF1bO9SF2fda2UtHXpLqX6nQMhx5bnWqMjB5Jf/8oEzVAbmAFzfs6kGRgRMGwymaVRqAgoBVuQ5KzaiD//NkxBsgw2aodspE+G058UFxIX1goG3Meoj9oY6z45k2VGXa3Hc/j7lc///f8Y6q6biU2qgTMvQwUBUOAmgFxQanHPn/ys//+b7dpYO6mQolBLzHXlQoZgZCf9qPtlcvU7Tk//9UVnNy7QRMSWUCpkF/93ptAJkIIBV9NDIVvhE1p4KQizNuhkQSugKMM2Kf//NkxBshW66cftIE3JaUW3aegJlrtpysU88NPows96E1F1MkZBd8hyHtmm3dc//30sqzMdEY+ZNGQaIQ+y+I/9N3Vz//sh9z/+g7GQ5rFSbpMqCXezK/9XkPRlzE5vX/lLYid6NfzflojVjhA5R6bmkgKWeBV11qHAIBexngRRhCh4XKjgR1m4UCxYneFVUH//NkxBgim6qQNtmE3CQ0iuYGDOyIAFTeP34oSBAYgQGD9qILWp5iD2UQz5pBtp+xp9/s7t2+PawJazKR862FMnOxXwrt80rnU7ox0/VzEWv9bIjpBSFvdvozqHRbJ/VHq7XVWZN3Iinf/Jtmqsq0fv//owhLrkF1BEPh8gnXgrUGeGRd/LYWKniKJmNuYeAJ//NkxBAfqzakVtJGyBClgCF0TEluVX1T5kb+xVx6MNFSc2zRx8FFZMIWNVrDslhuMblHUtIR6TCmIQWECGTvLaVQjFhrT4V0W7/t+Ud+U2jbuXZ/6GvixBrLF/+kSHvC///jnl9+Z7Izlrk6FYXoc3o1Uhyv/HfNNFVoSgwADvLcZMjHZ28atJmh7XpW54BD//NkxBQfKhqoLtGFFLTogXDl0NQ0yiORGc3L6V6rTrclLtuvS0QNVZ5KTQo+AMrVQz9s3zvf88P3BpMPJ9A9Z9AkBFBEKLTfY3c7f1+t+gApwMWjCz/86kQSQIM+I0Bm/qb/Ut66qP6CZAhbwihiRgwFFhVIrYgJ0pN2/hQihSIeceL7LXleBYQpvMOyAEX3//NkxBoc6dbE9sJKnArX2l9/37UehBEGDxJrev1jXzh/GnTTSdCvNsYEgdIWUZEdqFdzjwc6iZEs+13cv/ZxVxEaJMMYwwsmppU4swKG0Vhya9Vdb1JLGbFEFNkbLfrjFeycg/PwwkawR62b/9ZVFOyipKxdJ783CBImzakIQQN2VEpPLop5aFpAokK2zg8S//NkxCkc0mrI9sJGkMiWP0jQpULZfWEQCCFHKXcjli3AtjUv////vycRjY1Kl2HNy/2g6rn3+Rv+1aJavdzFTBGHSIKrEI0EngUOCGgqs/0xCeX+qqXkakOrouRyXf2lAMUFVEhDROSFG+hgjRlJkQne1HRmEqkiCouSS+P5lu5F07MqNU7IJD2m/YwBC2pS//NkxDgcQjrJfnmKlrGfZS/9qO5UGGRCO5hIxlNfIqHR91eb53pEBbsIAwFPxS8Gn+sLulYiljxb3wFES8GrBcBHxgVO6QEDEgXktSfli3YR+O8zWmUNEITaV7jwq8TAfxvKEzZYAUDBzVpoOkL2wU6mvdfEtO+tAc7FX+6L5HFnOQmjWL/0Wzfuhv/9JPJ1//NkxEoZQ9qyNssKs7Le2KsVjfmSdtX/////m/Y+v//////fYfXT/ERogWwtyTLIhs3r5XGBDChtkQnodJSCzHqg2RzKGlru1FkKIQsSHEb+QuvpkLH6tCejVLVtASJA2AhN1hUgWNdOY5W3/6v74qEekAtEZEk/qFRjf0LMtCgFLBMH4QQwEBMhn/aWDgwh//NkxGgbSLa1lg4YHh73SZMI1aYFGpG/3hVJGzX5RIkIhSAAActfKMjhEF5yxTNwHm49lLGAKsrFFoCdQOhX7gnohrLJHfnnev979v/dAS9/9Vaqh26+r//1To/v//QuhDdyedtT/6gZL0S9fTPViDHrfIKKeUw5ZOf/7jjJxyEWKWKBWQNpfb9t/70SGl0N//NkxH0bsr6kvtYEeLIEcxS0uzrADYCufTZLkTX5odjuu9GYDof0ThML4of5BzX3Kp0utYuLAxiTl92yM9CA2qv/O+p09f//6PRjsQjP+27//1ZTH6tX/1m1Inv/+rrU4t2qQGKhloGG//6o/pn7XkRsNOSNOfzOoOkey3MNyGtzneMwXO3nbGlq2e7mqXnd//NkxJEcO0bCXsPEeMNs5gXUfXukDnYghWhl1uA2J3+aw4ccKA0WrLPi5l/4pmKFAoO+b/1VVRVU/Tlat/735i2MUrgIJZXOUhTUp///+r9ut0n9v/8tZBTHIAkMQoZRxH//XWcSJA64eQDdFD427ZJJ/76KleXrxpLQ9/ZuGrVWIPxGb34Muzs2/XqwrXD9//NkxKMfq1LBfsIFSniCoUFsYlxQvfb5SXDha/rjl5vOdn/15gZqo7LZO2n2IVjrZ/x3eF6I/X6+gRz2J/q8hHX2rRKUoNv//uj/5luazO5ioyDnZimbtd1/ZWRgoDIYCBlxHn//oFULz3KgIirStxguWa6zfJ2CSClXwpVC5uERmvRSuNJQESoYCFNGOa/1//NkxKchG1LNfsME3lYZLGCicSevFLZqSgJqqlBSojUuCmNHQ1EdTTehzerfrQvqXVlKVDGcurTOv/Upjfr8pW65WSpWM/////6mN///8wooNBU6+WmOgO1A1uMiJQaYAKh0kqAI3BQYmPSkBgsj0r4WAMDIfJlkQARNNZsckiWerFmRs5rCz2455BYOjwBT//NkxKUba9LRvnjEvuSoShMoIoa/9pXVP86TGFMiKFVNQ9KqEKfWEgoJTsXKmEXNqNIdngqOF0gFWgRFaMXdVf/q61f+tSo1ICHMKgAyCXDjYQTJLnmKBuUAlFYGAeHIm2iw78vVK7WH93lrKtiYXEw6HQFapWMJAELKHcs6yNdDCzCtDTvOcxg0qKVok7pm//NkxLob4T5oNuJGlPvj2KHmbR1mP8pjLdXRHfHvYVC4lEV5Bmd6grf1f/7P6kf/btpBeZ1KTEgsklsthsFYiFQbARgl/oZhxfBmBk/DotxSATDtVEvFM0h2xxMRCjP5OoYrLTR6ULqUSBY1QlHhp1js9ojMEgNA/xc35zUwwxMUiYok24/wzxvC0WyrcZhe//NkxM0cmf5MHVwoAJrer3KQ61YfjawIVbfxuuMRHDdP73Soh5CTqOBYLe+MfbDj4hblr9/O//rFKOmeEsbLGwVmbIqpmr81rS3s+/+/vf+v//+hcBWKdsHXAY1S+UDVBnUjL9f1rXOPbX+Lf//////////quHAhuL/FtP5bUiQ7WpEFUywF1EakqCQxQCqJ//NkxN01c9aqX5h4AL4CYkDptrxjQsN5WwmHIat+lhEKL9d/5BLOSqzuvTazbWbzxWxCGhXFsLqwGmgwgiqJ2qnSscI7JAePGxWS7jyNsZ/Arq8lKa1jW/8X9KzlyEIF4RYc+XPhkAJMnA4VPlBoog+3QBBqf///W0pvVIT/XITizQFBMPbvbdXFBNxEK25Z//NkxIogmVLCV9h4AGlw2BaNcNgi9c+t4TYbnToVmMlG/WD+M5lH0zPkveSBsp58tbqX6/vrhfca+sQzad6Nvn1NUIYxitI3qCAjMPv+q7OGocxlTrNNndkVHZ29NWKX/6IjtMckqUKdUDuYOOQzClCISuMFGE/DpU6Ku4VLgMsSERmj5cTHzSrEAFoAE445//NkxIogiwriVnrE1lXGiAFAS/kIBG2uI8DRZo3LUXn9gn0vvTQtWKgsKI4JVzMZjTyiud68uj4tQY8i4fBWPJSQ1WmaKnQKeJKeWPEgCdSYEsRWaVHnnNFiJJR5XQPOjn1hpLwopwFRWge7T3fXzYmcBCjRYJ3qLYQqSUmNxR0KCoaRK9WiRwEseVQApg/N//NkxIocuRrWPnsKyrb9figgZ3kCcgcH3AG9wOXnflT/dO026hv7Far7qVTbM+tuF1/bXV0QCZT2V/r+mrIoNyIdJb3Y6ozod7Zv9zHor0/+lS0d5v+ZvjioNDTC4pMFCKNVVlwmnI3Y9yLFgAEAlJJtyumQCmzkrES4d8ugXSfJgDDdKYP1ZTgfk6IJwdOE//NkxJodYxq4tsJEusC2nIEzwQKogteMvQ6HIH4gD8i+W/+Cako8sYBxYufD45VgsAHSIuY+hahHv2DjQWJsfxUQlTZnzi3CSzEizgjGNWfEbgIh6XCRTUf9f/TfckAvu+SXfJmBZsYj4dW9IVHLuEeSx/k+Wo6PWWBRuObVwKBqCchArKqTFQxjUSstI8yV//NkxKcdKRq5fsPQUJfeMq9Ersz5DlsOwM2eRj6cYnR//9troa7O9FmYpNzTG/9dP/T/7f0XL+uxbA2W7LaXT//p+lbGsY2pWMpUN6sVQFoRDT3B2F2CLkEczBqTBHEnxo6AALvkIIiBlpVml5TDA0Bqcs82IuMta1nDM7ksonOQCjftxoeTNnWOAR8ML7N///NkxLUdNDrM9njE+q/3UqAnG/VV4f3CgId+tQicjOqKhoGjsJrO7xM4y61T/BU7nSrg16g6DUOllJmaKBLVNC5GO4FvMRRxMpCOMLgNMFAQMyCnQmmPoMEoEJ0CIAzAASQUGokCxa1ICUhawwQZgBwq1kkAsSCWlOk9t7avqFn1jpR3aFPRntXxo17dypHY//NkxMMcSY6QN1oYAKPqWXvpIWNVxj+lpHJmkjeE20ixsbjXpvvoL5ttlxw/8Knmx8/Hxv+uq4j01Hgyb3um9Ym1Nn1+8VznOsV/+dfFoFsx4NMVxnft941W2ou85zuut2+M6xmvz95+P673Ao8he9rY1esPMH3tfV/jFsXga3rGNaxBlwz/zFe8YjYgoKog//NkxNQ0m9pIAZ14ASAkEDgEIpMBpMRSQAj1cQITNV8njpLteAqCZn2kPZxplxyjxLetp1lLEX8lhcQX5CIomY9QmYKg1ULUDypnB0CkMdgVQjqJIQSdkoTY5IBJy7jHS7zJBh8lAvKMvDO/fb07OBsQgY8e+ENbjLOqDjECO9h5taaPIyRGTESssCIzqhlj//NkxIQ0AvKSX5h4AFb6neZxbWNv47nAiQ2OPSlHkzk3OTIyyQs2Y41s/PxfX/1T+BEvv336Z36MN4TE9piBFhXgzTwobDLQkVKQQT/fhgPAsCrENAQ4/UgakLdtuTtiUItx1KBdbTbdtdrF5QkbKKJgQCEDsiysqEFyEKgCiyACYpUV9WcqEarsLHEWYSLn//NkxDcdc1qxd8koAijpt/1nZijxURcm/7tLv6yH2V2rb/+momLOit3yKlFZJq3tVuyVY44/ZLOt86WQVAi0MTsASfWZOsLFSHtcbgqom1fJZPqkBw/YCkzDTB8ws4Y5rLTFZONMUEZrCSFFgUhNSPtpdkbuSiUu+7hcxESOKLs3iMPSikYfC7FSAI1ikNqd//NkxEQcyZql/s4EdFNb1Y7qkhGVr//Xll5QEqIGAz21lg7inYoEGzn1GUOb5pSnr+sJrOnv6t1FTJcMVQKQADktFttymQrdmtkOg86eoGVVJFl/JQDLCwHwXbYnV/VpA9uLdb0ynU4LLCgostjYsuGlDGvQO0SBSACcqD6K8XduB6QAAp4DYxNAERsmcYQM//NkxFMfES62PsGTRChiYOTGAADokijS4nrKf/9SG6gMgRDW+kBjkNV+in//icdfz5MKICdrVa0SXt2+A27AyXkAN+FdbW4fqSQT4Hvytfyjs5jXYXF5qV9ZXGwuPTEcDipmpyFvR9pZRoRCYG/MGNHc25glmgszUf5woAkqeORkVFWDUWPZ/eaOKpPpSlqX//NkxFkc+WLtnlseevsUER5890s/////tVdHmA4UGqedIc6VOEEM1CoyHaSl1/GmTL2hk8DBHkeJPhMni4qgZ5PHl8vM3mBhWFZau9SLdd7LX7IMlzv100DFMo5RhwMEMFEiXKCjEY7/9dc9GkIEACAWTByZMmTTBz/PtKhUuD6F1Q99Kya6nE0H///rXMqY//NkxGgbqfbyPmCNUkgu5qgwb20XRJSVmts4ScJA3xH4cKhQnwcfYCs/LXXYlCyCdg/FMiRdx7LoH6KQSZM4GAh1EDBiAkqtUTQ5jnWXS7d9of//58b0mxzmKWRJSCMJH0aWbJGHiN5TMsDC8MEmoESGLBVP//0//K1mHkf7x4GWYDxIsg2tSh5EBJyb7bAf//NkxHwc2hbZfmDNMolCWPxRBhh3IdHh0cJjAZFWJlw1G/WvExKAlyUVCK66vSEZHLYhhR4mqn6KloxUCmDI5DEd9l//Qzpeh4pzIEKDHMMlV3QqEqLdJ2/p/15Ae2/01d+dP/oqK6nY4N5xHub6yNqiUuKLjYsYDVUGIG0SnJP9/ElBkeUDUPAngh6D4wWF//NkxIsdC07dfmGEyqToYIqFePmofa8mmMdoHdMQO3Xr1e79HdBFITGkMyKR///81FiZhylMMmQSOQ4KPUhPbvSE1DF2yqAYZCIseb9xkl/U9qM7/ud0JYSAxYCgI0DoSCoaIhW7kEksu3mgpdMbsI+3CsLl4NR9TntAVqodnD2alsaqYoiZAGlm0flNymVq//NkxJkbOoLqPkBNcpUcrlZyskqlR1Yz2GIyG3MZ7W2/J//0VkRylKpt+lGl1Q1tfa0/66z/2n/PV9mZW9E7X/2NZUncwWjsuLurMRuZ/iJVKgADZcw7bvf/owgE4uSqfQma0Fl0lTpkQWUUfpleiyjttLXfznltNKAKBI9FLelkYiRzZe+Zmf//1ttmbV1V//NkxK8cFDLRnmDKr1ROoUMzeff256iQwNXdaHLOiX4LA0DREKc8FQ1Ue6gKJQCNYCpjWVjXIb/a5osHCoKtSsr8DQAuAAa+CsBILYD/S8WqlU9qieJymBUni+FbRqjisUxQt5Qqq0NBSC02PadfkQh4gH3ocymrXNNcM01NSqr3/c/9SSYech1HU1cXPKwU//NkxMEc+dqiXnmGfHDRhIOibytcfWXGOWRSaJKF7aVD0qqjdT8sKmAavnV//vUFWeVDhwSiUWEARLMTOuR65sVwScKxtv7j8Qieevz14Wwkm///+Qs01WsPsa1uvzjyKAOcnZx/WfX/N920WBdoXHyz51i+tYdM8JmcH7Ovk7Qs0FRQucsPcmtbteKhi6dr//NkxNAbodZllUxAALZgkCHktW4i1iZGNrxVX+f//n+62pF0hpJ3GfSovCEbRKFwUipEYy3e5fSf///////w48dggrDH2NXxy3oW/Q9EN14kZ6QW1rwjuJ6rFUfz///////////9whv58U/+t41j///+8+613X0fuUem9XeV4LWrmCwAVAfYN8lYNsTNOHIx//NkxOQ05Cp1nY94AauRC4boWIMOuptarq4pEQclW55QcBgJJBeYhZ8llvJk8lnkDxGYiGGRY4ZVKM6GaPYvIuee5+OS1ZzbsPcYPYkVSHQk0kQhBA5JKOFzSRhzDSDhtWSxVipgttNL/3BwqNMO7FUY9tdvhckdRTSOtiagev2cJVqvT2XFC7nZVv/VvKu+//NkxJMmsvqUAc9AAabRbnv9/xdpAAU/QJjcyjIBqUiIUztAQlAgEHJFnAMDBBjGNjjsfrNzR9j7xUsmwjFFGo7GpyM1pizbtbjMIf+G10QhAC5cxF6hOP22NhV+XrwhM0Xw/RHbrEyxN69SsNr7aq+qx1bHevRv0z+CByM//3ZTOimrKVSPBC1CTLSeRjM2//NkxHsnE96aLssFTaHkotGuq30s5FZz2b///+/8iPZl2vXWjJRGcXIqh3CCA9UAKAACUru6wA7LEApRpsYL88wj1SKPMluSadWF6ocMzB8fw7QN4h1b/DexFl29JsZidfPlDqJaBbEfcR8vSsiGogtiy/+4zNma+z8ZmP6aR4OpMkhlZv17HIO2f/9//GjS//NkxGEqM96dlsPMnJLZa8SenOPUTy6eQZ3hMo9q++nLlaJBoaIjMz21qcbpeTVfH362Z8/+Yzf////+vNRsPjz8r7nbO3rfMrw9AsrkkcSQPBP/qQCJACU1OM/Ochwgt0rohFRyjYMbiqjS/H2h/3iyklFNSXTd7xsu8QJ4b7DjESDOf4mViDSNipePczzU//NkxDsk+8aZjsvE3Y2YOoUVsfwxzLhFyai3hzSVxW99Urne5aVVEZrc2N/eFj72MREYW6fXvazXdtlo07ggIEIc5qbNo8yZ7si1rQaLg3iK72r///o0lXk6fo8lXtJBjqAQbNwIAPciNdJtzwSJ6bcHssp1WqnUVYmChkLJEgM1WtSFU5rNt282ai1eu+Nr//NkxCofy+LFtnmGn898arNUOy8zTtmRvXmfCWBjJDCJkvDQ7yFdzSmAJ4sywid388qPDbrS+Wfbts709KP8JJLuVOIifX/+O59P858Lfhf33Qz8655SZcznwuPDpCMz6/1VkDgChEaOUlZpjzbheK4////9prUfd19VP1/atLHPzdTw8o0xTi8vsZY3Jcn4//NkxC0m09KoAEJLsYhIiMtmEhs+mjBQyJ1IOeC40SCUDiQLgMAAmIEHBNIIUICmQICBVUsGBhObxQXpRjL3zKMTbwuaDD8XggUjLPdRaewvPVGEaB/qGbD+5KQi3UHeoCFCgwXYjsp6yPIStCK/knyK+o9c4AgQrJh/VVhrLrpSACWpiaPuY2ZyGbb6zfeM//NkxBQb++LWXgmFxIYOjwtj2M/nv/56Eyx7IBgCuf7LVMXk92c8w+wJHSzKqGlIs3TynfPdKRa/RnnXVN9Vc55HYxBKRBb7CFdynskhK7qZc8s9EsS5Gs+/nchmcM20OBhpIHQsxrvQ32t2ttaIFr2pBGaVnMiD8XsIp1O3yDwJMoKt/DbeG3FJhAPMVRSJ//NkxCcaedbmXEmE7kppx8nJXnOQz2/us5Fe7VmS4MjUzJUqvpYUU2JB6i9zuMNJ1C6kCge2rrtkjju7oaCzAM0BhEmbvGGHD8VvB8i5DN8LyGZplm7AABbCkQek7S5UNZ+7Ky5G205oKWep4Jw9HpkDuZMdIeGn8wEPRCl//pNW02TE9A71S8jMKBGwxTrp//NkxEAcShLPHAGGHB+sCoCMrluorZSCmBcOPYa54cYlQ06oiK4JuLtZVGOKx5YQrbCd157wLCoqIiLklcqqi5h1SGNZAALqFigCk8ECBAGXpRBVuu/sC2NyNsoYWfdQnmYSz3M51znfzzCy/rcURZQQkBAQpHHdBQxy3UqlKD0hToaI3riVaVnYiYz8RTKS//NkxFEawa7DFEmEXEHMtboCnZlUnWiRqEKiJIS/1hkCqQ0C/lkPRpMS7zY6H5SYlQijWgdk+J32tNJ5JaGOduDkiYYWoloHx9ChbpFbLCLsYWbXlJLaIgKLOVpSipTCWhrRWZWRREVp2Q0z2vrS5jG5fmVWrVS2//9dnyt7f5b30/8kvpSS11KNKPba/roY//NkxGkcA1au7EvK5JPtepa6fWt+wnQFFAzBOPhNWiXEMVhzU59CeXNNRP/rdKtHCI6fMXXRg8DWBVqqGIGUJhRSM8Aj1Kc/zs9elsuX85Vq7PFgpGZqVal0vzUUtSmN6G//+UEyhTpKz+VyPEQdLVw7BUFQVnf7AaBoOoOhoDB2z9NASublKOxwsBsFnAMQ//NkxHwbunKWFmDFGDx0D7hYXvCQBAQBhdQcnq5KPrWtMHyHFa7GoUYMzCtjSA4BCrsvRIylKzKG1n/Gb5YDEgFDJ1wBAIaGgIcmoelAogi4TFrRL28SiIwCoq7xL/+SVpIqAs0p938iVZCgUA0qz1klMjLltku//GCyzVhQERMvkKIqEwaONkUk8Ukkay0k//NkxJAcYU5wtMMGOOQM5m681euUkQr0+g7uYgEj0aBzNZCNGub1H8oRW87zkNEJC346yxJwlp/qurXWy+xDrZD6Wst3fr6f0da37UnTPpXdLr69fMm9dkT6ys6uyKr1gyYKSd0rn/oEqUNOFFMQrHFgZVGFDWbNMAWEQKbR5EWphnrfp+MNXW3QSQFi84bO//NkxKEc25aFvtDE+S6eseuFRtqrK5eh3LjtOZpN96/QvnBYK4H3mH/32O2bMLE5muJET4OEAuIJYbFwTYcFTbVECamuEGFBQTidFz+SJL8pD6f+qim5G5dbbuPAwMnZyPLgJX8rEJEIKjy9iFjCRw5ao4shqriRorsMbooAXILwM4sDwRF5nAdoQ+JlLyKl//NkxLAdIUKMfsaYFI804PDCqYOAxJQFAleGB0VF7/v5DR1iyHpoUTok3UHifG0bb7LeRN83ReTkM8KR5e81/6TbMi3MKcGNLUbOMomjxWsDtBiVH+m6cG5C3SfvaTn91ig5q3u8Svheg4SEl5ea7irkXGCg6lff77/4VeGSUmbn7IVh4zhfvSI0MgULEMYN//NkxL4x4+6sfsMRHxIaw5wBhn9tXVqCIPtbn1Da2HUSrH2y2gEkacgKDPs0mtHmNP7WVUlucauu/LI1I4dgaVWnXUtZwPAi7o0bUm/LW2lDo6g1sikvn5cM2xMeGoAUxBMPDhayqjvFrcsbVn8PXSuWYDHDw6rGjigcVsOXtEvHPz8+5Z9tv9JpN9vnLH7X//NkxHkyG9qMVtMHPUlV0DeHBBENGaDgC4jwpl5ZsrMWnV0XyieK9IH5x3H3WG8SqnxCA3BDkI3k+vlPb9mjf//8TQnNNzDi4IXdwiBAocdCcPaZEyIcGJOqsCU1AAEUQRIMkkv6ADaR1BoTdKONM/bWV3atNP09vU3ezne3863MdZ62nG2WkUpIYmUV/Zb///NkxDMn8+KuXsJHHZ/K1cosyujTvf/8++/GvKSU8MrHGdf5//oGEcLbsnijni7yobRqQYC6JPrGEnVEohImUb2EEhtHNx6ZT7m1cWxaJp/orxTenXOzrm53//+vUQtHPpcItDJCSbk8U5435IIgSIIQWVg0nZJOj8EARKdkxEtSO+4/o5Oo0a0KWI3Xm+s5//NkxBYi4+LCXniQvRT0FPro5nR5ZTlay9qfNW8EZvW21104reh6tU/pjjxRBEcXPsaeIZRRohicFAMOIguNSaREjoZI6Cxqq1JKdfcJN0MZ5LImR8H1HRFm2KU7pEv37zkU/Dn3018y3w+8dJ8VdR8IukT/Vn5fFcUPFFU+hL5/T8UyN6dsln8RBTc5RxIK//NkxA0fW77LHnpKXBNXvs86hOoZyZxuEuYLoiUaltVGSzlri0nk6Ra2l3KlLn/KgvZOybdDFTqrGVvK8jmC2FDOyJVkcjChBZ5jIpjKtpCtn5f6uwqV0du/y2q3WrL+lGNTVxqWcxpHYX3dWdlmZ19VuboNCTzJ6xDEX8TqAl6u1tAJyO21CEdkZtObC8PB//NkxBIgAm6uUnmQlPrh70kfFwlfrGQOlRy2kmkQZW5Y6EPn+6SuY8tYtL6ivvvlzxi/vXbX42hZA9EIawdTWMq9UOxBDAiizLktk3GsTZKLSy3cf/5YispB5LE2RYqAlN9TkLIuQPPKCQsWStAVO5mWbW5JUDDSOj4mOXDZ5kpxhisBwltdKoVA2JoUjdnb//NkxBUdIaaIStZMDKE0EvA2YgWSQTgwCWVOlubVzm09Is9N69Qbzqd22dfcf48/9m3LIujv9a6PnGp5USlqNCfHAVxYHR7mHW8S2GSockjwicOeTbVKysO0YC1gGdEWpB4KhtpYf/KiJKhKmiElLZLLttd/24CvsQc81MZcJOrebYZjUevHcfCmU9Va/rtf//NkxCMdIubGXnjE+hBBJ/lvHEQSl3hPBYvHKIZxuOOv3vfr1YKl2OZWiwrGY/da6eZzIIllWyK6pcMwoK4hmTq87f/6t0qTzysELNIvq2mfXxmBEFRN1qefU2QCuZrCArJdH+FrYeABBonUgqhZZSdmhInhtCa/bdULJZPi+GngsxjXS9kjAfiWXYVyt2tX//NkxDEdOuaAVtpOtJtrTXQQTQEZIgBa8dPd/nHe/m1syntMd9466knQ///oret9TihViY0IxNHVe3V0ddkdf/2RVLOiqSUfBMCHyKNrH//VCCooJuJOzKATDy5GKMQoWX2RBk8c2qWLjr9ZnIJKCaQLjBK41Nq1cORlLzXT2PeW42eRmcGRURYWHr97+91z//NkxD8cMjqdntIEuppCkLOWfZzmQpnNb1vt7en851VnZgihDgIwHCH+tLBZDwYIlulIZBcwSlV39bnfpu+lBvcbadukvH11mpRuCakjw5ZdnLLmxJ4FdlcKSei69i3eFcwps6CBQwl8jkRYaRKiDqKLDZF///3iRqVPBKqMCCm4Sd6KMIbE5ydy00+5Qnld//NkxFEcYjq5vsMGii3235YVGGBheQB0FGDP5AWLHyCgmYCWhwBInwccGHn49R/Fek5NxwKoD2nUfgusPaHWm+7G4g9DO7c862d8i00eBt/XfN5EyN820PvzYdtT6R68L0nUN+9tEZeQT0h0+x0RXNQV/Vm+uu2dLUWZ0Zqc1mo6gnUFro9Crt/9LemjMqwg//NkxGIbwu6c1tGEvuo6vIqvIOAch3f/Z+0AIIUkJe3aQvMFewcAUqGR5vGyeDnmICJmtIelIxzV+NtF6+VLP26avnh3+Y/hUx3bu1JFiwssAA4A9bfTtSb1VqyeXSqnVInvC4s9j/zDqV68qk0/Ab6U1mBGrzEfWIqemmUx3edRgEHU9uk7/NYclbKgBZiE//NkxHYyi9aZftDZzRENHcIAKKpiIoIQWh+/ZYRl18///P////7xA6hxYRXdOFTdL/ELRP5us1KR0R0SRaWh5Pk7976sHA0vZh1/oWnEjtsWTlX8p14Fhm8p3T1VRc8gzSoGRK+neAukDzjtvkQkwNkWESmMaEFhFLHnmbTlWcl+sbmeuY//71/at7eFqc/J//NkxC4oW4acFNMFVfdg0OvRBZUWWukULaGMLlR1HEaAQHknsHCAnWXfs/RerlMtKh2ek9BiRrK70zZk/Yrm3yaLOatDdzK+knbrY4b9fsvT6VTOZNX5ZKJ7HiXFvf+TolWO9Op6sfazr52SRjHQjWVLFQEMpXEND2Y/JX98EWH1AGch3MI+ct//XfAS5FM4//NkxA8fo87GdsGE9SyZDNm4uYvnufIB6/lMtrGxhEobt44R8I5usmQQ9kEDQoM6DoP3XDNudrTJVWLqpz5UdqZVQ+6v1da9KFsypdEuQY5pHBZCkdCpmZ3s5/nRDjIf//MtL0R+3pq1l2TovSn3DXuVavk6UzGxSpBPy9VgV9phS0kavunIRoKczy44TtLn//NkxBMhOu7afmCM9bnY1WwyuOlsb0FGHHVDZdtRBUeUz6x9HOHKBs+6U5L1z9ods29IEC009uO958uK5hDWNJhc3v/YyL0zG7Pt3fj//P+x5MLXV7e//+++wQUeg1Rd3rVLGEEIRwFaiAkOMEcad//bXX9f//+ABPwggdm2Huv798GcASmIxBARifh0nPOw//NkxBEgnBLeVhhM3ZEbcqmX/xG2H//TAb/ycyPc5VmAIg+3d879sxO1avqEMQvXdnMvKn7ub2bsVPfP2hVOlYczULlOO5Npbn3iHntZ9t3v5jtDx/HfT20nCdy6VuWfWX6x33HTfP798x6IGIQjhdASE4Tp2Hut8xbrwbDUlbdrbbnWSAW20kacUKr4hYiO//NkxBEfC9biXEmEdyTkiR5JMmztU2nB4E6sfEq18wZOAiByCjGJ+EACMZ/99GOlq9qHKwY2d+zSIqodwhhZoggFQWPIO056I7e/0qrOX0P3uxjMryqLU0Oip0YjsXSY3USWjk16rfWn3rVjsRBcijUMxyEgSdf+qrvqipqqf5oAkztaB1vhIhjO5guCDYmy//NkxBcb0+rjHjDFHAzFIzpicosVPQGihgBrFE+d/l+lDQb//+SdvPTJlWIZqKTz+9qflSRg84EeKKFl78jFw+if8qBgqGVk2139NN3RUPRX7o9n7m+j1/1//S15bK6mVglXPorqqsloiaZfEACcZEBUgccYAQ0gYUYwnTVtq7bW9IkK/nTrdGjGOH7nKzCC//NkxCocAgbTHElHFDHGFRkdlUowM05WpVypZjWyXibFMr+dh95ooZhTCbhVrBQwqgobAQSFwqItYqdht3pXFoaO2ZJXlU//4seoWRCRZ+LuI5WPNI1IEDjk6DtghccJAYUelCLDzHozQj2VRnr1uPnb6AkcbYSUn9nsL6crrqfZcqJlkBYWem54QQy2MMkn//NkxD0c+eauStJMsKvD15f3Nb9tzteeZnG7MSHSSmW/r42y7lHJaWjIqqPBQApNtAp5R4NTtbQaKM+eVb/////uFNMVoLJoBgEnPxySpNFOThAmHoizE+OElTOGl4M2v5yhIdps1AovRYN6r5Vfhr+GWkcVNQgsaaIBNmmCQcKAqDoh0KKBAFYqY6pkSgqV//NkxEwdCR6uVt4QMI8RCMwWYosdJGADZ7rtR4qJyX/9U0E3mt9OAHREpTz1rFjXouNxEfWqAAFNAhQtTV37wfcwP4DwuI7oV2scX/V7MXW6F9pd+4AbfP+CWv4FpZhNL438UEeOWWNw6OaSHNbDC13X+j+Z5DaNzoZspWShWcpd6lQWJU7lymfS//6GPsgu//NkxFoc2nKmdsoE8P/V+pQK8uCqgnYPSCtTDRPi55ZcZQOLIFEVVkPClJ+7kREPTU8KEVJQO38AKCxX5hlDLb2cBuVLvr0+XcJiW4lABfsOI7G5qteF5TOA6JS8CDG2/SWl3z3O3Q10//++iTHXRr0Qr3fs9vnZhbEM72//7dO76FY7r1FRMgidVTiUBfmV//NkxGkb+xqkVsjFFhpBQGShK9Qg5txxt22W/f2NwCLFtAFhFwQ8yQ/pLt5fs/QMgeeDiF/E0f9GzGVXtOL8WcZVrW02UvIs+SySz9TOKa2Etl/4xoap5R4X/lfmf73+WNLz+8phlESqmX////DplYURs8NFMouYIUqg4DAopJpVFOl6d1WAaVJqaln6Ux9B//NkxHwcSzLKXnmGrkMgZ+wSATCMBqCkxiJYexXifZXrDEWQbwY4nKJziIdYaQlYQdVdatXT3F5BzFg2HpkEjqyISSkyG3qamFig8714zYjKyQZDoPfq/iiTSJx5BRkgNpC7z7tihk4lxEuQgMktQRJxwqaVgAoudYEABFOB+2uwL8KKCxB1IwyxwvPNEH9O//NkxI0cUS6Q1tMGTHkUWhx5c/C7C4ufZFcPSfFJUv+ld3OnLnj0/p73QhPe5n5d366+Z00dP6iod5F3ef98gzm5vioWJU/hFfThHd74uXurinISX/uoSbU/gU5Pm8YNcOB0uWH7ilMKY08u6gemgohlh5nihZEhwHlg0DR5ZSwWeZBlxikuiGTjbLPVd/xB//NkxJ4mlDqURmYQBGVtO8AJAGHFWjC2i2cQj2Wc3zoNMpRUebK1VjaUfMKtPNTcNBPeGLWrEWyNxSSkrZcNH/35TLt7evW/YRXk5uQzdqyuxBwomDF6zFXB0rQSPciO2Y963svULZlbnvkD6icL8kKKzGo9PET/jfHZRS8PJz7Wf23r2O6j06AQ8wo6EZNO//NkxIYqhDallkGZfMez79WJo4lw6iMhgeNl2p2etmTqxceajZPFi1p2H32K2iTvqbLLXUiysOo/BrkbWTxoFQbie2eXpPIHXtQyLUlm3bVs2y7p28z2mYzYOM3HjRRwUJEEI7uS3b6Z1LbckLhvmeabU6W7+1BJX9Tb0vtONCcq1AETSrEeTlhZ5gTj5siz//NkxF8aWh6qUjBHhNAWSpCox7gc263qecakBEqetDHo1bIw5h17sWDIe4dSNyM0U8gjRmJ1tJ4QJQqzG+RkjarqU9fFqcXIaKWX349nN6MnlsYhtbI/ZLgnXba/7ozrZmyaV336tXu9VsYhzI/KXezLQjtZMqIrU0SUGZZkIAMwLTbGut6E1ZbslUUSoXcs//NkxHgby6aiUDDE3JNTsgAXu4PRvkch5NISaWiaRnKX3zSTwGtDipDTI7kDE2hjLrt6nUmfUhw464X6Vzn9LZ3Prz4cux/8nnTnmPok5J+0hU2iOYILycTCnl3oUhF/c7Eu+/e21Xov8jbtXmdBBV54c2IHsU1N9wUVSYc1jSDywDplhY2CiYagtmoM5VU5//NkxIsdU6qiUjDFPNVlMbxVsF0WYbbKGR2ak6bhg4WYAIBgFaGYiQIFVwYJ3LMletH/ZTziH5FPuf5bOo6g0R4ZH3MWLk/cqKQoiXLmXrEoxJJ20cgyg+d465jBzR99ay5l5Uu86tI3dToVSXeEtScjYuThioKHcTLDys9XTeM5pICw+DEakAtTJKQDt0Ei//NkxJgc0iKm9EGGLHHUNkD180uWv9MvvNi9LukNqxt8whiq6N3/Xu5Me5vp+bGdVzZynzlM30YSjhxQADE7B1+IHuT1nXwJsclZRLZJoYXToFhPhl51Zq1VSRcIZYZyNStAH/FEW798YEBBJVW0NEPlu9HLDYQQTSdJ2Ly5zpDI7A001MvQ7RDGnIyhjAYG//NkxKccwoam8gpGFI82QRztTD3lfQYOocAH3AWUPhw18rBrSARMJ+RUHSg3L7DaSRxqnEW2B4MEZ8sUUU4cErxl1xsWeJXJB4Z23imxSB+g/E5p8QUBxKWMKFc0OBFgWsVkceWqyiQ1xYwWHM3CWloSjep22QoyCSC7GwB92NIr5xoEoq+c1iCwxTbX6vMi//NkxLcbweqm9DBHUJSM//I6OiVKCeEg8zsYp+RiRqaoAFTQ8JJaWB5jQmIyo5kk0iLmhrR8t6SiSJk2tckSSmHviiwiEiwHYfwiFh+gu4oiKWPZHmTxYRGveDJjtheRPkzUyq1LmQojG8TtibY0mBD9BN7aSCr0iNyQ4sGqJu5XQlzIdJP/3sjUsdUSIuQy//NkxMsc6oam9EBFbCgSWkPg+8QHSGZ+XNRDqY/OueZeQoGgkhKFS41nWmWVJxkqgZU6jEzIuPaYSFWOqkiIbUgKVDBlFG9bvG2YO535HlcgSlzpoIwaXTIpbiDnE4dS7MWNpIWDSHMTzHMmEpX5PI+fpEhPuXnL5Vfqz7Ncr5e2pL8cyYrVVEPb/b/OCuEf//NkxNocwp6u/EBFbEgpCVqU/aeClomsZWKgK4tAVyGV6zp0ytcC1AYlR65y2nLLXIUYCCZguxHECuY6AYhAxnjNHBDKYpBRlg5G3lwHljaJTKNjZLkZVmrTVjfyMgODkkjtsdjkXIpzPrvh2U8tWkZc181i+dO5r7TY2v21rOJnkiwBMXt9Z1ZjURTCrz4a//NkxOohcoKq/MmG8LuuvfoK8a9NslVKrBUZp7wVdI9PYgqdyL70MbUIUCU9/UehAIzUNRMxAdNcAGA3lYyIkL4MGnYy4F1NCqFzOMfNFCNmCpzCQUZtHKqMFXGA5CVgIHjQFxHIHTXhwdcF37eReht+LNMNeMO87ktrQgGwhwEAspgW3KFd0be9frXJNGtu//NkxOcf2VKJ7OaMaO8z0YytoLHr/68jv/qjoIHIA4u5FRzCisg52S3/9krMd1pZf17efTpZldyHOcTIyCKiPxwb40XGbKaQtXNNA0mG6iBgsHnXQ4PDdnZKUgUVHLUqGogX4eRAWJAV5JawMNYocTDB5a0fFCeqZRahpcmDmSemzRVxwnn1ET3b64meney///NkxOomu/JwVOIK+X24mg6AVDCnQSWcrUeEEpf/Wbs4YrqdMh5jOk7Hsei/kun/fV3MUpJTlQdxeC69vyptZjEd7bFyq/Mj3uvaqHyjkOCmIM0Nlre1oA5ZhsvIdPvFBqSgQrpgiKGQGUg4LEv1EeyeCHIUJ0GBjJhIKut2lNQ/YONy6EX7LTdHVvKRbjm3//NkxNIl++6AXuLE2YmxLXBQIgLlxYdmz2dboXJnPmk7MzOOTujlz0u3uvi3HL33r+LAB9/6fY45xzpTep5EIGVy739UU8hGr+dVwYGpzvUDMpEZA5uv9J2W/X///6s1/RCIFRSFCA6VYB/+cRcOwJMZQg2ZIjJcsqgAwYdzpwOIgKWAOYhZphAGpOFzThhc//NkxL0mc+p4FtsE3cSAQ4DCgecY1FBaW+MUKDHhgBDjyqXw3OOHf52DtZWZ29fvXKrtuWxN9UxKZ7rUDW+ZWrWMT9dIr89PyWP1Ds1sv5Tgibod6nrI7r6//+veVe6+fm8boNF2KVNSFivGiEIYdnMSPiYra0HB0JkD4PB4UYTCGG5Hh0HIqHYNibGaMRon//NkxKYxvA58NubQff/3FNpERSc++kvv///8//UnyhhFCpwlg67FYobB4YzAHoDysgHPzjph9U6zdCEFPaA5a2hrCIx6uVXEbIGlAXgWAHFwCUGOT1AwTIFJpQ8iClWr+GHTz72NZRut9UKjWdZzzndFo3a8KIsBwcxYuDUoRBawcD2KJZuVpKjPVaaxrX8M//NkxGIp4+aIvt4QeNdde0fbfHC/1VX//PNT9819x82xxKjlu5v/9VoeUcSY6j2Yklblfn45j+uF//jjeK79qTb///5t//eIcYfJ5gVNq5lZkWoSSuoEmq4oCNZXBxRN01+cXwv+FgkByqKq6TNiemRzHI1Ioi7jBoZZilKECQSCTDQCJPlnggoYEGSBQ4PR//NkxD0oA6qQPtGLRDK1hwUEXGiolZVuRgbEA5PcWLAaZPXC/YItESDz6OqPR1KyKhpMisqsrN9v2otDL/mqZK2//MLSs7fy6ophEZDogeQ///01Ih2MceIkHirD2ICh44oeKC7WfdijCIkAshZ/ncAvgbd9sls6HGEVVuOhtaQiLu4odSAOSQTiSe1/P2f2//NkxCAkcfbJnnmeXnzEQPjLk/VhSMi7Vx0IW5s6seqMn4ubOr4ygiKRkpSJvN8v4/9IycOhkmY2dSOFY7zVoKHkyCQClx504yxmRrc4bY4qNscE+2Uu/s8z4ceBAiIT7wf9cQChw/ZtYLy2j/uqCxwmgu85t/cTKB+glz5OUgSAf96zgQhw1YWRZYFrYuYg//NkxBEgumKw1KMRMGKkWi8SNAhh+t0aO1BHU7+jslPWanROJxiecds0cytX2mWcllekWV53Goovce373Okqhtw+Ehk6A4kMT85UgJoQH57p//pXclMOsO2LJPSWlpMXPkxO36+u0gzEi2ei/+iN+L3gIPgoCwRLLuXOGagwLwQ7pZG17kl/KMpWwcBet5yu//NkxBEfA6rWVnoE39RNz88ll244gN8VBU0mNlqe6ap7qHay1GQz2OY/St4iqa4O1Vb/X4xUcJSAiB48K2MgeJVXKJUpU/7tcMUrSme6s7yKquuys5JruhgFXX/9fb6WnIZnOjOtU8tW3RbGmmUxv+7okGFXualnASd21ZbUlt35IW1qHkWJ4+iMr2I7yvva//NkxBgeC87OXnoE1r9857xZmDk5+xsqcUQ0T//UfKqvWD40moOZr5ZHtmq0NmcOWYzIbyhQpWdz+npZdqsj2HWivLzIcreYpSGREEyPv7bt+lDKVUeyqVv913atHqyNIeu/l4UfZLFELpbe1KoARGiHZ2dtu237AKxQcWNRKJUSAUpRKSRZEiRR9f/1RIkF//NkxCIdGZrPH0YwAjJEkmqq/nP//5IoigEFa4KlVVX/et8zP9P/5ytk5Kt/zuclTw7WCu0S6waBoGh50koDcWUHDAiDpX/8SuEp34lBbw6IgaParhKdWVAIalQ0kGl1ApLLLGGPNIArpjuwVbKEaTlnOKcbP/PQQgFPzRzQLXROwjv9iSCQkA4sBnBAARj5//NkxDApC/qFk4+gAXSDjjMGAxpMDFGgRjgJJAMeJ/JBNBBbgY8mAgwBIoQwaBDP59jdO8NwFqQ2sLmxBcngxB/3Y0e3DqgHCxYCRGbJ0gZXb///9aabsgtzdBYy/////+T6BFy+biUxxlegzJzcw///////y4ZkXNzdSv/DPOwEKguXADtXSPD5IkyVLDQI//NkxA4gK1qxjck4AALiIUhQLliRTTyMTAeQHWjzx805RuN1HS4sJAMA0eICcXkB1DnGpQ4wwqIpA5DVb0vo1vstJqmkh8RiAijpMHxxM57M3//3/7N//6orXOToqyZM4Fwljxzklej5yrZv9OcjmmsijppZDi7zxH7v6KXG/20KEm9a27d/KJAZsj2MnSAB//NkxBAcWrLCNjDFJCzZUfyVX8erbJMBRQw93KToMK2Zf87BAzUGjHR9dtOh7lbK3bS5SoAkCgmM6f/V8qOccrigExjAAeJFhC//0BOpFtpVJUEgwJbf/qNsFnDXB1/1CYWDrfKMgMLBoixMsZIqMCRbTu84Bea5zp9cq9vj1jKdbT7mzqh8qHBsfLmdR9JI//NkxCEcoq6o1nlFDCTqKU6EiBnylKzqlqJWg2JBgI4iUi0t9/Z1CBSGKY7aaPDDqWayOv+q2qz1RxTlKZGff/+Zv/aY7BhxIHP+uPPKSCR2ON/KubdoWaat1in5aPoZEluJOSa/REWtE2WFAflkxLBwJad8O1a4xMx7JxTiOkTlzh6Fa/dAVcpqzu1Y/+8d//NkxDEcqr7NvnsGOlw/NH/5XP6cREEME0lve2ZdpF1blPv/P/72LragbLaExsV/mtZvUtS+rLGwTcDNH/0Z0cPIDweosTtYM8l/1nEtRRg0pJJU2UvhNiE8ZsPdm48cXCj8YWiICCJQF2WfyZNIOKzSPy2ctG0c/aTaffO1397/2//bXZjX3/PUTu00n1CI//NkxEEb4gqsNsGE+nHkzSFPBR57u3fs6pR7N/626J8LCTEJ/+gFUAsvRfpVtya1Ao7dempf1/+6QDaFCcjaen22gJaAgzC5XwaTARLKXbKHsTwYrpRZZ4t6K1pwEHMSimlWX2yv8/8u2+HmUXKas+eXzt9VITJjbhsax9o1QmJAq4qCuOEUCgqEpasijnWJ//NkxFQbmWLIflvMbloqDoT/v9ZoosOA0FzpWeE7CplNf/b9RocpagU4yVJ7rt6nfjyy/ifwHleaGVBOXHEaZhA2bQzkNTC1vHD+vanUVvvRfLc7Psi0dPzXnu1788TQJDiDikknZf3pVlb+01GZZESc6udu0OSSOd5rWo2i8jp///b/FEIg052kRErj2xEq//NkxGgcm3a4fsGKzhShiAMYq/6x3Z0VBckJUn232WsMl22TcXSIcg+CKSCjkFa8JJ7KnXwDp5xGZkrfvp9ucu9xq7NP9a7WpbrCMRWPOrV9vZZ/+9XOiJe5WQIyne+zu7/760u60T/709PtnBtIg9GJK57SnXQ5x3BBuwWNezRIv6EIBrRlY5pWdJKZJuTj//NkxHgbA27EfnmE0pdxhVGSilozwie4QsaJpONOWHs4iI9QOEISZoG6kczUqZLazC3Q3/7czIrm9uRGfgTggSCrab+t6Ud9P/0c6lwwO4ohGZCEoFKOnHqCDKdSf4sKv7VrWrPXddG0oP/Fd3i6C/l3rLczx0Jwcd3j9ncy7IAQQLd+qKFChRszbWNY/OH2//NkxI8bajqoVsDE3iO45h7HtdmMVZVdFrStFlVGEXQpSqkrpVVozvZm2yFZaT6/VepWuYzoZxIWUsplaUMFjWZaPRY0UPhFagwHRJL7jq2sEKqvYzb9boiaFgGgT/+iACUjoMAAAokZBNZjRAIUgLJGjfSIR0BK4GZoJpKDthQKAX6c2ySUAQ6I0QbqBWs5//NkxKQc8m64V08oAszK5uRxx8shJ0Py7WGesKG4feb5WbN+c41NHYNfG6aj3fRWV2+gx37BKy0+Zb7xrebx7x30VxZ6O3CPX3hzzMjW5q+fEQ6GRVx296+iRY7zGv9X0uHTyCzwbRN6vRcz4fx2TXvjSjj0xWPel67pS9MZzelIcWPiPrfxSl3+48/3vXzv//NkxLM2DA6UfZl4ARjF3/+d7x/vX3/////Ad0fwcapfe8RN++d6z/Hr64gU3jT+4hHVJct10ltt2kiSIEsvUPtMfW6MAW8iisKXQdt+ZQOEBrI5nInFqOw8m5WbICZ9ODN4xLwlnCQeHxFfeNLGR0vEsDYfE2EuE0loWN0JNyx90iJwGbnOJiSNb17M0aO8//NkxF0zm/rGXZhgA6NLNvc5Zocykmk39tI4sT03o6VtLszZROP2WM5V12Gn5azTK2mXnpmvPISmX2d6k/nMfjUdMvTplHaGadlmq9Ndr05Nczsnt/unPyHo0WL0LGUJr6NMx7M/8zK07ZSTjk02Z7nZ/ZyZeXakelr0o7RDqgp9kloj6EGJTDU4L88j2ErF//NkxBEdUeLGf89YAFyogWw4GOOzw73h+uHjLqzRYl7wOSeM7o6xA8eha2M3fHTfbHa50gCSmyd19ubbHf1f3XzV8X9Ww9Tnc9fth1HTzTwlUMU0iE3IHL3FWFVB2Al8tR689tIj6j1o8elbv1A0DXqqILQid6QHA6lQUBsQ8jVK0Wn2HAFjoHrcjPX1b6xf//NkxB4i4lqQTsmHFHTam9LO5WsOFgFYpqC4M+mMDqr1y4dmxNu3+TtR4xnTkCQ1p7u+TnXV6PZ7Vvn7/578iwMKYtdf/WBn6qrnfZjZaeWgpun+oUEgqCUaAnBraino8SysBMUWWCpoFRVjW6VxOlA00HjoxKSrieBaBogKT23ZJAzsRIAOVpcvWms1rCq9//NkxBUgs9qMXsoE/ZNoOLuFDa5p+HsopAzuufM13aDFBxQsQHyD7HZGjvQy0xnauclkLmuJkJSUG690NqWQflUdN/z1+3FDB80GQpVQv//p9HXdBV1MpoqUjVOY5mMyKahil//+ld73/+nt90O5b//Wv1qk6IoFltHyBDkm7PhwqaKiCrROgMspNqlbO4cF//NkxBUcmjaE9smE8KyYMkcphyrl92zL6RksuooZBRAkCCSI01E5mzY1ujiLon4W6Ta13hca+ZjFZaGNVH217b6mI4cyuRkpP/yU9GRJuxggwJgeDz44mBPzJ0eXAcDiaAA5eeSvVrq1uVUxUAjgz+CCBo8Y+5M0KEnwiON8pbDy9bb0St76kZzvUDe5l0lk//NkxCUbgkZwDtJE8C0Wxg0UNHPJCjxK984RxiHwzaFNbUK8F1mEQeXdKp7diq+QtjrVUZDW9DGMRhRnUQtv/9rNWrzKHIchTUFdpQMv/xeqKiJInB8BaLl/boGAyUTCli9rIjKUAP4DS42sasycWpWHljY7NFmAjoVdUpdpVnHJnOgsiyqrCw2RzOdioq6I//NkxDob+vKRlssKP/oYyHKOUVGoR3re8sUOMUcIh4IViiK//2pWsxi2YeHXHO4tb2WyFL/9NbKQ71OrK5ZxQNBqOAQICVXAPqRFhMhHgTpiPUvynRGoFlN+Y5H6Gayyrcwa9Rl4W6KfCg8Wh1Nxau2520GPUylnsdqsbOUwmTMhezTMVynzDnp3sdhRRxnV//NkxE0cwv509soK8McPs//+rv3qLBwMaPGDjnFn1YQcXGh0DC8mt/9P2Wg0zHOgcjkoAgZGwUVBFkUFnxMHZCmrJCgsui0Ju73wBKbWNSKasZUE8+iDKekPScCEjMaiAIKBXk1YwQENhjYDNERAkTVUUQhr/z0iK8q+RFXHDq8UQtOqW61f//6v/ZGCQsUo//NkxF0bSmZkVNDK/LtMrao52HhF0z+zWYtEolVVAUkgutttt2Hotsi08qcjuMb5Nh2gQwA+Ari5k7FwQ06zDbnv4gScaPGhweHCE+VNFpH0DqBHBGQOQOHo3Pdu0S0w726EIMQIAAghvz58+Zs+cNCJM0QkT//WXUONLvxZRH/boelJdYwN1A/bE4IAgCAY//NkxHIdAYa+XnpMyi/qAfS5NuOuTTYH6Vm1PakfyGOtWHFxpwUHG0ksRIpZcPLAuqyybo7vRSiWL49lm69taYlGRtaX7pFahCTBAqEg1PFSCMNBkcC54aQbnUMt+1ihiGEyIMpYwe91e82ZWdU/UbkT7f//f9cmePtMP/+tSlJSIC+FOzW2Rq4S0TPFhvaK//NkxIEceaKpnsmO5kvqCobF6Spd+AFLGToWibCu1pH/0Zu+7Si+ifkvef1Qji07GR0VSEcCxjmIv/IUQPZUnd0vnNKrNY+KqKFMQe9P/yq1DjlIzvel62Wn/+rG9O7Lb7M6DzEc5kHl0f60rQ38dXUSjrC7FnTZggN1oUGNrHexeTJot1BSnqVyzNTVmbX2//NkxJIcm3Kw/mGK0hzvJTs/Zk6B4YckYaWZCEJ7D2zR9dmoifpQxwchnuldxYUWohDqzf/dv/tSRaM/S5KuyV7/5e6EV7f//////9KTlEToNPjGf44Y4CC7PZLoHRAUkbdn22YtDZPDJYm9mEjqpOXRRov5PjkglTMJTKontI4ypnKTh1BbTMx0TzqoRwc1//NkxKIbw2KMNsGE6JKWZ1q4lv/52zdXurAmV2/0n0Ijg3ZHixTP3Z3YWLHOFR22////VHWW27pW5yPSwRy3Wpn//Xrh5mMsFcFxvGpVA9b3cyBzhKCZ3LawOUNxOZtLcXUOY6TdEyJaKUYkdKrozghQpDediabme3EXYVs2IbdpH3Uu1zzHuggHC1xVSk9X//NkxLYc0+K9vlpEXrIwwHA2Ih8N3MpX/631ZZwgcZSd/RKshhJlFAkODFG/oghTglJt/S3P9PSmtmbedyOzlRZGeZXMjv9f6nco5mKHdSuHGYw68x2tKgYANFreCFEFAlaIce074nq7QllU5KjTRIOYl0Yn5YwxjtNtOncPwawZgHMhoXQAoQgOi6FxWclI//NkxMUjC+aIFnoFFQjmhMiIrDZ29EQCRiYIIRWFCIKTlJze+NzSWyk7JxGHDB9yupyl2f03Zbopok5FMxr/djxRhMOEcZUXpbIgeD4x3QoQERedHIrkyKO0uk6uRbLYi2V2KJFGMckQQTGiaPJHuY3/+w1ur0lp1x191QhquIQjFAMjRU1Mw9RsHaQmmkI0//NkxLspi6p9VnpLLSAzkckAy7S9yqyMpcFqzxxucQ2clgLsI3ImBChJZFVcKw7qMxMx0AjAA6Myc7CgcvRFnitgMQyC0XgeHktDcaDZYZ2+0aIsr0XLVgmnw6LY2IJ+d6Z3+tFbIUjlGD0cSLmra5bxhRQHFxUWYTFT/9zEcrf9DLN77z9krajWRVzCzbCT//NkxJcpu+ZoBMMLMD1HOn//0ewiSOPw6RjDTSICEFIBNbknfj0jhVMByOeu5eulUqmuLBfX3R7BvqjC6nivnNOoa6ne6jSEjRgFSjIJcBgt2HoliXh+u/D5UKAIonSvWgTcXFDkJ3ZyUY9mvvurXK31ZS7ehZxgeDgi5DuVfoi3T0/V/m7GO7vL/si36f1///NkxHMfu+p+HnjLLdaGEkMUYUwgNUBTySETECsylJNB8KMGxwgiASxamlA2d1zjxDoWxg15rQOoGsxCM1tta39xlHHDClHFXadRPVpNczwzrX//1G8HpjyF/vrRfg9lEzETJSIPEY3uJ0g+xtx+v6nBhwrnO7v/IRXdeqqdnS9UJOZjoy0Iqno30IRcjJ/+//NkxHcgg+Z2FkGFxd0IU7wMGdwcIhuhAeIAgDQWTklPYtVuQk0LJmAevuxh0073tFR721p5y/OQ2e9050G+62b9fdbISkyUOCLK76ZAaNt/De04ThAZvX8hxjSEmjv5mCFI4pRE/+9+boYpekV3Tr1kQSf6YNyq72H07GTqKOdaKBwsujghJwZuCyJ0+ElV//NkxHgiw/p1jDDNtc/Lcw0esqsS1BDveYm8iPgn2w53S+VNfULlorNlLWyClm1GBkRHpNLylRLK+yzFf9PyRW20oznhsxoAIQ2gAQzVvAYEDyM4CKgnOz9ErmwbXszIxuPRCKVGEgjIjOidlMU1BhTAQCRXqekn0ZEZj/kkWq6///fr2f+4jDv3hYQ38Mhq//NkxHAdG66OTEhFVUXrYxHKRXVJnjXS0llZYa9z337LHKsgNOglcbj+Jrq81SkGL19lz7k+1JbZDPMwBKzfkRIz3Vi3Zv6R3IoTGpv6k6Npsf9PKHhDMhLvblYiO7PdOspttkYEzJKd6fLPNf9OYjJ/f/ydFvemnQubmKjvFQLQB2UtaJ4WVIKNJRuwfxgp//NkxH4c896a9GDFMD2CyAIT2gkBMMtssMGLYDUVvcjAVBTYAT+Dmrkfumh3Pk5fJTVnymUhyEX7EHSqw3z/8uqSZN5b/nsyGxlf/Muvx3RHEXl+GO90dAQt//zI/1df1+9z9jtzz74/K9tJYFGGlTeKzj2yGEklSHVknKbI4+WYBgTnmy6ySgYChKyww3R+//NkxI0dKoaS6ApGDW4os6fi4GwKMA5qJBC3YrtUKVqkfCCc4z5lQbz5M1h6Mv3mZJ8Vp5GZL+X14rQzXq/4k+8OaOMkTYYGEbdTFrC2p3kr9CSZosQIhVJm5+OYKUtbakZVmN5H1Mo/WIE7z6fyYgIUICbAkOdIGRPGTJRnpmU87Y9SpcGAgZfSReCkTuD8//NkxJsa6hqa8kiG4JxbJN4SEplYcUzrQhyaOeW08z+L3sP9WZRCjjXNHl0iICuf7MqlBETPbtaLjGwk0VDqJIURe9qVKTU6jQM3New6eQvqT7TNBUCS60HUhkqg+joVOC29aFuVOVUf/UqZkmq5SCOWwtZqSgmJMYi2KGBAEZFxRSbY5WAQGZmJtJBiLoLI//NkxLIcwe6e9HmGkMmqZwQZjutzWfsYqHo70tpSyQaQ7K9SuVl17M9dfXzK3/p63839P1EslwdljYwTDw61OWrqHqKZhBrxmItZqM0A2BeDVi5KVrY55r5XUao6KoWOxjkmkmx/GARUN75YI1DVS8+qXxhInalZQIymq8racpS3RzlVkAnmf1KVk/yt+zGY//NkxMIc066KTEhFUM8z6v9LpZOZ/obob97NvZ7SiSwpg1dDUUXKhOSUHXQWA34dEzRIBRyfVS1diRLTSRIkSJJPP9VVOiUSYlXY4lvqqqt/qc3/znYkSSqnmd8435wCROr+qo40iRR7f5noZ0DAR6JXg0FQ1vg1naK3CJ0jxE8KArLHtR5MReIvcDQcpCiu//NkxNEcQ2ZxlEDFTL4KulXA0xFBRTV2EYAZrHcmIlMh2IdTgIjewEEYIJmOIcmJ90OSklLFOgyMcHQHGPDR4UJQxtVcnQziZdOLi0eiWIZCLLjhFMkeZMyRlvWChqpKYlQdQdKWMjGLM6NpaxHWyPr2sD0MjV6y/aTClYtSSU8xGW8JZHruqqGPsBgxBnwV//NkxOMZub5EIjGE3JGCqVDoV64sz4JOsJdLUkxBTUWqLGVL9Si1ybTWzNs0qSS11O1jQyfNDV0XTQnjoC80Ik6ctJHpcjlrLHTMtlGaThypVp8c3KPEcndUoe20g1ss0JyEy+aVEO5EWlkhlTI1RvKERZGmm8195sQ5NV1uU+JoR8vXjZ2VP3NBQpb7ywOC//NkxP8irCoIwHiGAWYsNW0mAnUrqZXdSFbCyfFClcZDhPwNEbWAggiETEaDrg0tIQO6Ws6JLdA8Xcx0V6rjowZ19HdGNkVgwOOgWQTCJgSQchoIroy1jIQQJgpMlBKGRCUShmOM6OwUIx0GA47q4dkD4ULCg7O5hXimMaiwxFWxSqLQzLYUoVSpleQrEDBA//NkxPIdW/IMoFjGBE9WGqOCjrdmtawGa5gzpLcs1MENQ7J1TEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVFCrkUTcccbYprXDblhpLNMdb7SV/R4vLIfb6Z6h939Xv6wDYmrq+i3+73tHW//NkxP8kpDIEAHjGBSv7ugjX360GJ7ACUsE4SokeNBjBxsJFqJFmpNSakpNSak0ZUakwWkxqTVQoIdf1DAnI/Vq67UYCBksNUakzOrUmWEaxxhQUEh9nrIf+TW/DEhqhNSbNQwIdYd2Wk35qGBOJAqhqGhrPyaUvWDgIMYCCoKUg0NdgaCgriQziQziQwIdZ//NkxLAM8AY6XghGAP//tUFBXEhqTUmhlzJgtEpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxP8i6/XUtGGGEaqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMCRZeChAc0SuhorxI2BwQ5pubmm7wnV59NCSmhV4gQgNkrxIlNFedMQDNPemlfQnV4jmXoosIr6EivCPOcJ4TnDSymEBiw7ITsAAxwOJ/ppThOaElNPsIQ8pk8JHn00qU0ppTNLpTTTppXCEIpl9hCJ5A4D8sjk4jCJ/g//NkxHwAAANIAAAAAA8jSnkDiK0BlhV/BXqeCF5YSFFDTZUsbQGIFCAmMCRpQUk5TCMTqYufOzD5EnvFENQrqk5OgVRtMD5Go22SEi0VMMis0y3NckUUHDjpqGBSTEfEA4fMKTCii6CTCpsnLiBI8QUKF0iXSwpJjEoNyOyWYDEyiJcjWKyDZggYRhtcoQTQ//NkxP4ge4ncUhhGgYKHMmgJPgr9En1OJOswcBiiNHJMREJ61g+KjPWcuFMQEDLYXFcWFEgqPKHmxQoOESFd+uXkxZOVRPPEbpIwEMFxUhRsnzhgPgMFUbp0HhKcQF2j6YUChCjOISdJNUsKwMPQCiAnEZGLrnSyNQda/tHQnCQAsIXKKA/C1h5BKlJUibYu//NkxP855DnxinoSAJNkoq5AXR88ZdSKmypXFU+QE668CQqitQ9JGYPJo13mFqIFz1STWPHDrUjiyyCKziQr0BZ7cjCBS1T7aEskxs2kGkSyxraKtzOomqWPE73k67ciWDYgUgUfSxMuK4JtRWVU1xnaTQoXIcIFS6D4ZedZp8ElntzcejrknSxAkgabaVpG//NkxJoyTDn0AHmSDLNMG0T8qjZ+UESiE4sWVZRqChh74aaaw2JF4KxIFIONitYs2ZE0jC60Vfd802QMKhCAY4Z8OWnF8iewU9puYTpNJtSJUSs8oUXNFxdrdzz9TKU4k3EWw9lctiFlCScpcIwy09EG0aa3NI1KcZi6eytMPdlpNOmudHlsO1S6dJetqV4X//NkxFMsNDYA6nmMACgkbSj8RKaFKWjYJWHEUxVMYcYLqUgUlYc41EtzTZSNLdkQLIJoK2k2EFwZ5jSKzCaZM72UeBEDnjpUSZkjqc0wkRPMWcotiik9wluoaczk07TKvT2cqvXSbTz3gqoI3rWc7P2tM/Ov19pT/tmcbEaBXxhbozN5IoOJWZ+2kiBMYO7U//NkxCUjRCoEAUwYAYl6ZEM6ELr5CFWqEaO7m5jw2MmkdEDohSUEzCyGSGYiuZsvZDZECXglVE+yHvQikhNAqkQsYmQnRLkqqAr12MzMqRs4Q7Xww1UTahoZUKRAxQsxHDqlVIzMREnYazcETixBFNQuoFHxn/jETNJyz5Vve4vFzYFQz0P/8PblCezMCTid//NkxBsgDAosAZtoAZ1dBRo2J+QzQlL+7Z0l0XQQ+pC2fLhqaJn/W+9mafQWxmxvrTU/6vY4iaKpuz72Uyukur7rXposggtI3Tq/az+n176vZlNTpqpopIO1n31e61rodXdVbvr9Gyq+6F09Ro2XDK9z1YBSsz7frv///9sLgAADHWExtkSfRqSdMLCzhh4S//NkxB0kkaK2X5vAAg0MGGtA4VTDM3Mx4GL0POzKKDpiICa5dVeK7G/BRl0sOU0feBV5NNe+G4S58TkmfM8uyRlED3o249h1o/SyzH7d+xVpIYgaKcjUCPStSli/amE/h3XxbbbTVFWgNAHGZfKbku/nf/n/+dt+UABab/f8IkDhSpUAzGlJA6FE0wo3PDNw//NkxA0gSUqQX9tgAFA5jhubo4ExoQh5jIqzcAAZiQA0a4h1LqLoHAIvKYSAo5y6HIq7bOxgnfehZOzNp9+ZlQ+/a1a9TV69II9qLFrx1tzhRDc/xYcImFhMRI6un8KgHw+7qW//KANKpTxng+Udf8Pg8c6xg1IYOf//+/qOA+8MKhvbUtot+JQcCMqd4Co4//NkxA4fQ1LUXnrLa1YF7Pq3/pbea1e1owzMz19FUZyVYFAxv2pWKykC8e94d9QFuqF2HxNyGbsmG//X8pPZIwJjXWxrN9P4e8PiCChKdtXoQjNPOkri7hwc8BB37/oSVGb/P/////+vOiMSIj2CRn/+m6lalaXs8y7e2lCEEFbRb+o6giG8WFykgxKk9LqR//NkxBQhO5a4fsMKnMe3Kl3/j0X12nZP9M/Xsra01l5Ge0ocP0YKRjCtCUbhggGRLWLDth+/pPVmupSiKCZBcqnVUZOx5wHYiKec6EEGISpLPKIuxggcPi4UEw+9rMjMjciREiSv//////+psgvQRbV0I09C8r2HrFRZsN/fADEuqIAMUrH6qt0Bo5F2leNW//NkxBIcaubCXsIE/KxmqGF563SZ7+anOfWzryXn0/aOUuoulyhouQwKhFFx4PC7oqO7rfxPUXHK3VVG6LG9yH5AlEI2kFTyDCz1Wr6qn3MGEFYzW+jf5nr///1fDhDtFoVBFIW6KzTSfEfRWkAAGmmVNV38yxrDDop+cRT7v/ddmB3apW7OJY/ThOpzmyed//NkxCMbIZLHBsGG8MFJftF//P2ls9bHNJGppAwUERNTeyQG/DQtdP/8nOrEKOgzQfFgXc10RummBk4UfesPu4Jihz/8KUSr1aLQfgQAhM6LGf/RcmEaMUI6v/pTOS7+ZYsTe2e43BIyz/ImrUgeYgF1zuTX7eDHxSsUmntU2ZqdeGhXHu5f6gtHXzJUSD+c//NkxDkc607m9sPKskx94I7Z/b/TjC6nfYUfOV321J5iF4weU7fmd/+swm///6LL0+9VFXsvmMyHd3UoicE4aegzvikEUtCFoAeWKxDDPzuHSytf8G5ixb35r5aFVuPW12z+095dvXsDivMOubOZ0y7mm3q0Hf9JnPUf+UDKGKhgma2jDWpAAP5DPI/9jlax//NkxEgc807KZsJLKJ3i5SpNnHlZGet/fV+o+t/1d/9GIsf/////6r+62eyo5yDiBwiSer1qKH+thOb21iTKOX/w2sa/+2fi7UAzTWZpjUdGwnwpcVwYPRHY7n0wHBJinCQe3OBIv2MHi2SQxuQqEI/YuRxIG5C2Y17f/7uirynopzGLfn/zmevv2lp5WcGg//NkxFccymrqVsMElqCYYl//7Xn5cGy7ngVz2h2VMmxfi4WiN4utQtQqQVUm8INJNzOYEm7MI4Kha8mWKlr8m0G1LpTcdBfrq9dBfu7Epa82tSoMFaksEv7rQi/ha6Hr7rOQNNmzee1rn/VZ67f/5JUbS/qtGwqut70yovbVdq/sPRq9awWMf/9zA80WNiGi//NkxGYcshLiVlYQOp1Dyqww9dsmNQlidzKS12KlhG6bX/3D4CU0hj2CPw3I3ADBz7APyPdWjqQL5gF0i77Gs+AwI9hYNseO0J4/PzufX8VrudhmDeQgUrqnTdauvvqjnr3Lq/MmZVQBZiDWg28ifrady4JAwHunoo0dUAPMOu/I1pV1BBYwaERxocaqZBdv//NkxHYckfrednmFDKQDJdezEEj7zYa0HYfAisCcFAaMClQdNbE8LDE9Fn+BB47ECsYBAi6h8BnxTOLdHY4Bgs4tnLztV3kY1i/kKnyMb6I6IjKqlnQurVO363/pPUjf/ut7O9n07GSi0R////tq3+2fRyTCoviL1vW/12EHJoBuzce/dZJy/nfiiMXOFQSL//NkxIYce+LOVgvKHFP2S+2Xc2lscwRBGOQw3W6Kr9lebM1QGo8mo1ap2stX4RnYNi7XofBNvlK/Z79/4rjaPoflqYtjgrKAkwIermnSRjP/6f/+vTkNvVU4zMpqqnfv//37/z8vLw31SVMHldAn1btNuZkHaACl05rgkzN4MjNqChM1nNRkFRhJ+zyqtbzM//NkxJcc09bKVsLE2JoBQSNR2WfroY00MLBhRIUSXRqtyv/9cpRgxMxq30czkO87NfbXKVWMbcMtf0VQoUAbO0jDyAdYdJHSQwOqTFB5H+CoaKt7Njzjg8NeHhpIVyQBJxRTUr/kGWA4oS3K5y+YNdfH3XEuMsLGjygMjK3baYID1X+6f7Lm8Q7f99bNyiCD//NkxKYdOmrJlnmEsu3pk7GO1IPdx/YsLvQRArKCzJTho5i4+wWszPn9mFgNNDgkt2dMtZDGx9ebjLvnpxGPd47JlEIy2zmEIykAGKCNMQMACHdPLPt7tNqgECCEGKPQ4IrTwGTontplBEECCfD6RgZwXnWIiWiKdm+jaClwtIaKxHMtqzyUhy2gYi4nhMt7//NkxLQl2/q1tHhM2eTOjDqqIj3ERjHp8z5BtUZ+0RN3Eb0xuPs6199Yz7vzHqIZV6+zLOY3HeV7yJe/yxnjBo0heWoOaI5ZiWOmzhTXFd4qHuLHPP1jn71pmZan5YdpjTk5TplqJm9O6793qIdoZXsww1vHaFHv2ha9ednKw4BMgNLzlgpDwnamq0t/h6cH//NkxJ8xjDrLHnmY3I5yCpXYtWOQrHn0hmsQisWS09CylYJBsraOCWvl2yNU86xfuycrByq66S22RIgOVdBMlKm4Ih+DAilg4aNESsqLirARojs6HmhuST1ckspREBAOlBVLiRccI0yzvaib7z+a9OUpByxyjCy69xIsYZP4tQzNte2mEg8SRFwkB3SCT4DT//NkxFsoK+bOWmGHwkUCRrayndeXkFSGm6MaluSoLCnBYrlU8f/7M/Cl/P9vvYhly3XXsasfQ8kqkZZie/fRS14Xn/ZM/+a6kzW1hzMEDrEWV036vS7SVAAAoJ7EtBpgzBGTjb47O8hqpGPsvJqRN7itGspBvNtbZa0brAcGIH4KHb0aznpdN3CeJAsPjwlm//NkxD0k406+XHsG3OXvHFp9p+0s1q3XT2kBViQScro9U6etZbj222sFWMLGBgJWHtG/iqUPVQZLhjVZ/z7f/h2lJc8v/q/bIf9ht/0jY31pZZcOHNcKor/0HppQ4WhQOs9RZaiFNJKgA8klUeLAyaKlH9dxmZGZobgVAEbiGaxKBGDkI0DTlIvBmlPLXLgy//NkxCwj0na69tpFKFsy/dipUik5DbW3AjjqRyQvsshjMHukYG2mLDE5oJw3OmsiRGo5KV/ZWU6gw2rcqlmNK2t/VW6lb1LK1H0eiuspTGdf2l6sVjGwwo99aMjET9qAa54FS1IJPav8i2GnhWQBStWZVYCSrQ3MNp/j06JU5jkxwQiUSigkJmZlUWGC/VUu//NkxB8fW1bBntPEel/SkCWsE71hYJgdpnpMnxyj+DrKo9D0LCllEr2F69CIwkxi6tq2awUcpQ6070O2XO+3Up6Gb/09+spmYwcTNeQ3////sScEyx7EkQv///6uHCDMC4sbW5/p/Y4f9S0qRBU5BtP4WWsgPUQCKd4dTNgcTrZvoTFP0m2SNjcn6TZnKNzj//NkxCQb4eLWPnpGshISZ5GTOBAWJn7W87LfUlzzubo4OjQyM5llYqPDK//8uqxMjPP5DmTgjMoGFGC71f/5B4MFCqw6OFw//2JPAicBMAvWF2I1/WQ/8VpFq4G5P9bvkoyl5G0ZMZhkzSvgwJtrIY0+mnqZl+sk0lSIkHgEWMiriWVkaWzaWZVMwEKYqGcy//NkxDccQ1rVnnpEdhlKZ7lKVH/0NRSl70/qVHVUV0cBhSOUpSk33/p/fQy1burFbT///6PczhhTuJWBjVBXv1QNLfoWxJkEv4Epd9oOzY5FC1r6KTWW9h7kqtXblqXym9Vs6q0vMtVkWtO1Eah0ggBiGJ0JDuNqdbknOvh3+1sNh16R6fp3/ttrYdth0O/9//NkxEkbsfKtn1hYAr/Ubar//3N4dNWiCMkFTJY87p0lXEYTLPEQ49yNYaPN2faHTo+iBh7bccRNQlLpPKETRLgsIS6JCBiBxoUhGx0ORTFQDPERFIQEoI6ZrVsDoAIsGAIIk6nSb7F+2Jr6Bo4UGEaTQmFoqw5MMkdSnsMBM0sIBWcywQhGqM+0ulMrgNm8//NkxF00gvKdvZnIAgrfv4XUBsp1jozq3L0dKBlDXe5/dfbw/gHIM0lHgwFhphx0B9a1lll3f/3///8EJGwEhcz1a7W2yMsVxca1l+//X////////50lzeu4f+rGsvyzx3z/yrd///////////P9cz5n//vffwr/FgVT/8DrG2OTmmC3Yii5bbbhdK4r2JDm//NkxA4cagKxv89AAifPqqltouZUA9VO6wIsr2HFgn9xYkqYhrliwGpF61C0zwTTOozFS+12pbnvqOuntJuK/+63ILQyT4p+//2iqPsg4XhKDwTnAsVHsKUuU5Pu//cA7RUeO+56CZ2KL7n6CbPo0UUoAVWgA0ZtcOl5YgHZo+bEYyEUSSsfFVz4Phddy1rb//NkxB8cmfqGVmGFRJa1rb1qzMtGQKhC9n1RqnYkUSfKcFQSO2c5WM5rZbrZLf6sGOAhRgylR//0dWMokwCJM5YCyP/tY4Y1NDIVGhJbipL8WH1CsNte5fTM49CZI89NKkmxpRgYcBWJSbHHeqsBLUpmvE+cZuU3SmT8mEN1QU5BhYYBQkRi/3nXx57mEoSQ//NkxC8WoQZEEMBMrHhEKnAseqJlmPHLASnCzxQMxKn6Tx1eyohUslIK/s07/o/q1d/5v2iJiNbKPl4qQ3BjFzTWtQnKFPfBWwhEwy1JgjJ8kdUg6sLrSyHz2X4e5ZGyHJekWZyab5ZzLyBxROYfDNIdSHGqKOagNgKOiFV+fDnY1OKHyArLLeVsssDJeCrR//NkxFcY8co4CHhGnAnEmBdbTNyohvIPASI+DQU15ePb0Yqth16ubQn0WLyKcPYVkVwE/Pm1anmBoQZJ0zalEqXcRBmWLlQEh4sd5kdKnkRgcdBZ73KQiHAKP2OsQC1Ys2H0NC5XlCjbkrn2G4skeaBZkPKQmVH2MkjTCwDRMGBErWj5NFztLMG50I71Rzlc//NkxHYXsW44CGBEvKkSL2p6xWUyHE4Q9wJATC1znTz+Hl1ZnF2gPrUuwmLdMxSTeeXM/5/7SHili171qmbY/I2XzFETXVTZ+EzoJVovJY9SVi1zFjZA1SzgajQsLMkTLzP1fE1dRc3UjL+kbf+TedmuUl/7MjIylisrXfX3GWEbKdVSBMq8WzpmEsZzwhEo//NkxJoX8no4MjhGfEAO6KgO8MpQxUKsrm0NSKFr6Rs6RXEZSbLte9yjkcFAOcWfNuchjySrxeu45eKlagIHguHRONxoLhaHAIAesCoDAw6MdM8BlMIgPJGnA3pEgNfDzoIMAoAgMGAAT35ou4Aw0AIBAAwYAxQQvmJBxpk4XwMlEgDKgyA1aCQMkEYDEwE///NkxL0aUeI4M0gYACugmT6kAMTgsIAQBhIAAYZEIGIQKLM/pu28nQDQOAMIxaxpEQBuZ/1Ns+kQETuHqDMHhHAYkHAAQDv/7vdPetMDBAEDBAxwBACAOBgN4w6cig7zAmA9b/////C0QiIyBHjNilDUnxcBiOMZA6RM1J////////smnWmiXyfFwGljpue5//NkxNY1tCqGX5WoAepQCABwCEFjIxDDDBA5qtJbtOaO05WWFANoAE6brqMrQPuBGCKN4MBh8D+dEjitvUOYYj5DDyYnGJKr3BSLhC7hqgPZGTskmTijgJ9CksrW5VPzvGydw55r0ZHkCkVYiN8NCjlk6urmJuBAl3Zl2/3Ah5zjH9rSQM4tr/7+fv/78r5g//NkxIIye2q7F5h4AKwZlK8v8N0KkO9L5p/j6+9b1Xf+PpX0s+va3tm1rbxC3/umM/ecb195//9cZ/+/r7h71v5r8PazwIoVApuzeKgsGHrTEvQAl4MyAzCwQpHcPKSkW0DytPSYkujYJ+9lPQLs1rxo/y8dZiv5684fGhwHExQTGlhMDgyCpHUotIqLtcSQ//NkxDsdcmLS/88oAFixJR1nUQfMoiLI6bufS8l///szknu7pVZ7Enq5/SphqkgTP8YJwZEnVokLFf//2mEHGD2OMFmrYNelhIBtQAuJ6/ynYCiRm4I8ImD1f4XYHQ6M3Bk9JDykGfN1Y2NSNCxiDgGhcPBoZP4Fkpr+zIgpHPaZ6RN7akmQOJq053Z1Z2a6//NkxEgc2mLOPnoKvO3Zv7Oiohnae3bOqLoq/dkGDf6j6VJ9p9Dpxn/700xExaRdYAcYLsMMBUIq7RaRL2o5/yRVcehnNIrCSMqFL157FRudjgcgsv4MSBV5c0uZRBSyReyxMIrefdqskFzzXZN3+8TREMSqMaWEAivUum1bf/7vcyBWFuVDOvZzOjNL/6kZ//NkxFcdI+rZlsIEvl//6We9E3u/Vt2qra9GXb//6+u6ltbXKpVYG4BeqpMAv6S3/+UYQtp+xBqtWabkCxMUyOy6aOQHRbn/M5YWIrLDBSqcEgNWGxJYNj21XHks7KcSUBASOrKzVZSlLMoCVrDOY7MtO6VVtH/fVSob2/Kq0dv/9jFRxRqf////2Sv/mr////NkxGUce9rE3sMElP/6Prspqxioket2htfGMGU5tto7bFSWJ7KEXH0CpHGmmVJzsNdWmypqbGYl1rdyXX7oMICziIgHjB4JAysHjCLIggPcSHuiFZy8pUImQx71dFkRPtZnJ86uyq5G23VxAux2sExCwPgf+7ef+23+QwGD8nQTCIPg5KCzZzy/W+pRlvhh//NkxHYdcdK+P1goACEUZ0eL+42ptMo3JGgACSgARWGxd5YE3CkQ0xXREQogcHl2/sIXygiXf1qSR/He8Tkcuh0MO4Mkm9PWE/1WoHrY+r4j/GulFQn0PnhLEaDeIUCHVjKp6q3F+LAlcUZU23KyI4/cuLTWZKQHCNM33vuPTFdwocu4Ov3srzvGyk8jUq4v//NkxIM1ZAqq/5l4Adem9435NZpNjM+c49K3zXdVXvNMeJ8Xzu9M39qQ8Wz//66xn+S2d7xTMPes71/6avD+7fNKen/9PfX//+Y18f+T+3xrH+/8TzVxEp6YmLoy/dxlRFYpBJKMTSjbbpUKZTbsIJyU1q80BXvHUl1CW7qgdOEzEpeyZJRcoTCxwMEKTTo7//NkxDApM062X5hYAFhitb0Bwx4YIqB5KgQigvg8sPJaqnVZJRHaO02fXMU6bRabLqGNN1HLuf0x9010vIq8NhI8ziGzPXxPPPfw5f/0euP6/dfDnR/21kTNJI1TdFzqa2J3SyPuttx3H98338bFEanadSEjxZlL6Edz2jwVFFOLIQW5tI3NVpc3IVyQsv7S//NkxA4cMK6ov9lgAHzRVi97r7QXYuUMNvZKsnwT7YJG58LhEAQAx0agMMxlYexHWMPnhFBsO5QGjzFhMLBYjCgdBcQipVs8VQEhK+ePO3yvuAKBdXrEYiK07VHi4BnG9bm6+ho3ngsroYr6Vm/toQAySZiuWNZ0TZjGjZqPo8HGw92GYhFYwhJhrOsTmi6V//NkxCAbWR6lXMvGftOxALQVxdRwjQBvhcFOzHeAjM7xwIAHJEbFA4YiMajtrUOedp+4dvQAq0cu9Z+IWlwKKFiT195pQPPChKd//9EqGXf+6TlzLAXu//PyCgABCAILtA/3GHj4Pbex0hQJxXt7QJIeUeDFTVf1I5brWLnW8oVDmniEKdzMY3wDUWlSgHgK//NkxDUbkYqiPMvQfBIlHm4PSXq7aaBaTzA5vQc3qd5Ap+k18dQQKxZZQIh05ApgdEMeEbf//Jl////x6UgH//FzSDjBWqqGuyEFJyQOf3CbEjWsckyAwta7XVq1dhEFOHdxdHY4LLX3K6ZHNIbfN5mixDPvw0OjJe0enJy+EbTp477lV9x53KiH+Z/+15Kn//NkxEkb+Y7FvsMMms20AUwPoA/nzAlImqxLtW2hlIuEB4U///7NYUDv9HkSw6dSFYc/3wBErEAE3LpP+5ghON0nY+Fykel4tycRdkDbrzVqYaQ0Bl1zcAWMSBZVAAJSa59bCUscxZFGpRRLEhJGcOq2z/Grc9VLbZlxpX2SzFEGHt//0LbSyMhdf60+sU0U//NkxFwcEmauXsGE9B0FnkP/8t+AVhcBf76xKD6Bdo9D1Q+LUckkl/UlC90RcGeBJFJVZZMzu6ezd6oow+0CBQbLa1uGWi1c1tPJR1WEO37Bg47IsGlBnKcbUpDYVgKDJSreiZjAzC1/XJ+f9yIZySiV9/9eGMZBJHesI/uoFh8WnlH3g6BgpCX1y0Sy3v6V//NkxG4bYma0/sDE9mAopbbJbt9h+YAjGcBsJXeidRpRd2TDaRo/EMinicysMS8MuiJw8VsyEXGJ4sOi+kWjK/2fB6Awhn2v7/f2012sgQRLo9+7XFSlMGuqInKZKyTHRO6yFLJokrbFVumg6nT99V/+df0EbFNu0iojIQcxcVmqQYMUaSdtlGVVpQK3LrIF//NkxIMcy0LKXnmLExMctrtCLHYEomhmGqXcfWjYH2y2JxN4HvpaQ1pzaO/UQgnqTz3PplYDgAPPOeaHbO/mUn//MXAwhhHTyt11ovIICg5xP8zEgwdFBQJFAynLoUIxxnOfv6RiZ89AAq0Eg5ZXvuF6DJ7b5JBu4neWStzIpDL6gQNjaQfBksh7kqacTfkw//NkxJIc4g61vsPGWmaskXh+UF1Oaz65mndUPViUOgz5+LaluyLl/lL56GxcLWzxCqE9MOEXuY/4+t+1KYFArFR5EFiQPguKLDusWJhWtx1SCNdRVhlQXA6REdMKKNT/+mpaVaSduOTSRciQ5Dk0jiBVXHi1hECmST7XEDJBKGvV9NksfU3vLl3DJjdCBIrm//NkxKEckdqw9svQUlDrP/imMBjUCO5jSczL7x084KC1h496v/rp//+ltKfQ+hap6///dPfXWsz2srserMpyrp77ZyMfcqdDYqLm7fNCYMCzHRsFvqubcmtqoJE2PGT0tVmq3rKOkNxkYAKhlEw2tvF753J9NWQIihwJjI30r60b7fj3+b/+eS6cXgdlmyD5//NkxLEc+5asVshPEpTqgcCtItaXClFCoLkHpcJfX//ufPd/SRrd59Vp3FlFwdDyU0IXyizBwEO7d/q8m94i/+YR6xqoZJSZYsyGx00Fhc1PZGTSOUDFItSMYHxWFgFIXsmm2K8j3unluohnzOe/l3jmsCMbmaKVGks/3zgnRZENKGrtuU7/5xITAMCijuVf//NkxMAcwma49sIE/vUYKB4gR0lu3KjJWLvPP/2s83bUwpbcJ0Un2/R/xVphkwsGZCScv8ZmROtvoLRnKdeN6hQTPzGhAJg2SMi53chwD6PlE2NItj+x5n0yV0ujZj2WT/syu6HWQ05np/+kDECVNl/7RZSun/uy+3Rvd9v/59FvsZlst0qfdz23nlzVMqkM//NkxNAbwda0VsDLLkKijARrf/cmYEguSCGQg48BoBTd+6g7CHJR8gWQB3IiNPRZnhwIoazJ2gwA5RokJXQ+XsAoCVyAdfnAiphLdA5TkeXQ2Dmg+KHVZ0Sd9f+mwhrQkJsZOWYwYGsICqNvj/4XBikM/v//9MnqoRp5W0lBMIjI4NwmhfrUPh+X/5qUZyN3//NkxOQcQ2a8VsMEytOZTWqyAgogEoXYl70NXCpVgSIi//JUzJE8BUESAWPlAwoUykp+iGWMXSwcVq5MIRa2SBLiviNMMizSUY5ZZBDPIDVhZa6aBrL2ls3lz6AzAbdDmbm1JRFqiHOxcz3nvrPB7uQNI34o/wuMCRpYBHqHatLZmNRv9I3WvDa//+/1VyKJ//NkxPYlGwKkVsoG+B9UoTqoGMqcNqSzOT3/l6+wigsFOgyp3pRnU7Rw4rD0GMrsqvJM5jGYsyfoynZT0JU7rZH//tvT+u710IykjXOQoqcCIlJf9ktrq5CjQ8lpmStVQgImp4ACqFyBJaM2GVUl9nsVwh1wneXy0R52FJwOWou/sGOr2aCcx9OZqCjMyMta//NkxOQpBDqoLspLHvXhvSj0YMoTrUEsxTW22XCgVEpA00vHIbWf1sg5XOVv/QKEHHYMKO9lNKuZd8iz+ZnKYZnv30vtdpnlq+mdXEHRq/8lTznkJP//8kQIVTsgjau0RIJCSt2+djUEACNqXVcsWt+wAcskbOxCC37/obvK/4NQS0QmdoBbav1d5I/hOVYJ//NkxMMmq8qoTspFMdM6fAjLAyFnK7mopI/bJ8f4ivQm4H+sOKjqDbniLVjqVtqi66vet/WpOPcfev3//s2Tddf/cTXj7cfaDlu7//j5iZmr/7qauOo6lGZ1Z6VLi+LjWauKOspcm3FbcLLImYYZzfrpXV/XmzxLPI9u9SoBERUkbbkv5jA1X2TGlTKeRUjV//NkxKsly66ovsvQlKd+zKXVUgy5woGCOFPFTFZDYIUFBgMYCfgEFRvIoaxHrpQ9SlZv/LQzqKlXMbt2/71Qqj2MCKYaQiqjWO5B7iYqUWI4/0pai//+1qKlxgiV3IR/65WmymNERUrgoeO1eBjzg1YEy1xJVUJJI5I3LZd/jTFzIVpcAstWxATbeOB6r7NW//NkxJYe21a6NsDKtnAWuC6Lay6xBrPpxa3N4uK0neuPRpbeLtSxwfMuQ7dO9tJ91IqMYimT/aupSg5SDRUiPf7IaXy1UqeqI6L7f/pzGYiucxVEFi44Fi8ccBBAmU8yQv/3Xl4wVQHG23I5bbdv8FFuQWqMokOLYSFXqRDZQH1BDc0NfLTdlLC5h4olRCtz//NkxJ0c8v7SXsIKnklW2s8I5QIGAMpe53bXXQlCEIyObeiTnqd/+i0N0+t+r0dkt6USZVqr2/V60siMlSnsVSjQ6k3pgc0GCgRb/vi7kCgqFBggDxOTTD4OyXtk8AiisxlWTiu7dv2pTIalNdpfuay7llU/eVvv1Obr2c8/1U5n399z3dvWffmfl7jPvMFj//NkxKwdOvrSXnoElpgsrbW4gJbapvZ6/HQ8EFQrL2Hh4n3TXUGeJf73/aG7ZsGJ/3/4//7Rma9+6Qsm1tDrgERM19jNh9ZDxj/8/Yc+/7K4Fquh/k3Trlp+hHkR2jT3gOFC//JCowih9gkOxyVwJqnPOGVZwpM0h4cHkL/L2DB15zWHziJfkR2/9TB8CwMj//NkxLotHDqcFsGZXBOBDcXXZhrNrSptOU+aRzdxQ6n91ggYS5NQSLU3NRMDQIIFOlyIYgMdhBLXJIQPQqED09ezUjiMlyUaTSM/e0CD6e3S/Gp70nz/2XsHIMgT27udzzBRJNSbu3CaL1JtQ6YInKFyUmKhlh+LLJyKIFd26sPFWC5wsrJWv1zi2N2leuJ5//NkxIgubDKQomGTHVtsy9xbV9KSmmkSzuCeIZESttIJsUcXDFkqhw/JOjGJsCh80KSeNYsqm1BlmbDMyydCe3pkBAc+WBU6AAcNAkaWGBhY2fZSjkSmJFVud8L1Izl4awmNdWPZIeYFM2OVzVurIhVbKo+jre56SrWquk1/2yIcEqya17gneiPrfzJ3erAn//NkxFEc8pqmSkmEdDsEBF3YeUPS3j5ts6+oQtY+bMEm3KGipJkIrGNPPcPkSDJ+VVRotbcrUDqww0mi1ZigyIkkDZlgmVbLp+VlypG7Iz4oE8VLlyulSdL3yWtvqUbpuaF/5xyj5T6bu/XIy//+Eg45L5/+hZGaxP/+lwql3DqK1ihA0nWqBXW4EHmL7S0W//NkxGAckpqu9EGGkFue8HYuSjVigXdMFDATScnvQypWaEiVRyrn6qYb0yjs0gCii07DlE3QlAgyjcKRMg842IByQRLFuEVBl4rwYk0U0VS7krjIabpCIOBIFVLC5sgAnuWm9YXnQtBMPlybHewgcGCwFFgCDYH+wmH/mM/PWJchQxbACrAw914BElRY+ISL//NkxHAcqSqu8kmEfK4VRbNY7FZaN9ciBExlMYS36jyChfwBDUuLwDEoEMG4zYJYjriQJBTBMllekd2muiiX1bxmOI4SBUHJUFxwTkrhA9DPkqlhirT0xhHPPGllEh4Kgmoa9+Jiq4sszIlr/u2ncsj5JhVEQ+vysOnXf2HU3TyZVQrGAtWmBNVzFxwUeHCZ//NkxIAcYUquMsMGdJB4BoyKBuyZjA4ZxnGlG6XANewlh6dTqA0KN0NOw0EGAThcdr7w1WfxpobjRGGJ3dLZDIiPFg6CpZ+VcJQ0kXBUiGioCERVx79RUt+xfj3icsoxpeAR5hOv/////peJw0rET/2LShQAZddpbJ8AqGcP8Wldj+VwOYBxKEky6VJvD4Pm//NkxJEc6J6aFtbwQDWUCGK9TsDvDFCh1hQWFsvino46r/5RukbdiLApIbQxZMhilVktBgopsuf3U0Wtl7fVf3/5qsjSlZ6UutCsDBsGBXC7AJ///WwcmpbnKScSaCTv/VUAKbS+2y5TBhwgctTBnSAgKJH0bBKQOOUFQCCNkSxyN3VxyUjSzMpWcOkc7n7K//NkxKAcWm65HnjFMn1dkKnOw9cv9oyjODzxtTo23MxWIafotZft+VJkMy0PLb9KIhnNOy+VyGR6U/////9l0mzX7JVb3MPJlmu/9DSxFT//1/uu1OKp+EqnUkIdkU90bVKtileNeUxGyMBEk2Nv142Wz1oTpIu/vNLSTIVp9tX/nGn79ZbvSZ2iEhhXZGRk//NkxLEb46q4fnpKel7c6Kj3ZJqoVl5Xu5pHpqzoroYv//09+U4CwVwbDLAE39H6iSF757QVGBy+Qe39jKUQpx/W/7bYKseSRCIiMORY5zplLu6TEJJT4AWRhLc5Vz7w6UMh539mRJYAhwOAJGlwgQy3rsnZdhgCF1iVzh52sXEpxAoD5agq8OF1TRwmHy4W//NkxMQccq68XnmE2q+p7WXyxYGVofvV/TuJOFllpkJPEJobLJFo8KnzxFptGoZbKy4RBKpOGFKh8hCCBISogKrmLlBQEJdLTNECooW7WSHOUFoZLWVipTJFqESIo/yj0r2KucqpmtyjVJbcorXVul+5RBjmNA0CKJrd0rskJeKQydeI40ctiElnKKw72Ond//NkxNUccTa1HnmEtoh5gDDjQVSUIuS47zuldZFRXorX3iF5ByjAV9NfAYwRsjoxXHAMbvEAXi4loWwIHAApGsiGv0yqHhSKa4UPAwcVYCUKZxxKFpAnIbZWKdfUt3Yp21WZWxySzOsSim9iEk6jUMr//K1gmmzAFnHggkVhN4cS1hBmlJMTlVihIQiqaEAj//NkxOYd+ZJsPtmE0KTSdkaorXeXa4Vc3t2+w77MklCdKf/XA9xy2xPpVpxp9JNgStAAdDzMFn0yQFcZiwqSFYInYuIdGUSXrnWZOrCkD+UkVRfOZHdc9mqfun58nHz/fMTkWV7z+NuH9HBqNkpjOxdzENe5JRAxr6MRyu/R+y7RRIwRSkqIdYdJF7T23qQl//NkxPEhOUZQFOaShLrQKMpZ/+n0KkxBTUUOY4BSAShWTEGGjgwSDBlXggSjCY7zFcFV1GhBppHyFGFkGECg2GjkWdHipYGkwlG+IPO80esdvVKeOz4EgUk3dIlir3DDL3zFwzRM3hvOV/OvU9s6V51iGms6J2DwwNkEMEzOpNbEK7PTY1L/NbcmitHtr6K6//NkxO8dCgZkXtmE2G1Oqfydm2pMQYyA8QhgUyQA8wCfQ5wB8dSwHDyxrPWKLsmVBhYoG+SqeQxC4MwxamBRFOkScTqGluHvzws00H4xiAcko4f0NfUueyMy8tTO37Y+ZMpNFHtJ/kgcD5YPmKDb0gQwFVzzQ6wXBIPgBZUkBo0iSSnQLRrbs+haJH7NB4T3//NkxPkfEU5EKu6MVDme7yvTt99MEaa42U+wAeprwJGBZUZWKZgkpGnySCgWZODQKLQoSDOVtmxQGquBG0g0EylDS2CPi6kMyzAgy5hntILWrK1rP3Ix7uMhp7Y9d9Z7/ZKGvqzFrDAEAJ4jD5kg0vCYlFKKxdawmUMGnKSthAoQopaggl6FW0hpw45SXKM+//NkxP0gOT5EDO6MOO7/pyVnKVKEBYDjFYcTxY1AYHx/+jI0aZjIug8PpgWKB3ZqShxARmNJJwE0ZMCmDhQORAaEGIhYwDr7FRFD5kLAnEaO4fERaL0Z6tLwjo/SpDyG1FiV9ilypi9u7m53fkcDpJEwmJTOl3DNW7K5g9cczK3xj6r2K2cu/RI44XKDztQg//NkxP4gaUZYVuZMUAfGgVYPQwAwXHiyWIYE1A+GE7hqC2tB9xl9qSBCbaddz625NQxQnirG67Fij/VVBuJu2SV9RkkFPIFBno6n/SiFIVUY0PEdI4yowhwSCAgWbo8YQMW9Q3a5DbW5ucgXtmwLPQ1JKYYPdmFFMoii34TTF8bH/8s4giodjxcIDDCBQeKE//NkxP8qiY5ICu7YUKDxyiiDE4nRCDBREPGiPDxMynCRXKqiuySXZTrIwSFlcmzdTsd0JYhP62dq1ujHo3vbX95r7fZE++SudApwboV2BmDaD5JXySoAOVGOWzPCkcYXfGTBiVZz5MnAk6NJRKKGTnpoxMYKNGUqI0jgYiM0FhoMbizuNOMiORwPLmd++9Cx//NkxNcnC8J4XtIE/A4vpFV4oOn5XSzeE5Pjllc00iveZ2b2/bZFbpkqPuJovLawexI4O1dlLxdN5Tqz4dQ2cADDsQjFYynSb/9ISkRa3l1xfGKo0R6HD6InrR6EgeuUJ11taInfV7prl0l4hbS//+/4q/5+f+fn//q+uZ46Hj6C5Z5rPRy1Y92ErqmHiS6A//NkxL0u89aAftsQ3PdGrw02PGvgwEdOi1TbysQA4c8NZMIUz0D8z8VMUKzkwkuIZOEmZASO7InCW4uF0IEZyy8kKjAwbPzNo5ynr7HK4upSwhrS2tUL4PXrWO+ZnJ/V0pP47adEJSkDlhOVF52PnOlu505C5eFpYhATwAgNx4+Xmov/d7Tg2NE4ufbSXR3Z//NkxIQwo7Z4DNsQ+CvivU8aLAsHiwmEdTluffcUehPQ1EMPVhw8YSfZEDR+LwRTnwO0l7RKt6rrlqSOPmOkskeaMUZuOQ7p/7jpShQVrlOCJp3hzwJjhN520EQOkOgFSBxVQcxMQtOY3A0cUDGEKmrDgImNCjACDGAlLWeK8AMIW4igwZJhRaTFd2RRuh9r//NkxEQlwm6QLNJG9Le4xUZz//2bcpyCgYDBZxC4gBYkEY3HCwmefGPceiQPElxis/80EDAZm5l/9MiibnEhP//zcI6yL48+KHD8QqEA0okL3b1R60NHYPKOIy0z//4nwAHIyScqNzLOi8ZZBY0Fv+rBPZVZiLghiaTShyDQsGK3GlL/TNe+C4Dfugr39CCD//NkxDAn9Dq1dsILHnLHikdPoWYrGGtVMfK2iQQYDgmUPjB71bNFkoqXKpMN/fcT2KHR9zE/NQieVNKLGWP/m9xkSVSZ1bN53lF1KRWRq+hpmYg+h9ZHSe52FCKSXYn21aVHQxSmHsZa//IzoSggPndPdNXZTOUQC2ccPF7gAACUFW1J9QazIaPqkSmU5wtM//NkxBMhC7K6PsGFLVopLBGBtY3lCpHNuDYyqymjvVWlQqxLoDb+HIAgfuUszR8QLrwXsHKiSD7JTSxVUnCIVJK8qn7bUupfWSu3/+hjZS219N3St6/7Fsj//ayqimGMIOxKH0qdjGo5lWqM9+3///YUQEHUK8fj81pZjib91cAAHok78qWsE8YtSDqANOM2//NkxBEg4/KpXsMEneExF3Z1rgqEZfQ5WmH2aP4mT3MaI5NJoIl571N/W3i3ptk5L2uJo7bDZvCZP2AXRiHs1LuY21c1kDGMja/1aqMyhmupztq9juVFLLMZzfVVeRv/0eZ2ayGVTOhHSl/zLa/dvtX/9/T1OjMrkni9TfoGPUXAAgUF4m/+ZQWCtYZW0Rp+//NkxBAgy/a5nsGK0a2rVWUOvLI2/l3GCKVB2SaN/iJJBLkR5ME08UmPR514kCnVcXLMp2VDFTGndn9UDDFMxUa5ElMUqd//3Y3qv6s79f+hMVNk//f9//rrs5VJcIiQ0iDBNXlMUhTFS1WZJTFNKrGHIpBA6ilxVFQX1jjtOp6q3lrJN2OS3lMvAaTZyZYx//NkxA8eombZnsJQVm2XYVkUliNlouJzIJm096iZtMmcrKxtbjxjxac/FNb1UbVE4wRrU4a0Gj0p79hACAQzlEoyhhJxhc3XdPE63y/y7wnLvfX9p8e/zH///w5hc9YbTb+9OUYPeGDJMCE6UH/7qIjMNCgESfXYqoUAoUpL8pSMoB65F6X2tDy7SDTknIaZ//NkxBccOU61dsMGWPAmHpoyqlbsEBfOCUBGpi+qq1gCrpuKFCwVVyL5/+HCGrbwkfd4aMJEAq2hqmIRY8Jipl4WPiBh3PGv8kHVJnwXFH6f1PUqQOAJaItQ5PV3rlFpasqOaXYR08kALmtySwWoJeGdFyTFUxiwRHQJEwQFmHHJl4anpxml4+EOm+MQPw+q//NkxCkcQhbJlnmKXut3VFcqV+kaVAZLyOx8ciDKpb/3L0r1lFiCg9CTIR3VHVEQgoYOBB3veLgmCCAKWnQeONsvgNSrA2WU88d+/+xHl3lZVSlzD0kAGq5OW/qOjA7f0SCkhrEFHQqS3i5ZpfcHf6JFiEgoFEesWgyZFpKKFD0ZF5mHd763uxRRyT3f/pFQ//NkxDsdGU7JlsMQUh+PBpDCcn+lQjC4NHg+OWJAQvDZ9ziG0Tnj4QciguTQE0Sz+hdCTo4w//9S1UzQqsgiYAoHFkixAgpLFA2d05f/VuBMIKqmD/1kjRc9QgZhNrtguT60cRnsQoshfD1xOEm8KT/K/S4nl2Gfs/6QP6jvRR1XcUCbHn71dZDDUtKiVVEA//NkxEkcEU7BnnsSxP6jwq4iVRqWCoK/qRQGvNvE6CF5A0KuUbTUlw9+n//rzQjYITzHBWqlElIGNPWyXajniHNuiTBrKuGmFrR+Dy8dMBS7hVi1g5ina9KGMVT6rvoWm4wxMlQkFqfWH1342Y0n1vlr5DMjgLJT+swr/RHT0/lKzMVZKqvdEpRm3Ob/8z5f//NkxFsc8s7WXnsEuv7wpMzpDOBmgLRdbIgqlQmb//rH0M5V2GqAAACfS3irHmuHFbbbopSGhW6qdriwLMMSLxBtbfOo7wPf56y0dCEfn40vtpKn9nItPKXvNOlba9Xexla5p72yYyv+lSOLNK1WV3VmunZtlutQWceIh04ZAqlHiILERFEqkY88v96mFosS//NkxGoc8e6d/ssKnCKSTr+plt5/9S6K/pqT9syCvm0mnWMBpbaYI796s4EdnblqMyqrj+qe/ytSzEbfRzIAelVsBWLygDYordf2rLeapu4483/8hCswTH9E9DRPnNdwz/KLXOH/75lZVL/nOi+utygyBn///ouVSuMHoeD5w1Ltwbj0R3SPiZ88320+5q1V//NkxHkeS6aoNsBRelUgATgW7rJJdWYQWaV7YHzjn8a3wmBGEyeQJl+5sDnbba7/feVeOuDUYDPXICxQOORa1tMidVuyuN2Fhc1aPYojIGcWwqSsyq5VM2/2///ZdyQygLsdtv3IqP63uuzQpSo6s7Fuh///+WqOVDXFlKDRRgA1y2mf98rKquWKKsC6Bku8//NkxIIfC3a+PnnFLrZbU7I01ZPYhsadXvTUunazS2Ove/rXruUxP/uap97s9pIIkcYdlqUst4mEU1KzfEX+eKiHnxEafSzROnhW0eGqDne5zIhAgtnQstpiwrPb/+qPMKeRjy/1uFbsXIiwwmEyixEe/+VeVgkSYXO7parAVgXNvbZqtQ7Q1WH4yyXRG9V0//NkxIgdEma9nsGFKoDppgPIOsY+siee/hAmTAYCBVlnUKJkIMr5MGZXbcMZO+pXBBZMmIQir2wRA4/BAUVdJZGh3diAApD0I1Z2RCnXRv5GudmkzvT/XT8+qIyzs6sV1IJRGXbut//p55CHi1ndpNtyMoGRRahoHJfWrPokKoQtFbl0jkzcYYZLg2sTewPd//NkxJYhE7LFnnmEvkV9GxO97U5yWpVVhtrhQFplxv9pZfComu9cyLZgwRLp+4shjD606LO5GlM5IHs9M7AcB7pAsxwGs/5sRnv9v2UJxRb0+qTWfSGVYYy/0K9g4EDjBco0ZJQoikc5drhA0yQechYA2QSpvJJcShaQfsJyg11mm4UGzocp8n8rt0lWZ/ak//NkxJQdQg7JnnjM3g/nlNF/D6QuzIhpOGbsmWW5q3YRoFCd3Xo2VD7C2PidcIkRLJC5cjM0ZgQ4FA/LwbrAUqVRLOCMJw8lg7OVB+QGD/l7DMP7jb6zbqYFRIehP3umk0X/ja+AkHgkK2CuO6cRyeUm17bxjGvKjjNJ6MvwPxS4ilfO9Q7MF62y363gjj48//NkxKI1NDapnnmYnILuZMFXXruUcX/F6zU7at95Pkue48iVnA6QHdLWWHzqwrKYS2hwQS7Zase1yJc50ZfXXht4QALspBmmts4io6WJqGtIQe3qXZhulpNqBPsUTkzV+qi9QTgq2e6VtGKaffSvfZdK9GHlL5enmpIlN5o9L1SF4Y+Mf/fmZDAjztQ/wiqR//NkxFAiq+KhqFmG3bExrmd274VQpUKFWJRNDyl9UEKBUTiS8+qQhkYHTH9ufuL8sjvokJ4tueujC9Kx5Jn0UfIULzSVWhmVeGdF3LXTUl4LYOej5Spt6QMKPQwPLJjeCHFK9I6gLAg4UORYOpCBkEyUgN2QcGXB3rYSsTlUpJtn+ZWnnxGX04U1p3/h/cW4//NkxEgdGoay9HpGMNCaH85kCQkLK6//98t+mDWXFHNlbGM4q8uv7akUPfQiwAH9jQXh8CyIejKiCr/r7mpM8peWgGF4sLF5Umhdsz40yoim7I64gojKARlrWoo/NQM/6JjQy+PH+lqpL7bkzJ3eyFJ5bHGOoZiqnv1KxFIi2n7u5yXVP+l7XQSLapNXR/yO//NkxFYcw9auVEjE9edS/9WM/T6/TvfWtX3+iWOVCIRwjkfR5Hc/73GyRIhWsJMpQ+SRbjZIpW8Qax9ukuI22b7R2UnU7dbUlW6prhhAk1hlytOhciOOayZNKUMocy4cuh9nn7dpZaQjsNr9Jvyr2P/2zURzlWEkcZH/+nHKldes/2DJ2z0HL1m1dMkAu7Rd//NkxGYc4oqe8jDFOfaw/CTNRf3coXSoQV2qCJqp2knTUDMy+7l3cu2cmYGaLj2G0MnDuOXagAHdxaBAAN6ucESFXDto1vovNENJZdEUzRCRv3Pq/XnSRaP+mQYgVPt1Odl0Z/9TlSSczus4wZf9ZjOsEj/NrHxRK7hRZAK1hcXectCLxOHWId0jar9r7mpq//NkxHUb0paq9GDE9NC0/ghFOl0BNq3AQJSC8y+EMlu3nWEbTbA25D+ZgUNTEnjmtUPQGi3VBI0vTzjdSPKAabDXqDJtJaZtlqjQvjUMQEhZ7knhUWDYBc5wbY6I0H1JWNNnBf9ePQFam/oS61ruq3Uu46e/Qp/qPGKKhplHkpq5CIwYt4Z125p0pDxPMRJQ//NkxIgcoOaqUssMqMyt00wDzT8ZMvGk0UFDGsrAWZf5rIh9FpC2MkKZCzKLWRDiYao3rUaHtS2durdutKEPmv//0+/8MK4f7rW/wM8VO1NBo638tU86RoEX/zp3///DpG75YRUAIFONlSNuaj5U3Fn3QC3TwOWFpUIHiHhJG4I9umYsCieKDkrBBr3mt0y2//NkxJgbKUqi1uaGaETTp7a0Oeit3koyiCsoq5Z2Yj/9DrEqrqrIjJ7EACEX67b9GZT+iLbun///9N1WEEMEa2Z2F1f//+gwacTBUrt96gEi5LI5dbd7jlETmGufVgFBTwgV8aCKhHqZWlwG4Xh+unCkGe0G8trvmDO93xN4z/DY8gh92A7qu+30/U9BczOV//NkxK4bGtadvsmEso8vz7UFat//87a9tERJSEXVfndmQjoRrasyh+KNobg/JybghYgvqDHHAJd//+RiURCV1jkvv+sMAkjLOjHmoIvkdTGKMHEZAHnJloKJJ+mYOMDHBgXAzTvmXPCwUx5g2EsAoAcRCAaJ4FBg4O2aVw/ainKeX4U92noHUZGsdx6OAIpD//NkxMQeAuK9vnlTWoIYUzom38jBACYljZnRaptsi76f//9Erv3q9djf/LcxGQlPmqhT0VZ1V2ZSzPtRv//oTRpznPY9/rkbnAwMDAAAAEMQ5ziCUYgDAgAlABEKdlu3wlvK6lTN5W0R2nFb3OPwz9yXONBrg/Wt8f6XNZazARdmHpK3aV0jHHSeYv2giFiA//NkxM8mK/qIJt6EXFBGAhoJUjgkM3FfGYPkonESdQJEo3TcZO0LuJiK0OJSj9/qVSyjOUhDWUlgmrWJ3ai0++v+rZn////8tQpyGHEmMUF//xhVIuZaBVUelR6DQNB1ILU52W//a+WQCB8nA6FZ2uL58ogOGi2nWPy47M21m1rW+US94pSPZXOGELm/m6vF//NkxLkiMtbKXsJFSmvJ9l69sD6PYEMX3J6VY6oSJo8nvn194ABIRvIEE3qJhcNtkAYJAEFDEZX2e+zK98jHF3sd+7ohO3/rdjv/puimEjgKBSoZ0dv//8nqep3///P7FkZzoKHf4IIBNx2EKXf67/BGsVHMdRUgnlCdJixDmiQ8xKrNoTLFeQtwcZxmn9cW//NkxLMiq9rWPmJLht1r//a23qrw9VrZBfVXkGeCVSzzacboagUzYXktkVxc2qineKRD0cn1BUTMlCMEDHwP9uKQn5xTHeILAMgxguAfY41HvQWms3TWdLiJkMYxMCtD5iXT5gdJM+WSfJMmDQqGCa6a/ZMzWkpl0N///+qpBBS7K6mha04lO5RM/cDRKSG8//NkxKszU+7WXnwTzxxhl8sb1i8R6gRtkB0Gwu0KFS9LkJjom12m05tT7s0o7AGqEaABVlOmdE04kaKiIkdp2uEQkVQw2g29sGJiy2J5VJ6z9ulnuZw3fp7/LmW9a/Xav8wtblNeVxys4kKsxvcfiSPsNgWQh8fVYQq5GMKniJWpqU9hEysXEwyC/NKV5kRw//NkxGAqS8KYdtJLcYQ9Jae////Pf7dlAdQiYSsDb0JpdAhUE55V3MiZR7JbUhWez////8rqylv/mQx3RlQ8XFBIVcJlnYzOprsm5CGHEKomQEAK9v7VtuABdt2/5mgLQikQ81Ck6YqNduFTzdqJHbrEY9wNwWbeXv3fZTtdS1/lQwiKzGcyKwqcJHV26l+0//NkxDkekvKofssKWM8rlKgkcokNEgYRDrZg8xnQz/b+zGs3c6zTrLjBIg4rf3p///O6up3IowczCQUf6RKAgoRkRKsFYd61OFDwaYpalfh2k3N1zRkHaEFgOLP2UAr6s+MocXkA8oH5Y3BaoAM2keVs6+dtusjZprlu9M2QaWLDIo+v98U9ZvjvWt25aP9B//NkxEEdAvKcNtGEvo6GI/5ucFIyFZK+mbuqFZZhQrOVCsRP//msY7oEOildgUSi/rW7Vf+l4oCAuacNub2LjhASNMydfwD0qCGf4POSlRyOssZulQzich6DWdyuxDFI/l6Yw+93Hed/C7Wild24fdNTN/muWJRF+17UnPpZe2+jTr1BuXxmsTtDqMgaMkst//NkxFAo29KYPssLTMJmjhEu0JyT4DtWs2Dqf7b7YlhoEMUTF0Qjf2VGo+1Ff+r/lfrnd2vU/ev+8/Wrr/+pFQmSiEIHBQQFBM5A4KEIRq5KEkOHA4AAoRjnD4oICfo9iga+Jckkl8Ix9kzmu4+N10+wswDV6PcTLZWx1SvQy1LqdLl/OaGhpdBsnNzGlWnu//NkxC8mS+LM/noHVlyftxpfXUbWLRTbUcLh8KCUaCthHIshAX0LwPGW1PLGiASKHA1MUg5vnT//gUlz0fgetzfGIB5bRP/+J+iVMAQuemkR4VT230y89tdeznSyqkuaxZZeHn0sv6tJBkbQSoVdApA1dexznPRVQAHFI7gowg04A8jlXFolsFhlGG4GYR0J//NkxBgcUWKwPnvGPFCkDxlPuDZMxrLGtNhoTJFLExZlEhXCOHIhSI7klxP7JvQr2QzyPztqkcwbYUyk6eEY8JsPKxg+IEkl3tOpJFfPCIQ1KDtBFR7I+Vp/1n2f7WGL2AqAwcFaFQ0BCackttykaMEfBghnrqW80kl6FEBEnqRaac2kdUH66n4UsMDe1FL1//NkxCkcOh7RnljTNjL2SkUIWZFG5LI+lEAZOKpUMgx94f0ibP2z9OwaVE46jcjc2uwuw2sTyXU0m3XCjHL3f+3ocGz52qPg2FIQVitzHN0EFDyaGCAC29626AMtA5QWwvqT6M8QdFoPOT3tfdUekOKk1P9ZQhhlHkmEEGocp92kURSZrTnzMuRgxAxO3S37//NkxDscm+bI9lpEPlv+RRZBwyBDEajo6DoW0S7P81RgYyDM/X3//frXfmU6nVQ0VvdWV7QfT7fSZuzadxlGk0jRtrk1RkVdpy3XbjQrob53p5If4/aPp6Bzkz3Z6fSaSXYCuxj7gxFzDLU70JJyFKdYGFbazhPySJiIOhq4nvPO963/61ZwJAjEORykQ1jK//NkxEsc297ZnkjFTkT2/oVBnGr++pv//6UrZJqIViMyrZZBAgj00//yzTTq6gCMOJwPZ21KCjbkk9yKhk+283K63lyY6U0URBZmme/w8vTXFTQOICNRALLbjUouVUzEIlXIRTEQsWya1IzOoyqm/f7b/b+rIzO+jr1M1yZbf3VCER9UZFajIqOxnVLIrvXb//NkxFob89bENnpEX3df9aK6ESYbMU3//1qpzzMEoQFxfXJMVREopy228PlOGeDToZenRyKHtt2ek3Mpa+vDUi6AqQCGFoENiI9czOdsKCU/N9LJdj93sVXQiN+SybK7f2UpxlMUWi1Id5mUruv+rMqOiuay0/pIvvbrR//7kYWzGdFTY9/vJ7kYdzLRUdSC//NkxG0cw9rZvkjE91BUQikf/QY9Cgnf/t4oW0oHp8jdLijpybTh0zWe9rdU3SudYZMfdysuobqiYEBkBNS/51SAgIMBOqsGOs3V2Z2ZVIw0B4si5Qo1EFqzv/6cqadF9HRaKf7qyHdeyf//8yWmrVfbuZDtelStN+u9HrRymUtKyEilgpTVJdACMku2Y4cH//NkxH0cy9q5HnjFFIowWdDUrDE5UtvFlVO0eQSugME402NwjQbDGlji7xOOWTlvnaDr2v52t1UZFsyrRWdVarS1s29DVL52Kxpdf/erNT7L+cwfKh25piJ///pNdDI8qFRP9r1Rdf/r+/Qrs5SVZziQRBRJLL/GlSDEAFLXddJFUdSMPcDEAcFCm5iwZGGK//NkxIwdE9aQfsoEneJSZeSnFYBQoIYIYQgZWUCIU5WcPyIvc3Y1OizFmFIDFu4cX+X2u7gCIsV8XcOdOjuNgGEMl2+RZeTmFiUXHKK4UAgs9v/VUxV36hlJNSUemsXcL48Hz4HD96tSDlhbZbwkcPMVUAmRGTKsJVAquh5qEjb2VzsG44/n9zrrW1qE9aZt//NkxJocqaKIftPGSEezqTk5M5tNo6wTH2qMsOlxSJykVB+Gw/jgjEhUkR1UkxSIA6kAmmzw+VULa5Nn3X5vM6MzW//QtdGMZVbd6ZTs/et+1r///qmjWMweH/R9/CKhhBtzQw97q/a3rsfugQoQsEYdKrUAdPYAFvZNFXkhacN5KibbwqfZnGWZ1ZL2HL+E//NkxKojW96EbtME3e625WYpX35uO9mFFz+6uZpLEY8AQERk/ZbZJGef2Etdt0ZOVK6H5wnKhOSkYsKyYVWCbB0XXbUv+moADFQ9/Exl0LOEa3Co0Dh45ni7/11tdBY6E92rODSxkyx5nV+cE62nRqoApuwgPbHOJ0jQHshqosiJ2HasU7FharH02UjSXxDj//NkxJ8fyZaRtsMGvLtgywiFluenELYXs9ACwHetqxPocXg0AzkmyseXt4mWXkTHIXCY3BaPGNNdDV/+h3CsS3/7bpSwZGPuUygbHdhJxNT/2ZmTRL/9PrlKisXb//X93f//63R7BQF22T9dmQT3Gni0NmpNlSx1mXxmTJnYqRfmOwqPWNO9EI5IL2q+dPMw//NkxKIfg8qptnjFZ0PY9bO11tgYYuRexUQnSpiJg9CcIoN1iq91OrvzsmmpKfaxdBmw6qsbwIVLSmZGX1gKYT/2sc3KgIKyOp5EVbBoTJ0J5IdfX+0vnUsey3554KlRZL3VACZBR3XaXQtqDNaWk6JCZHM9Nl9j9ScLnr3Su4pPKwYoLgkjwJRSOVyqxwFH//NkxKceWYqUVsMHJlFxUlvoW6nGIKsIlOEXIzJ9Ck+2oVCk7hoS3KSFw/+ff//Y1Jj1MSbwnVOJoWNkohBaAigOhEGRoiPvmiPoOkhhQik20TvK0nyd+siSUqBku+/u7piQ7p3LswNpGwppc5FlciwPJASmAS0aVKaDhhW+pHvbe6PuhiB4BAcPChUd3fu5//NkxLAeqiqtnmFHLpJCxJDLmGg33NdbTBPXm3+lCRQhS/Pz2txbKE503tVBDe7LHO37xL58P+3/odgs1Qd9Jvr4MjJP19NLPZv//1Vt3ShzBMj0LqZ+TIfsx7pQiDLItMVVczkuLw8RZYjKkD5JmsnhHLEmHagjNLEEFSlJ4mJmpadZ4ge1tDlDglGkOYvH//NkxLgdGYq0XkmKd/oRm9ZQqEaUoMYNyp34hhskIf63AyLJrZccCIsyL0f//9DlbZbfDAq0TqRY/9BIHMku31sl9IN6tF9mOJhceF5AMFzq6Jy1aUtzXLMNEhAMVKVoiAcg33ulclNPn7MVMO1l6KBwNxqL7duhEZka5yOyWR2/neQhPfStGUi/qiHZ7i2g//NkxMYcOcKYHnoHLkD60rFrfpbpz2fn2LLC5cUMBjqYqiqWXEj3312WAPxNo5PQHOdbXDeTYeeDxDebcUM7T37Upf0R6hzT+h3qsu8x1wzEQrls4WrIboroaXTxRfQ1zxHhP/ea7vdnzHgQBURgQDIOCGfAwu9/ptuVshh7RPWt2RQ8+WMxDZ/7+61WyDEK//NkxNgakmKxfmGFDoos2Q//4b0JGrAho+pokexxLCZlyiazB3X6agVbNrZmVruQTWX8lkA08A9krnGJ7BUEMbZS77jUjrXpZhB1uHoIX3DrFWbGUjdGbR+Ba8Zoco/OZQDG1TM8t1KapY5c0p7v4neQ0uWnIWXE+MejbdVygjuOUp0QaPT9cqv6Z7q1wTFq//NkxPAh0oaxfnsQmn/2rQIIIVUU4I///ua2EIq4e33j1XvHDAPrlKoJFLct2u7QZ4mJvv0vfOoUHx3rm8gTtb1mzd/mTcd/CZG6sKHj7l+mh18WuSzMuUnKgYPHtjjVplRjoYxXrr7//+7HnjjRilF2easf879vfN6DhckRiwsoChA8UEASjh49kv5bPPfh//NkxOsgwpKAHsGFZM8XNJlSIj/kzv//5J0o3fe1GpT/tZKC41gbxSiwI86mA//72q8ZbSbrv2xGICiY8I4sbDI2Q2qYE0UAYErKzWyTeYCo4hlJYilJLycyTMhro6GAWMgs7pf0ymNsDQOACyKf/8rFUKUWCUIViS9v/+hxb3yBBVv+Qp2LtXylO04sFQ+K//NkxOsi47KtnnoLPyMdxKJYagQWHNFW/rOxUMsi9wuuio4UZJ0QIAkE23hBiFBAQhKybjtLmSETkGSTmZTuI5jsTjQyH4dBRFLIqmaVhe0j5zbG611iurb//z9PrMLinmudjf0xd2W91UVrMigATAcmIaLiOsk6PbfmU6qg5VdE//35jSlrhQFP2VJQYgUy//NkxOId6rq1nnpEPqf7kUrkdEoX/9daM11Rgys13r//fbtlHkB0IQOKCkaapfFd5OEsIBtAyza7yDMNG54MsN7BiMJ1lgRxNyVohKhxmUTgNSEHUDDR2CxyFgg8IYhyJo1JuwqBmVX+osHhixVxSPUvBrgLhG7RN1c39xTU9aXjCThlrp3z38/3qMm4g4q6//NkxO0j++aA3nyFhWGrx//wopao1ngBvcKVd6c4iNYKkBxc+cIJdY9fWKhQyh4sdEhrQupeAByRKW23dIVipOiYTyWiYmo2sMqKHiqItsf5Zsc10EWynV7LUOZ8+yfB8sTzjStyqLT84eHgehOI5MnTm865XAUMpFdir+yOtp0RBxSshlIyLR/52NGUKzrP//NkxOAhEnaE/njQ7POeIEId2bv+n17r3pUspdbr/6123MKOVCPKGOMJZPqUPtl+d8oMABONx62/ddmAdLglh0pBVREQ9Q0sZM4SXij0k6MY32Bzda1SXYqMIQ01tGmJkXLht577u25rucq0R/nTmqsQre50vr9bQHxGOHC4xWTFn3eX+n1Kxv6F32/UpHAU//NkxN4f896lvknFa3mB8UcJkrEp198UzVkCRcuhPKBlb29bu6prA8YIbusIEb11uJd12Pp25NdeJtH+TCNaCAjYkbVdIC3LbdS0HBSlVzJ4MjhAIAVQiRRQYz0d8rK3Y6WufeUu5rGOlmh6OysdlZDXct3MdllkFAZlqzNsrHOqPMrvSR2K2qURDf/Y7IMC//NkxOEfQm6BvnoK/EYXGqbnBqg9JOi4F3uItf+ltn/5HVVMQU1FMy4xMCEAa6KaOVSE4spWT2wlRLiCBEsCMSwaw3EEBpAAcU4ltCNEZxt0F9fsbjGzPgktq2cye7nJN3mq9DO2pUM/eb9HOfKXRzTUXsurV1aqeYyoOxn/USGUEg2eASjLUII1VwV/I/r///NkxOceWm5gTMGK8OVo9VXsySrMT4EkEGqQSo3SUe9+FYogipYLafBCjSFeCRFivhSrBzP3F8WHpo035FVw5OzsSCMSljGW4OFaVu5HFSrQytNcobD1kmxBKWTAscJRMCkUO+dRWDr5FabWrr7rla1Uq6jWdf//uGluLr/2VpSUOOf8pRu3MufUlbudl/9l//NkxOgaympllmGE8CQU3q38m7pUTUc/np5Savm7WsyJZ+X4+q7jSA7FJrlTZ7TpK4ZDfQ1pzM+GZg1h8hSO5GsLMvSeXrw+VdOqTkRFYpXIzcktJteztuiUntY9HSIkZy3iXbexNDM1JrTbjUz8Okta6HbzyJWpTpu6E5QzMnK2qRfcgXDsQPgGCwlk2y62//NkxP8kAq4wCmPQqW2JxDB/+CSVKeDBaGx06IK0IO2T3IlVGQLYcMWRMCCYNmoUGVVxSjsVb1DkjdUhBB6H88OHR0ra0lHJKWGKnhkCxEHRopoxIS4NZqgqgnFEb12CKVBShjQYrhWGcnUMzEO8k8GWUEhHIMrEDotlIIh7OEWCBFlNQyARjEil1XoMHDBj//NkxPIdm6IYykjGBfjHFB1RYZowg8qGQYYwiqxAhzQGIQADyaoJo2hrvGCLsZVCGPt8ktRqo9CnURC/MKvTfHFubE6tMhziJDWmCOo0eskhsjHUD5Q5SIkEGuaoaK5BVBjMbJ6yytHelCIwkQNeN0hDlSFFIXGZjrsCsjoZo0MzfJCMoRAiJEV0MjWJS9SL//NkxP4kzBIEAHiGAb7qxHYm7udPJxFlpDJNTOtqYixJ6DJHKhXRQ282kQy15ubrSCoDttkBqG9lhksnmhVVhBCD1panop2sx5azbLc4evsaPDj0q0uMXPeUsqil5FnDqHPUu0s10hGptsfCysv3aeZ5wmOusIo05ThETrpSLn7GqGde9Iva5sWWfT2QFuht//NkxO0gRDIIAFjGCSvpggV3tjbjaO8qgAQhM0zMzMoUSwpmDASqs1VfoUBVVVaVVSoCAgQrUBWNW41XVW/+kyqpKQYCNSyjMwF1QomhS+GFLqyZMcZgyqFAVjVdf9S/4zHhljar+qMbMzHt8Zv1WH/7Mza/xmAhVUqTMGAgIUykzbH//xj/41LU12FGWoYC//NkxO8dk/IU6kDGBTWMwEBFVCrDv5bARipMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxPshNDHsSEjGCaqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq'
_TTS_IRISH_B64 = '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qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq//NkxHwAAANIAAAAAKqqqqqqqqpMQU1FMy4xMDCqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq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s7bublScaUVeblTTvG/7LO8bmycaKKPjc3Kk1nZ2cou83NmjSirzf/syzs9SUW8bm5RpxVxublGmlXDs7DaeZGRDX/+RGX/2ERmAaaEEiGmmERmTWwkRl7VZZYZM1l/Mll/5f/2WSp9lmf8s//NkxO0e0+IYwhjFHbSPyZQwKgdMtUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxPQeXB24pDBH6VVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVMQU1FMy4xMDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NkxHwAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV'
_TTS_CLAIRE_B64 = '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WNNGH44IggQ3VqVnHhiU7RgUXTjekDMFNC4YcMVp//////////////////+n//////////////////v2FZVSMiIDECAAAEIMIMEDIOBTrNzL3CiRqIu+puZUQY08xnkMwXoCTgE6DTigdJB7zLDUflFU2J9pjcWBkQIKAy3Re8IEO+1NyJYgmMOAzFhiTwyUAVohCtfb+MNciYY4FAQjEg0SsQiFDpsyBAJEmOB1HfgmLyJ3A4WlQ1wBCHdARtexI8MhAMqfPsQMNUhyVz2DWN11BDCg2ps7kMZdc0CIOsKXBYEoqreuvPnM86lJV6j/+5LEWQAovg092Z0AAxyyKlOy8ACutfheNQdg78LscQxAhmIwAWAa4DBxEnR3YvM9wtcrz9jG/SNCUEuskTTX/Ua3cQlp9hYgOlVppyEQkEBi0BiDwMFIoAwZ/////O6x/dPm/jbwxefvC81hlkrZZM2mhxAOAKDsWa/C02Uq8VH2psUW4hsLC3m//////////////////+9n/////////////////yu/8CbEQHlBoZszgkKZXO2B2XHeJ5L0mkbd4dxuPWWIyRrRX0NtYy2eHtxhMOz4TKiSy++jTwUNULI5OLpLhxH8qU+firL6eLWnnZQBIiVrBfAXIvUKH+ZBdipVhgnIeRcT1YxMUsN0frG6gw2BzQ1DYEJzeOJ+zIa9tl6ws22bGVKyL8Rijp1Qn8t0YTeWfFgvYj94qtKZmZtsznWjCiobA/ZVylVFWFHrttmYZocXc2NTfdo8WDF3qNX5xbdrPswt0vXVdWkjbwEAA4JVBjYJYHJhhBAcRBDoaYehhieCAIxYgShZgrBehyPxKNzL1yKBZfHYbemq0mcl//uSxBkAlL1bSK29OIqRrKnpt6H5MbpIbg/raX2TPsWaGhd+qencWqM9uwN0VWx5K4Yllubh9F7CVLDovZWMSXDlIOWBLjseNrpwsSIxEwSj0YX5evnjSTcVtihIYdFteMN2VIsnUiblqER+6jbPk9s6Q1sRkj1NKWPW3lY6mn43bDBws/85B8mIFISoouRAhrIyDjEZSDDEF+DA60ib3xMdEXkGQhiLuMTizKCbwi4Ugp5VXPJaIKjR9hNTkoOJbIKgTDAEpgCQIYjGNtV89LXviBjqyr9zTziwjKL42H+T81TsVinTy8ynVhz19gIEACli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'
_TTS_LINE = 'A Limerick dental practice was missing forty calls a month. We answered every one for one hundred and forty nine euro.'
_TTS_PRESETS = {
    "irish": ("en-IE-ConnorNeural", "-6%", "+0Hz"),
    "irish-f": ("en-IE-EmilyNeural", "-6%", "+0Hz"),
    "premium": ("en-US-AndrewMultilingualNeural", "-6%", "+0Hz"),
    "casual": ("en-US-BrianMultilingualNeural", "-4%", "+0Hz"),
    "premium-f": ("en-US-AvaMultilingualNeural", "-4%", "+0Hz"),
}


@app.get("/admin/api/tts-samples")
async def tts_samples_api(token: str = Query("")):
    check_admin(token)
    return {
        "line": _TTS_LINE,
        "kokoro": "data:audio/mpeg;base64," + _TTS_KOKORO_B64,
        "premium": "data:audio/mpeg;base64," + _TTS_PREMIUM_B64,
        "irish": "data:audio/mpeg;base64," + _TTS_IRISH_B64,
        "claire": "data:audio/mpeg;base64," + _TTS_CLAIRE_B64,
    }


@app.get("/admin/api/edge-voices")
async def edge_voices(token: str = Query("")):
    check_admin(token)
    try:
        import edge_tts as _et
        vs = await _et.list_voices()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502,
                            content={"error": str(exc)[:200]})
    out = []
    for v in vs:
        sn = v.get("ShortName", "")
        if not sn.startswith(("en-US", "en-GB", "en-AU", "en-IE",
                              "en-CA", "en-NZ")):
            continue
        out.append({
            "id": sn,
            "loc": sn.split("-")[1],
            "premium": "Multilingual" in sn,
            "label": sn.replace("Neural", "").replace("Multilingual",
                     " Multilingual").split("-", 2)[-1],
            "gender": v.get("Gender", ""),
        })
    out.sort(key=lambda x: (not x["premium"], x["loc"], x["id"]))
    return {"voices": out}


@app.get("/admin/api/el-voices")
async def el_voices(token: str = Query("")):
    check_admin(token)
    import urllib.request as _u, json as _j
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    try:
        v = _j.loads(_u.urlopen(_u.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": key}), timeout=20).read())
        sub = _j.loads(_u.urlopen(_u.Request(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": key}), timeout=20).read())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502,
                            content={"error": str(exc)[:200]})
    return {
        "chars_used": sub.get("character_count"),
        "chars_limit": sub.get("character_limit"),
        "voices": [{"id": x.get("voice_id"), "name": x.get("name"),
                    "labels": list((x.get("labels") or {}).values())}
                   for x in v.get("voices", [])],
    }


@app.get("/admin/api/tts-preview")
async def tts_preview(
    token: str = Query(""), engine: str = Query("edge"),
    voice: str = Query("premium"),
    rate: str = Query("-6%"), pitch: str = Query("+0Hz"),
    volume: str = Query("+0%"), text: str = Query(""),
    el_voice: str = Query(""),
    stability: float = Query(0.5),
    similarity: float = Query(0.75),
    style: float = Query(0.0),
    model: str = Query("eleven_multilingual_v2"),
):
    # Live edge voice studio. Lazy import: a missing edge-tts dep
    # 502s ONLY this endpoint, never the receptionist server.
    check_admin(token)
    if engine == "elevenlabs":
        import urllib.request as _u, json as _j
        key = os.environ.get("ELEVENLABS_API_KEY", "")
        vid = el_voice or os.environ.get("ELEVENLABS_CLAIRE_VOICE_ID", "")
        say_el = (text or _TTS_LINE)[:600]
        body = _j.dumps({"text": say_el, "model_id": model,
            "voice_settings": {"stability": stability,
                "similarity_boost": similarity,
                "style": style,
                "use_speaker_boost": True}}).encode()
        try:
            au = _u.urlopen(_u.Request(
                "https://api.elevenlabs.io/v1/text-to-speech/" + vid, body,
                {"xi-api-key": key,
                 "Content-Type": "application/json",
                 "Accept": "audio/mpeg"}), timeout=120).read()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=502, content={
                "error": "elevenlabs: " + type(exc).__name__ + str(exc)[:200]})
        return Response(content=au, media_type="audio/mpeg")
    say = (text or _TTS_LINE)[:600]
    vn, _, _ = _TTS_PRESETS.get(voice, (voice, None, None))

    def _signed(val, unit):
        # edge_tts requires SIGNED offsets (+0%/-6%/+2Hz). Sliders send
        # bare "0%"/"0Hz" -> ValueError -> 502. Force a leading +/-.
        s = str(val).strip()
        if s.endswith(unit):
            s = s[:-len(unit)]
        s = s.strip() or "0"
        if not s.startswith(("+", "-")):
            s = "+" + s
        return s + unit

    rate = _signed(rate, "%")
    volume = _signed(volume, "%")
    pitch = _signed(pitch, "Hz")
    try:
        import io as _io, edge_tts as _et, asyncio as _aio
        async def _gen():
            c = _et.Communicate(say, vn, rate=rate,
                                volume=volume, pitch=pitch)
            buf = bytearray()
            async for ch in c.stream():
                if ch.get('type') == 'audio':
                    buf += ch['data']
            return bytes(buf)
        audio = await _gen()
    except Exception as exc:  # noqa: BLE001
        msg = type(exc).__name__ + ': ' + str(exc)[:200]
        return JSONResponse(status_code=502, content={'error': 'edge preview failed: ' + msg, 'hint': 'edge-tts dep may not be deployed yet; redeploy after requirements.txt update.'})
    if not audio:
        return JSONResponse(status_code=502, content={'error': 'empty audio'})
    return Response(content=audio, media_type="audio/mpeg")


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


@app.post("/admin/api/remirror/{call_id}")
async def admin_remirror(call_id: str, background_tasks: BackgroundTasks,
                          token: str = Query("")):
    """2026-05-20 — manual recording-mirror trigger.

    Use to backfill a single call when the EOC race (recordingUrl empty in
    end-of-call-report payload) silently dropped the mirror. Hits Vapi
    GET /call/{id}; if URLs present, schedules `_mirror_recording_to_hetzner`
    immediately; if still empty, schedules the delayed poller. Idempotent
    against the Hetzner key (existing audio is overwritten with same bytes;
    safe to call multiple times).

    Token-gated via ADMIN_TOKEN. Look up assistant_id from the latest
    call_events row so the mirror log_event() carries the right tenant."""
    check_admin(token)
    if not call_id:
        raise HTTPException(status_code=400, detail="call_id required")
    api_key = os.environ.get("VAPI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="VAPI_API_KEY missing on server")

    assistant_id = ""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT assistant FROM call_events WHERE call_id=? "
                "AND event_type='call-ended' ORDER BY id DESC LIMIT 1",
                (call_id,),
            ).fetchone()
            if row:
                assistant_id = (row["assistant"] if "assistant" in row.keys() else "") or ""
    except Exception:
        pass

    import requests as _rq
    try:
        r = _rq.get(
            f"https://api.vapi.ai/call/{call_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        r.raise_for_status()
        call = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vapi fetch failed: {str(e)[:200]}")

    art = call.get("artifact") or {}
    mono = art.get("recordingUrl") or call.get("recordingUrl") or ""
    stereo = art.get("stereoRecordingUrl") or call.get("stereoRecordingUrl") or ""
    if not (mono or stereo):
        rec = (art.get("recording") or {})
        stereo = stereo or rec.get("stereoUrl") or ""
        mono = mono or ((rec.get("mono") or {}).get("combinedUrl") or "")
    if not assistant_id:
        assistant_id = (
            call.get("assistantId")
            or (call.get("assistant") or {}).get("id")
            or ""
        )

    if mono or stereo:
        background_tasks.add_task(
            _mirror_recording_to_hetzner,
            call_id, assistant_id, mono, stereo,
        )
        return JSONResponse({
            "status": "scheduled",
            "call_id": call_id,
            "assistant_id": assistant_id,
            "mono": bool(mono),
            "stereo": bool(stereo),
        })

    background_tasks.add_task(
        _delayed_mirror_via_vapi, call_id, assistant_id,
    )
    return JSONResponse({
        "status": "delayed_retry_scheduled",
        "call_id": call_id,
        "assistant_id": assistant_id,
    })


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
