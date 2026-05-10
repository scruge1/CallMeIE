"""LeadIngestor — single chokepoint for unified_leads writes (P2-4 Phase 1).

Each channel handler calls `LeadIngestor.upsert(channel, payload, source_id)`
post-row-commit on its own table. The service computes dedupe_key, performs
INSERT ... ON CONFLICT (dedupe_key) DO UPDATE, and fires Telegram for
high-priority channels.

Dedupe priority:
  1. email  (lowercased + trimmed; gmail dot-stripping not implemented v1)
  2. phone  (E.164-ish — strip non-digit/non-+ chars)
  3. synthetic  (sha256 of source_id+channel+timestamp; never merges)

Fuzzy match (business_name + contact_name Levenshtein) is documented in
PDR-NEXT-P2-4-UNIFIED-LEADS.md §1.2 but not implemented in v1 — deferred
until manual-merge UI ships.

Failure mode: any exception is caught and logged. Channel handler's primary
INSERT must NOT roll back if unified_leads upsert fails — the spec says
share the same SQLAlchemy session, but this codebase uses raw _DbProxy +
psycopg/sqlite3 with manual commit boundaries. So we open a SEPARATE
short-lived connection and let it fail in isolation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from typing import Any


# Avoid circular import with server.py — caller passes get_db.
# Wired this way so unit tests can mock the connection factory.
_PRIORITY_CHANNELS = {"audit"}  # auto-priority=2
_VALID_CHANNELS = {"discovery", "onboard", "owl_form", "email", "whatsapp", "audit", "manual"}


def _normalise_email(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().lower()
    if not s or "@" not in s:
        return None
    return s


def _normalise_phone(s: str | None) -> str | None:
    if not s:
        return None
    digits = re.sub(r"[^0-9+]", "", s)
    if len(digits) < 7:  # too short to be a real phone
        return None
    return digits


def _compute_dedupe(payload: dict) -> tuple[str, str]:
    """Return (dedupe_key, dedupe_kind)."""
    email = _normalise_email(payload.get("contact_email") or payload.get("email"))
    if email:
        return email, "email"
    phone = _normalise_phone(payload.get("contact_phone") or payload.get("phone"))
    if phone:
        return f"phone:{phone}", "phone"
    # Synthetic — never merges. Use channel + source_id + payload hash for stability.
    seed = f"{payload.get('_channel','')}:{payload.get('_source_id','')}:{json.dumps(payload, sort_keys=True, default=str)[:500]}"
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"syn:{h}", "synthetic"


def upsert(
    get_db,
    channel: str,
    payload: dict,
    source_id: str | int | None = None,
) -> dict[str, Any]:
    """Upsert into unified_leads. Returns minimal dict {id, dedupe_key, action}.

    `get_db` is the connection factory from server.py (passed to break circular import).
    `channel` must be one of _VALID_CHANNELS.
    `payload` should have at least one of contact_email / contact_phone / business_name.
    `source_id` is the originating row id (discovery_submissions.id, etc).
    """
    if channel not in _VALID_CHANNELS:
        print(f"[LeadIngestor] invalid channel={channel!r}", file=sys.stderr)
        return {"id": None, "action": "rejected", "reason": "invalid_channel"}

    # Normalise contact fields
    email = _normalise_email(payload.get("contact_email") or payload.get("email"))
    phone = _normalise_phone(payload.get("contact_phone") or payload.get("phone"))
    name = payload.get("contact_name") or payload.get("name")
    business = payload.get("business_name") or payload.get("business")

    # Compute dedupe key
    payload_for_dedupe = dict(payload)
    payload_for_dedupe["_channel"] = channel
    payload_for_dedupe["_source_id"] = str(source_id) if source_id is not None else ""
    dedupe_key, dedupe_kind = _compute_dedupe(payload_for_dedupe)

    # source_refs: map channel → field name used for the soft FK
    source_ref_field = {
        "discovery": "discovery_submission_id",
        "onboard": "submission_id",
        "owl_form": "owl_lead_id",
        "audit": "stripe_session_id",
        "email": "message_id",
        "whatsapp": "wa_id",
        "manual": "manual_id",
    }.get(channel, "source_id")
    source_refs = {source_ref_field: str(source_id)} if source_id is not None else {}

    # priority bump
    priority = 2 if channel in _PRIORITY_CHANNELS else 0

    # Postgres path uses jsonb; SQLite path stores as text. _ddl_fix translates
    # the schema; here we send Python dict + json.dumps because raw SQL doesn't
    # auto-serialise dicts on either dialect.
    source_refs_json = json.dumps(source_refs)
    raw_payload_json = json.dumps(_redact(payload), default=str)[:32000]

    sql = """
        INSERT INTO unified_leads (
          dedupe_key, dedupe_kind, contact_email, contact_phone, contact_name,
          business_name, primary_channel, channels, status, priority,
          source_refs, raw_payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ARRAY[?]::text[], 'new', ?, ?::jsonb, ?::jsonb)
        ON CONFLICT (dedupe_key) DO UPDATE
          SET channels = (
                SELECT ARRAY_AGG(DISTINCT c)
                FROM UNNEST(unified_leads.channels || EXCLUDED.channels) AS c
              ),
              source_refs = unified_leads.source_refs || EXCLUDED.source_refs,
              raw_payload = EXCLUDED.raw_payload,
              last_touch_at = NOW(),
              contact_email = COALESCE(unified_leads.contact_email, EXCLUDED.contact_email),
              contact_phone = COALESCE(unified_leads.contact_phone, EXCLUDED.contact_phone),
              contact_name  = COALESCE(unified_leads.contact_name,  EXCLUDED.contact_name),
              business_name = COALESCE(unified_leads.business_name, EXCLUDED.business_name),
              priority = GREATEST(unified_leads.priority, EXCLUDED.priority)
        RETURNING id, (xmax = 0) AS inserted
    """

    try:
        with get_db() as conn:
            cur = conn.execute(sql, (
                dedupe_key, dedupe_kind, email, phone, name,
                business, channel, channel, priority,
                source_refs_json, raw_payload_json,
            ))
            row = cur.fetchone()
            conn.commit()
            if row is None:
                return {"id": None, "action": "noop"}
            try:
                lead_id = row["id"]
                inserted = bool(row["inserted"])
            except (TypeError, KeyError, IndexError):
                lead_id = row[0]
                inserted = bool(row[1]) if len(row) > 1 else False

            # Audit log: status_log row only on first creation
            if inserted:
                try:
                    with get_db() as conn2:
                        conn2.execute(
                            "INSERT INTO lead_status_log (lead_id, from_status, to_status, actor, note) "
                            "VALUES (?, NULL, 'new', 'system', ?)",
                            (str(lead_id), f"created via channel={channel}")
                        )
                        conn2.commit()
                except Exception as e:
                    print(f"[LeadIngestor] status_log insert failed: {e}", file=sys.stderr)

            # Priority Telegram fire-and-forget
            if priority >= 2:
                _fire_telegram(channel, email or phone or "(no contact)", business or name or "")

            return {
                "id": str(lead_id),
                "action": "inserted" if inserted else "updated",
                "dedupe_key": dedupe_key,
                "channel": channel,
            }
    except Exception as e:
        # Defensive — channel handler must NOT crash on unified-leads failure
        print(f"[LeadIngestor] upsert failed channel={channel} err={e}", file=sys.stderr)
        return {"id": None, "action": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PII_KEYS_TO_REDACT = {"password", "passwd", "secret", "token", "api_key", "card", "cvv"}


def _redact(d: dict) -> dict:
    """Drop obvious secrets from raw_payload before storing."""
    if not isinstance(d, dict):
        return {"_raw": str(d)[:1000]}
    out = {}
    for k, v in d.items():
        if k.lower() in _PII_KEYS_TO_REDACT:
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = _redact(v)
        elif isinstance(v, str) and len(v) > 4000:
            out[k] = v[:4000] + "...(truncated)"
        else:
            out[k] = v
    return out


def _fire_telegram(channel: str, who: str, what: str) -> None:
    """Fire-and-forget Telegram alert for priority>=2 channels."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return
    try:
        import urllib.request
        import urllib.parse
        msg = f"[lead] {channel} · {who} · {what[:120]}"
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode("utf-8")
        urllib.request.urlopen(url, data=data, timeout=3)
    except Exception as e:
        print(f"[LeadIngestor] telegram fire failed: {e}", file=sys.stderr)
