"""Bounded status metadata in the existing call_events store, not billing data."""
import hashlib
import json
import math
from datetime import datetime, timezone


STATUSES = frozenset(('scheduled', 'queued', 'ringing', 'in-progress', 'forwarding', 'ended'))


def _identifier(value, *, required=False):
    if value is None or value == '':
        if required:
            raise ValueError('missing_call_id')
        return None
    if (not isinstance(value, str) or len(value) > 128
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError('invalid_identifier')
    return value


def normalize_status_event(message):
    """Retain only identifiers/status/provider timestamp; no caller or artifacts.

    Provider timestamp is optional and numeric in the official SDK. Preserve its
    raw value without guessing units or treating receipt time as event order.
    Missing identifiers remain unknown, not inferred from another call.
    """
    if not isinstance(message, dict) or message.get('type') != 'status-update':
        raise ValueError('invalid_status_event')
    call = message.get('call')
    if not isinstance(call, dict):
        raise ValueError('invalid_call')
    status = message.get('status')
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError('invalid_status')
    stamp = message.get('timestamp')
    if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                              or stamp < 0 or stamp > 10**16 or not math.isfinite(stamp)):
        raise ValueError('invalid_timestamp')
    detail = {
        'schema': 'call-status-v1',
        'call_id': _identifier(call.get('id'), required=True),
        'assistant_id': _identifier(call.get('assistantId')),
        'line_id': _identifier(call.get('phoneNumberId')),
        'status': status,
        'provider_timestamp': stamp,
    }
    encoded = json.dumps(detail, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return detail, encoded, hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def store_status_event(connection_factory, normalized):
    """Atomic insert/dedupe. Return only after explicit commit; failures propagate.

    The unique event_key index is required. A missing migration is an error,
    never permission to fall back to a racy select-before-insert.
    """
    detail, encoded, key = normalized
    with connection_factory() as conn:
        row = conn.execute(
            'INSERT INTO call_events(call_id,event_type,assistant,summary,detail,event_key) '
            'VALUES(?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING RETURNING id',
            (detail['call_id'], 'call-status', detail['assistant_id'],
             'Call status: ' + detail['status'], encoded, key),
        ).fetchone()
        conn.commit()
    return {'status': 'ok', 'stored': row is not None, 'duplicate': row is None}


def read_status_feed(connection_factory):
    """Last persisted receipt only: not heartbeat, active count or delivery coverage."""
    with connection_factory() as conn:
        row = conn.execute(
            "SELECT created_at FROM call_events WHERE event_type='call-status' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return {'status': 'no_events', 'last_received_at': None, 'coverage_verified': False}
    raw = row['created_at']
    stamp = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
    # Existing call_events created_at is UTC, timestamp without timezone in PG.
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return {'status': 'observed', 'last_received_at': stamp.timestamp(), 'coverage_verified': False}
