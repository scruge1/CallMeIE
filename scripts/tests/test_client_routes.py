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
