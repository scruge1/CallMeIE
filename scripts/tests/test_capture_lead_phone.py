"""Caller-ID handling for Claire's demo lead capture."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


def test_capture_lead_phone_uses_call_signalling_number_first():
    body = {
        "message": {
            "call": {"customer": {"number": "+353851234567"}},
        }
    }

    assert server._capture_lead_phone(body, {"phone": "+353800000000"}) == "+353851234567"


def test_capture_lead_phone_uses_tool_argument_if_call_object_omits_it():
    body = {"message": {"call": {"id": "call-test"}}}

    assert server._capture_lead_phone(body, {"phone": "+353851234567"}) == "+353851234567"


def test_capture_lead_phone_allows_hidden_or_missing_caller_id():
    body = {"message": {"call": {"customer": {}}}}

    assert server._capture_lead_phone(body, {}) == ""
