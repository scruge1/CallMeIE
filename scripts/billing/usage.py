"""Read-only provider observations. This is not a billing or AI-time ledger."""
from datetime import datetime
import math


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


def project_usage(snapshot, *, now, window_start, tenant_bindings=None, terminal_observations=None):
    """Group a bounded provider snapshot by provider line ID, without PII."""
    observed = snapshot.get("observed_at")
    available = snapshot.get("status") == "ok"
    fresh = available and isinstance(observed, (int, float)) and 0 <= now - observed <= 90
    records = snapshot.get("calls", []) if available else []
    terminal = terminal_observations if isinstance(terminal_observations, dict) else {}
    terminal_ok = (terminal.get('status') == 'ok' and isinstance(terminal.get('call_ids'), list)
                   and len(terminal['call_ids']) <= 100
                   and all(isinstance(value, str) and value for value in terminal['call_ids']))
    terminal_ids = set(terminal['call_ids']) if terminal_ok else set()
    unique, conflicts, rejected = {}, set(), 0
    # Ignore duplicates only when their usage-bearing fields agree. Conflicting
    # observations must not be resolved by arbitrary response order.
    fields = ("phoneNumberId", "assistantId", "status", "startedAt", "endedAt")
    bindings = tenant_bindings or {"status": "unavailable", "rows": []}
    tenants_by_assistant = {}
    if bindings.get("status") == "ok":
        for row in bindings["rows"]:
            for assistant in row["assistant_ids"]:
                tenants_by_assistant.setdefault(assistant, {}).setdefault(row["tenant_slug"], set()).add(row["display_name"])
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            rejected += 1
            continue
        identity = record["id"]
        signature = tuple(record.get(field) for field in fields)
        if identity in unique and unique[identity][0] != signature:
            conflicts.add(identity)
        else:
            unique[identity] = (signature, record)
    groups = {}
    outside_window = 0
    for identity, (_, record) in unique.items():
        if identity in conflicts:
            continue
        start, end = _timestamp(record.get("startedAt")), _timestamp(record.get("endedAt"))
        if start is not None and end is not None and start <= end <= window_start:
            outside_window += 1
            continue
        line = record.get("phoneNumberId")
        line = line if isinstance(line, str) and line else None
        assistant = record.get("assistantId")
        matches = tenants_by_assistant.get(assistant, {}) if isinstance(assistant, str) else {}
        tenant = next(iter(matches)) if len(matches) == 1 else None
        names = matches.get(tenant, set())
        name = next(iter(names)) if len(names) == 1 else tenant
        allocation = ("configuration_unavailable" if bindings.get("status") != "ok" else
                      "configured_match" if tenant else "conflicting" if matches else "unassigned")
        group = groups.setdefault((line, tenant, allocation), {
            "line_id": line, "tenant_id": None, "hotel_id": None,
            "configured_tenant_id": tenant, "configured_tenant_name": name,
            "allocation_status": allocation,
            "completed_calls": 0, "completed_provider_minutes": 0.0,
            "observed_active_calls": 0, "active_provider_minutes_estimate": 0.0 if fresh and terminal_ok else None,
            "active_state_status": 'observed' if terminal_ok else 'unavailable',
            "ended_without_timing_calls": 0,
            "unknown_calls": 0, "confirmed_ai_minutes": None,
            "billable_minutes": None, "allowance_remaining_minutes": None,
        })
        if start is not None and end is not None and start <= end <= now:
            group["completed_calls"] += 1
            group["completed_provider_minutes"] += max(0, end - max(start, window_start)) / 60
        elif identity in terminal_ids:
            # A committed end does not supply duration. Do not resurrect the
            # call or invent a completed minute total from receipt timestamps.
            group["unknown_calls"] += 1
            group["ended_without_timing_calls"] += 1
        elif terminal_ok and start is not None and start <= now and end is None and record.get("endedAt") is None and record.get("status") == "in-progress":
            group["observed_active_calls"] += 1
            if fresh:
                group["active_provider_minutes_estimate"] += max(0, observed - max(start, window_start)) / 60
        else:
            group["unknown_calls"] += 1
    for group in groups.values():
        for field in ("completed_provider_minutes", "active_provider_minutes_estimate"):
            if group[field] is not None:
                group[field] = round(group[field], 2)
    return {
        "status": "unavailable" if not available else ("fresh" if fresh else "stale"),
        "reason": snapshot.get("reason"), "observed_at": observed,
        "window_start": window_start, "window_end": now, "timezone": "UTC",
        "coverage": "partial", "query_scope": (
            "calls_updated_since_utc_midnight" if snapshot.get("query_scope") == "calls_updated_since_utc_midnight"
            else "calls_created_since_utc_midnight"),
        "limit_reached": available and len(records) >= 100,
        "carryover_calls_included": snapshot.get("query_scope") == "calls_updated_since_utc_midnight",
        "outside_window_calls": outside_window,
        "conflicting_calls": len(conflicts), "rejected_records": rejected,
        "lines": sorted(groups.values(), key=lambda group: (group["line_id"] or "", group["configured_tenant_id"] or "", group["allocation_status"])),
        "tenant_binding_status": bindings.get("status", "unavailable"),
        "terminal_guard_status": 'ok' if terminal_ok else 'unavailable',
        "confirmed_ai_minutes": None, "billable_minutes": None,
        "allowance_remaining_minutes": None,
    }
