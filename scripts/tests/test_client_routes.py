"""Anti-pattern guards for MVP customer Receptionist Today routes.

Per MVP-CUSTOMER-DASHBOARDS-DRAFT.md §6 + §7.4, the customer dashboards
must not leak any of: upgrade-CTA, scarcity language, the SaaS-default
Inter font, purple-to-blue gradients, NPS-style copy. These are executable
assertions, not aspirations — the test fails the build on regression.

Surface 1 only (Receptionist Today). The §4 edit-budget meter ships in a
follow-up commit; its own anti-pattern tests will extend this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.unit


# Forbidden substrings (case-insensitive). Match draft §6 / §7.4.
FORBIDDEN_SUBSTRINGS = [
    "upgrade your plan",
    "upgrade to",
    "limited time",
    "how does this make you feel",
    "linear-gradient(135deg, #8b5cf6",  # purple-to-blue
    "font-family: inter",
    "font-family: \"inter\"",
    "font-family: 'inter'",
    "x of y plan limit",
    "of your plan",
    "customer success score",
    "🤖",
]


def _load_html_helpers():
    """Import server.py without booting the FastAPI listener.

    server.py guards uvicorn.run() behind __name__ == '__main__'.
    """
    import server  # noqa: PLC0415

    return server


def test_client_today_html_no_anti_patterns():
    server = _load_html_helpers()
    html = server._client_today_html("test_assistant_id_long_enough", "tok123")
    assert html.strip().startswith("<!DOCTYPE html>"), "expected HTML5 doctype"
    assert "Today" in html
    lower = html.lower()
    for needle in FORBIDDEN_SUBSTRINGS:
        assert needle not in lower, (
            f"Anti-pattern leaked into client_today.html: {needle!r}"
        )
    assert "days left" not in lower
    # Empty-state copy matches brief.
    assert "no calls yet today" in lower


def test_today_filter_clause_branches():
    server = _load_html_helpers()
    clause = server._today_filter_clause()
    assert "date(" in clause.lower()
    assert "created_at" in clause


def test_html_escaping_in_template_quotes_safe():
    """Single quotes / tags / newlines in token or assistant must NOT escape
    the JS literal."""
    server = _load_html_helpers()
    html = server._client_today_html(
        "normal_id_8chars", "'; alert(1)//\n<script>x</script>"
    )
    # All dangerous chars must be removed.
    assert "<script>x</script>" not in html
    assert "ASSIST = '" in html
    assert "TOKEN  = '" in html
    a_line = html.split("ASSIST = '")[1].split("';")[0]
    assert "\n" not in a_line
    t_line = html.split("TOKEN  = '")[1].split("';")[0]
    assert "'" not in t_line
    assert "<" not in t_line


# ---------------------------------------------------------------------------
# Surface 3 — D5 edit-budget meter
# ---------------------------------------------------------------------------


def test_client_edits_html_no_anti_patterns():
    server = _load_html_helpers()
    html = server._client_edits_html("acme", "web_business", "tok123")
    assert html.strip().startswith("<!DOCTYPE html>")
    assert "Edits" in html
    lower = html.lower()
    for needle in FORBIDDEN_SUBSTRINGS:
        assert needle not in lower, (
            f"Anti-pattern leaked into client_edits.html: {needle!r}"
        )
    assert "days left" not in lower
    # 3-form-fields guard: description + screenshot + due_date, no more.
    form_inputs = (
        lower.count("<textarea")
        + lower.count('<input id="screenshot_url"')
        + lower.count('<input id="due_date"')
    )
    assert form_inputs == 3, f"expected exactly 3 form inputs, got {form_inputs}"


def test_tier_edit_cap_hours_has_all_six_tiers():
    server = _load_html_helpers()
    expected = {
        "web_launch", "web_business", "web_premium",
        "care_silver", "care_gold", "care_platinum",
    }
    assert set(server.TIER_EDIT_CAP_HOURS.keys()) == expected
    for tier, hours in server.TIER_EDIT_CAP_HOURS.items():
        assert hours > 0, f"{tier} cap must be > 0"


def test_tier_caps_drift_guard():
    """Receptionist tier-cap mirror must agree with the portal source of
    truth (hours = minutes / 60)."""
    server = _load_html_helpers()
    portal_minutes = {
        "web_launch": 30, "web_business": 120, "web_premium": 360,
        "care_silver": 30, "care_gold": 120, "care_platinum": 360,
    }
    for tier, mins in portal_minutes.items():
        expected_hours = mins / 60.0
        assert abs(server.TIER_EDIT_CAP_HOURS[tier] - expected_hours) < 0.01, (
            f"{tier}: receptionist={server.TIER_EDIT_CAP_HOURS[tier]}h, "
            f"portal={mins}min ({expected_hours}h)"
        )


def test_portal_db_503_when_env_unset(monkeypatch):
    """Edit-budget meter must 503 cleanly when PORTAL_PG_URL is unset."""
    server = _load_html_helpers()
    from fastapi import HTTPException
    monkeypatch.delenv("PORTAL_PG_URL", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        server._portal_db_connect()
    assert exc_info.value.status_code == 503
    assert "PORTAL_PG_URL" in str(exc_info.value.detail)
