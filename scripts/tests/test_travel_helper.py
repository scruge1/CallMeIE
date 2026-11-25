"""Exercise call ownership, provider effects, source binding and retries locally."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import travel_helper as helper


class TravelHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "test.db"

        @contextmanager
        def db():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        self.db = db
        with db() as conn:
            conn.execute("CREATE TABLE call_events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, call_id TEXT, event_type TEXT, assistant TEXT, summary TEXT, detail TEXT, event_key TEXT)")
            conn.execute("CREATE UNIQUE INDEX idx_call_events_event_key ON call_events(event_key)")

        def admin(token):
            if token != "test-admin":
                raise HTTPException(401)

        self.sms_calls = []
        self.transfer_calls = []
        self.search_calls = []
        self.sms_response = {"ok": True, "sid": "SM" + "a" * 32, "status": "queued"}
        self.sms_error = None
        self.transfer_response = {"accepted": True}
        self.search_response = [{"title": "Example venue — official booking", "url": "https://venue.example/visit", "markdown": "Our booking line is 020 7123 4567. Open 10am to 6pm. Tickets from £25 online."}]
        self.vapi_assistant = {"id": "helper-id", "model": {"provider": "openai", "model": "existing-model", "messages": [{"role": "system", "content": "Existing prompt"}], "toolIds": ["saved-other-tool"], "tools": [{"type": "endCall"}, {"type": "function", "function": {"name": "otherTool"}}]}}
        self.vapi_readback_modifier = None
        self.direction_references = {}

        async def sms(to, body, from_number=""):
            self.sms_calls.append({"to": to, "body": body, "from": from_number})
            if self.sms_error:
                raise self.sms_error
            return copy.deepcopy(self.sms_response)

        async def transfer(call, number):
            self.transfer_calls.append({"url": call["monitor"]["controlUrl"], "number": number})
            return copy.deepcopy(self.transfer_response)

        async def search(query, area):
            self.search_calls.append({"query": query, "area": area})
            return copy.deepcopy(self.search_response)

        async def vapi(path, method="GET", payload=None):
            self.assertEqual(path, "assistant/helper-id")
            if method == "PATCH":
                self.vapi_assistant.update(copy.deepcopy(payload))
            result = copy.deepcopy(self.vapi_assistant)
            if self.vapi_readback_modifier:
                self.vapi_readback_modifier(result)
            return result

        app = FastAPI()
        helper.install(app, db, admin, send_sms=sms, transfer=transfer, search=search, vapi_provider=vapi)
        self.client = TestClient(app)
        self.config = {"assistant_id": "helper-id", "parent_assistant_id": "parent-id", "phone_number_id": "phone-id", "label": "London travel helper", "area": "London, United Kingdom", "enabled": True}
        response = self.client.put(self.admin("/config"), json={"config": self.config, "revision": 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.secret = helper.settings(db, "test-helper")["webhook_secret"]

    def admin(self, suffix=""):
        return "/admin/api/travel-helper/test-helper" + suffix + "?token=test-admin"

    def event(self, name="sendDirections", args=None, tool_id="tool-1", call_id="call-1"):
        return {"message": {"type": "tool-calls", "call": {"id": call_id, "assistantId": "parent-id", "phoneNumberId": "phone-id", "type": "inboundPhoneCall", "customer": {"number": "+353871234567"}, "monitor": {"controlUrl": "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/call-1/control"}}, "toolCallList": [{"id": tool_id, "function": {"name": name, "arguments": json.dumps(args or {"destination": "Example venue, London", "travel_mode": "walking"})}}]}}

    def directions(self, args=None, tool_id="tool-1", call_id="call-1"):
        if call_id not in self.direction_references:
            self.direction_references[call_id] = self.lookup(call_id, "directions-source-" + call_id)["sources"][0]["reference"]
        values = {"destination": "Example venue, London", "travel_mode": "walking"} if args is None else args
        return self.event("sendDirections", {"source_reference": self.direction_references[call_id], **values}, tool_id, call_id)

    def post(self, event):
        return self.client.post("/vapi/travel-helper/test-helper", json=event, headers={"x-travel-helper-secret": self.secret})

    def result(self, event):
        response = self.post(event)
        self.assertEqual(response.status_code, 200, response.text)
        self.last_row = response.json()["results"][0]
        self.assertEqual(set(self.last_row) & {"result", "error"}, {"error"} if "error" in self.last_row else {"result"})
        return json.loads(self.last_row.get("result", self.last_row.get("error")))

    def lookup(self, call_id="call-1", tool_id="lookup-1"):
        return self.result(self.event("lookupTravel", {"query": "Example venue London official booking phone"}, tool_id, call_id))

    def transfer_event(self, reference, **overrides):
        args = {"source_reference": reference, "phone_number": "020 7123 4567", "confirmed": True}
        args.update(overrides)
        return self.event("transferToBooking", args, "transfer-1")

    def test_admin_requires_auth_and_never_returns_secret(self):
        self.assertEqual(self.client.get("/admin/api/travel-helper/test-helper").status_code, 401)
        response = self.client.get(self.admin())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.secret, response.text)
        self.assertEqual(response.json()["providers"], {"sms": True, "transfer": True, "search": True})

    def test_config_revision_conflict_preserves_secret(self):
        self.assertEqual(self.client.put(self.admin("/config"), json={"config": self.config, "revision": 0}).status_code, 409)
        updated = {**self.config, "label": "Updated label"}
        self.assertEqual(self.client.put(self.admin("/config"), json={"config": updated, "revision": 1}).json()["revision"], 2)
        self.assertEqual(helper.settings(self.db, "test-helper")["webhook_secret"], self.secret)

    def test_wrong_auth_assistant_and_phone_rejected(self):
        event = self.directions()
        self.assertEqual(self.client.post("/vapi/travel-helper/test-helper", json=event).status_code, 401)
        event["message"]["call"]["assistantId"] = "unrelated-assistant"
        self.assertEqual(self.post(event).status_code, 403)
        event["message"]["call"]["assistantId"] = "helper-id"
        event["message"]["call"]["phoneNumberId"] = "demo-phone"
        self.assertEqual(self.post(event).status_code, 403)
        self.assertFalse(self.sms_calls)

    def test_maps_encoded_destination_and_default_device_location(self):
        destination = "King's Cross & St Pancras, London"
        self.search_response[0]["title"] = "King’s Cross & St Pancras"
        result = self.result(self.directions(args={"destination": destination, "travel_mode": "walking"}))
        query = parse_qs(urlsplit(result["map_url"]).query)
        self.assertEqual(query["destination"], [destination])
        self.assertEqual(query["api"], ["1"])
        self.assertEqual(query["dir_action"], ["navigate"])
        self.assertNotIn("origin", query)
        self.assertEqual(self.sms_calls[0]["to"], "+353871234567")
        self.assertEqual(self.sms_calls[0]["from"], "CALLMEIE")
        self.assertEqual(result["status"], "accepted")
        self.assertIs(result["delivery_confirmed"], False)

    def test_explicit_route_planning_origin(self):
        self.search_response[0]["title"] = "Tower Bridge"
        result = self.result(self.directions(args={"destination": "Tower Bridge, London", "travel_mode": "transit", "start_location": "Paddington Station, London"}))
        self.assertEqual(parse_qs(urlsplit(result["map_url"]).query)["origin"], ["Paddington Station, London"])

    def test_model_cannot_override_known_caller_recipient(self):
        result = self.result(self.directions(args={"destination": "Example, London", "confirmed_mobile": "+447911123456", "mobile_confirmed": True}))
        self.assertEqual(result["error"], "recipient_must_match_caller")
        self.assertFalse(self.sms_calls)

    def test_unknown_caller_requires_confirmed_valid_mobile(self):
        event = self.directions(args={"destination": "Example, London", "confirmed_mobile": "+353861234567"})
        event["message"]["call"]["customer"] = {}
        first = self.result(event)
        self.assertEqual(first["error"], "ask_for_and_confirm_mobile")
        self.assertIs(first["retry_allowed"], False)
        self.assertEqual(first["retry_condition"], "caller_confirms_valid_mobile")
        self.assertIn("wait for explicit confirmation", first["guidance"])
        self.assertNotIn("unavailable on this call", first["guidance"])
        self.assertFalse(self.sms_calls)
        event["message"]["toolCallList"][0]["id"] = "tool-2"
        args = {"source_reference": self.direction_references["call-1"], "destination": "Example venue, London", "confirmed_mobile": "+353861234567", "mobile_confirmed": False}
        event["message"]["toolCallList"][0]["function"]["arguments"] = json.dumps(args)
        self.assertIs(self.result(event)["retry_allowed"], False)
        self.assertFalse(self.sms_calls)
        event["message"]["toolCallList"][0]["id"] = "tool-3"
        args["mobile_confirmed"] = True
        event["message"]["toolCallList"][0]["function"]["arguments"] = json.dumps(args)
        self.assertEqual(self.result(event)["status"], "accepted")
        self.assertEqual(len(self.sms_calls), 1)
        self.assertEqual(self.sms_calls[0]["to"], "+353861234567")

    def test_landline_caller_can_supply_confirmed_mobile(self):
        event = self.directions(args={"destination": "Example, London", "confirmed_mobile": "+447911123456", "mobile_confirmed": True})
        event["message"]["call"]["customer"]["number"] = "+442071234567"
        self.assertEqual(self.result(event)["status"], "accepted")
        self.assertEqual(self.sms_calls[0]["to"], "+447911123456")

    def test_mocked_provider_never_claims_success(self):
        self.sms_response = {"status": "mocked", "ok": True, "http_status": 200}
        result = self.result(self.directions())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "sms_not_accepted_by_provider")
        self.assertIn("error", self.last_row)
        self.assertNotIn("result", self.last_row)

    def test_explicit_mock_flag_cannot_fake_a_real_sid(self):
        self.sms_response["mocked"] = True
        result = self.result(self.directions())
        self.assertEqual(result["error"], "sms_not_accepted_by_provider")
        self.assertIn("error", self.last_row)
        self.assertNotIn("result", self.last_row)

    def test_failed_provider_delivery_remains_failed(self):
        self.sms_response["status"] = "undelivered"
        self.assertEqual(self.result(self.directions())["status"], "failed")
        self.assertIn("error", self.last_row)

    def test_provider_delivery_proof_distinct_from_accepted(self):
        self.sms_response["status"] = "delivered"
        result = self.result(self.directions())
        self.assertEqual(result["status"], "delivered")
        self.assertIs(result["delivery_confirmed"], True)
        self.assertIn("result", self.last_row)
        self.assertNotIn("error", self.last_row)

    def test_duplicate_tool_does_not_send_twice(self):
        first = self.result(self.directions())
        second = self.result(self.directions())
        self.assertEqual(first, second)
        self.assertEqual(len(self.sms_calls), 1)

    def test_changed_payload_reused_tool_id_is_rejected(self):
        self.result(self.directions())
        result = self.result(self.directions(args={"destination": "Other destination, London"}))
        self.assertEqual(result["error"], "idempotency_conflict")
        self.assertEqual(len(self.sms_calls), 1)

    def test_timeout_retained_as_uncertain_and_never_retried(self):
        self.sms_error = httpx.ReadTimeout("response lost")
        first = self.result(self.directions())
        self.sms_error = None
        self.assertEqual(self.result(self.directions()), first)
        self.assertEqual(first["status"], "uncertain")
        self.assertIs(first["retry_allowed"], False)
        self.assertIn("outcome is unknown", first["guidance"])
        self.assertNotIn("retry_condition", first)
        self.assertEqual(len(self.sms_calls), 1)
        self.assertIn("error", self.last_row)
        self.assertNotIn("result", self.last_row)

    def test_directions_require_a_completed_lookup_reference(self):
        result = self.result(self.event())
        self.assertEqual(result["error"], "invalid_source_reference")
        self.assertIn("error", self.last_row)
        self.assertFalse(self.sms_calls)

    def test_directions_reject_another_calls_source(self):
        source = self.lookup("other-call")["sources"][0]
        event = self.event(args={"source_reference": source["reference"], "destination": "Example venue, London", "travel_mode": "walking"})
        self.assertEqual(self.result(event)["error"], "lookup_destination_again")
        self.assertFalse(self.sms_calls)

    def test_directions_reject_stale_source(self):
        event = self.directions()
        with self.db() as db:
            row = db.execute("SELECT id,detail FROM call_events WHERE event_type='travel-helper-source'").fetchone()
            detail = json.loads(row["detail"])
            detail["checked_at"] = time.time() - helper.SOURCE_TTL - 1
            db.execute("UPDATE call_events SET detail=? WHERE id=?", (json.dumps(detail), row["id"]))
        self.assertEqual(self.result(event)["error"], "lookup_destination_again")
        self.assertFalse(self.sms_calls)

    def test_directions_accept_sourced_address_and_postcode_formatting(self):
        self.search_response[0].update(title="The British Museum", markdown="The British Museum\nGreat Russell Street\nLondon WC1B 3DG")
        destination = "British Museum, Great Russell St, London WC1B3DG, UK"
        result = self.result(self.directions(args={"destination": destination, "travel_mode": "walking"}))
        self.assertEqual(result["status"], "accepted")
        self.assertIn("result", self.last_row)
        self.assertEqual(parse_qs(urlsplit(result["map_url"]).query)["destination"], [destination])

    def test_directions_reject_unsourced_venue_address_and_postcode(self):
        self.search_response[0].update(title="British Museum", markdown="Great Russell Street\nLondon WC1B 3DG")
        for index, destination in enumerate(("Tate Modern, London", "British Museum, Wrong Road, London", "British Museum, Great Russell Street, London SW1A 1AA", "London, UK")):
            result = self.result(self.directions(args={"destination": destination, "travel_mode": "walking"}, tool_id="unsupported-" + str(index)))
            self.assertEqual(result["error"], "destination_not_supported_by_source_use_sourced_name_and_address")
            self.assertIn("error", self.last_row)
        self.assertFalse(self.sms_calls)

    def test_directions_do_not_use_url_only_destination_evidence(self):
        self.search_response = [{"title": "Visitor information", "url": "https://venue.example/British-Museum-London-WC1B3DG", "markdown": "See https://venue.example/British-Museum-London-WC1B3DG for information."}]
        result = self.result(self.directions(args={"destination": "British Museum, London WC1B3DG", "travel_mode": "walking"}))
        self.assertEqual(result["error"], "destination_not_supported_by_source_use_sourced_name_and_address")
        self.assertFalse(self.sms_calls)

    def test_directions_do_not_use_www_url_only_destination_evidence(self):
        self.search_response = [{"title": "Visitor information", "url": "https://venue.example/visit", "markdown": "See www.british-museum.org/London-WC1B3DG for information."}]
        result = self.result(self.directions(args={"destination": "British Museum, London WC1B3DG", "travel_mode": "walking"}))
        self.assertEqual(result["error"], "destination_not_supported_by_source_use_sourced_name_and_address")
        self.assertFalse(self.sms_calls)

    def test_saint_in_venue_name_is_not_normalised_to_street(self):
        self.search_response[0].update(title="St Paul’s Cathedral", markdown="St Paul’s Churchyard, London EC4M 8AD")
        self.assertEqual(self.result(self.directions(args={"destination": "St Paul's Cathedral, St Paul's Churchyard, London EC4M8AD", "travel_mode": "walking"}))["status"], "accepted")
        result = self.result(self.directions(args={"destination": "Street Paul's Cathedral, London", "travel_mode": "walking"}, tool_id="wrong-saint"))
        self.assertEqual(result["error"], "destination_not_supported_by_source_use_sourced_name_and_address")
        self.assertEqual(len(self.sms_calls), 1)

    def test_lookup_and_directions_in_same_batch_never_send(self):
        reference = hashlib.sha256(self.search_response[0]["url"].encode()).hexdigest()[:20]
        for index, reverse in enumerate((False, True)):
            call_id = "batch-call-" + str(index)
            event = self.event("lookupTravel", {"query": "Example venue official address London"}, "batch-lookup", call_id)
            send = self.event("sendDirections", {"source_reference": reference, "destination": "Example venue, London", "travel_mode": "walking"}, "batch-send", call_id)["message"]["toolCallList"][0]
            event["message"]["toolCallList"].append(send)
            if reverse:
                event["message"]["toolCallList"].reverse()
            response = self.post(event)
            self.assertEqual(response.status_code, 200)
            row = next(row for row in response.json()["results"] if row["toolCallId"] == "batch-send")
            self.assertNotIn("result", row)
            self.assertEqual(json.loads(row["error"])["error"], "wait_for_lookup_result_before_sending_directions")
        self.assertFalse(self.sms_calls)

    def test_directions_tool_has_source_dependency_and_deterministic_speech(self):
        tools = helper.tool_definitions("test-helper", "test-secret")
        tool = next(t for t in tools if t["function"]["name"] == "sendDirections")
        self.assertIs(tool["async"], False)
        self.assertIn("source_reference", tool["function"]["parameters"]["required"])
        self.assertIn("bot_say", tool["function"]["parameters"]["required"])
        self.assertEqual(tool["function"]["parameters"]["properties"]["bot_say"]["type"], "string")
        self.assertEqual(tool["function"]["parameters"]["properties"]["bot_say"]["maxLength"], 550)
        self.assertEqual(tool["messages"], [
            {"type": "request-start", "content": ""},
            {"type": "request-complete", "role": "assistant", "content": "The directions text has been submitted. Open the link when it arrives.", "endCallAfterSpokenEnabled": False},
            {"type": "request-failed", "role": "assistant", "content": "I cannot confirm a directions text was sent. I will not retry it automatically. Would you like spoken directions?", "endCallAfterSpokenEnabled": False},
        ])
        self.assertTrue(all("messages" not in t for t in tools if t["function"]["name"] != "sendDirections"))

    def test_fresh_tool_id_cannot_resend_uncertain_same_sms(self):
        self.sms_error = httpx.ReadTimeout("response lost")
        self.assertEqual(self.result(self.directions())["status"], "uncertain")
        self.sms_error = None
        self.assertEqual(self.result(self.directions(tool_id="new-model-tool-id"))["status"], "uncertain")
        self.assertEqual(len(self.sms_calls), 1)

    def test_fresh_tool_id_cannot_redial_uncertain_same_number(self):
        source = self.lookup()["sources"][0]
        self.transfer_response = {"accepted": False, "status": "uncertain"}
        event = self.transfer_event(source["reference"])
        self.assertEqual(self.result(event)["status"], "uncertain")
        event["message"]["toolCallList"][0]["id"] = "new-model-tool-id"
        self.transfer_response = {"accepted": True}
        self.assertEqual(self.result(event)["status"], "uncertain")
        self.assertEqual(len(self.transfer_calls), 1)

    def test_source_bound_booking_transfer(self):
        source = self.lookup()["sources"][0]
        result = self.result(self.transfer_event(source["reference"]))
        self.assertEqual(result["status"], "transfer_requested")
        self.assertIs(result["connected"], False)
        self.assertEqual(self.transfer_calls, [{"url": "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/call-1/control", "number": "+442071234567"}])

    def test_transfer_requires_current_calls_source(self):
        source = self.lookup("another-call")["sources"][0]
        result = self.result(self.transfer_event(source["reference"]))
        self.assertEqual(result["error"], "lookup_booking_number_again")
        self.assertFalse(self.transfer_calls)

    def test_transfer_requires_unexpired_source(self):
        source = self.lookup()["sources"][0]
        with self.db() as db:
            row = db.execute("SELECT id,detail FROM call_events WHERE event_type='travel-helper-source'").fetchone()
            detail = json.loads(row["detail"])
            detail["checked_at"] = time.time() - helper.SOURCE_TTL - 10
            db.execute("UPDATE call_events SET detail=? WHERE id=?", (json.dumps(detail), row["id"]))
        self.assertEqual(self.result(self.transfer_event(source["reference"]))["error"], "lookup_booking_number_again")
        self.assertFalse(self.transfer_calls)

    def test_transfer_rejects_number_absent_from_source(self):
        source = self.lookup()["sources"][0]
        result = self.result(self.transfer_event(source["reference"], phone_number="020 7000 0000"))
        self.assertEqual(result["error"], "booking_number_not_found_in_source")
        self.assertFalse(self.transfer_calls)

    def test_transfer_needs_explicit_agreement(self):
        source = self.lookup()["sources"][0]
        result = self.result(self.transfer_event(source["reference"], confirmed="true"))
        self.assertEqual(result["error"], "ask_caller_before_transfer")
        self.assertFalse(self.transfer_calls)

    def test_transfer_rejects_premium_and_emergency_numbers(self):
        for index, number in enumerate(("09012345678", "08451234567", "999", "+448712345678", "+447012345678")):
            event = self.transfer_event("not-needed", phone_number=number)
            event["message"]["toolCallList"][0]["id"] = "transfer-" + str(index)
            self.assertEqual(self.result(event)["error"], "unsupported_or_premium_booking_number")
        self.assertFalse(self.transfer_calls)

    def test_transfer_control_url_cannot_target_other_host(self):
        source = self.lookup()["sources"][0]
        for index, url in enumerate(("http://api.vapi.ai/call/x/control", "https://api.vapi.ai.evil.example/call/x/control", "https://api.vapi.ai@127.0.0.1/call/x/control", "https://127.0.0.1/call/x/control", "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/another-call/control", "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai:bad/call-1/control")):
            event = self.transfer_event(source["reference"])
            event["message"]["toolCallList"][0]["id"] = "transfer-" + str(index)
            event["message"]["call"]["monitor"]["controlUrl"] = url
            self.assertEqual(self.result(event)["error"], "call_transfer_control_unavailable")
        self.assertFalse(self.transfer_calls)

    def test_web_simulation_cannot_send_or_transfer(self):
        for index, kind in enumerate(("webCall", "vapi.websocketCall")):
            event = self.directions(tool_id="web-send-" + str(index))
            event["message"]["call"]["type"] = kind
            result = self.result(event)
            self.assertEqual(result["error"], "outbound_action_requires_bound_phone_call")
            self.assertIs(result["retry_allowed"], False)
            self.assertIn("Texting is unavailable on this call", result["guidance"])
            self.assertIn("Another mobile number cannot fix this", result["guidance"])
            self.assertNotIn("retry_condition", result)
        self.assertFalse(self.sms_calls)

    def test_bound_call_rejection_cannot_recover_by_confirming_another_mobile(self):
        event = self.directions()
        event["message"]["call"]["type"] = "vapi.websocketCall"
        event["message"]["call"].pop("phoneNumberId")
        event["message"]["call"]["customer"] = {}
        first = self.result(event)
        self.assertIs(first["retry_allowed"], False)
        self.assertIn("Do not ask for another number or call sendDirections again", first["guidance"])
        self.assertIn("Continue with verified information or spoken directions", first["guidance"])
        event["message"]["toolCallList"][0]["id"] = "new-id-after-failure"
        args = json.loads(event["message"]["toolCallList"][0]["function"]["arguments"])
        args.update(confirmed_mobile="+447700900124", mobile_confirmed=True)
        event["message"]["toolCallList"][0]["function"]["arguments"] = json.dumps(args)
        self.assertEqual(self.result(event), first)
        self.assertIn("error", self.last_row)
        self.assertFalse(self.sms_calls)

    def test_known_web_simulations_can_lookup_without_phone(self):
        for index, kind in enumerate(("webCall", "vapi.websocketCall")):
            event = self.event("lookupTravel", {"query": "Example venue official hours"}, "web-lookup-" + str(index))
            event["message"]["call"]["type"] = kind
            event["message"]["call"]["assistantId"] = "helper-id"
            event["message"]["call"].pop("phoneNumberId")
            self.assertEqual(self.result(event)["status"], "ok")

    def test_tool_publisher_preserves_model_prompt_and_other_tools(self):
        response = self.client.post(self.admin("/publish-tools"))
        self.assertEqual(response.status_code, 200, response.text)
        model = self.vapi_assistant["model"]
        self.assertEqual(model["model"], "existing-model")
        self.assertEqual(model["messages"], [{"role": "system", "content": "Existing prompt"}])
        self.assertEqual(model["toolIds"], ["saved-other-tool"])
        self.assertEqual(len(model["tools"]), 5)
        self.assertEqual(model["tools"][0]["type"], "endCall")
        self.assertNotIn(self.secret, response.text)
        self.assertEqual(self.client.post(self.admin("/publish-tools")).status_code, 200)
        self.assertEqual(len(self.vapi_assistant["model"]["tools"]), 5)

    def test_reuses_existing_event_store_and_only_adds_config_table(self):
        self.lookup()
        self.result(self.directions())
        with self.db() as db:
            tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            rows = db.execute("SELECT event_key,event_type,assistant FROM call_events").fetchall()
        self.assertEqual(tables - {"sqlite_sequence"}, {"call_events", "travel_helper_settings"})
        self.assertTrue(all(row["event_key"].startswith("travel-helper:") for row in rows))
        self.assertTrue(all(row["assistant"] == "helper-id" for row in rows))

    def test_tool_publication_allows_provider_defaults(self):
        def augment(assistant):
            for tool in assistant["model"]["tools"]:
                if tool.get("server"):
                    tool["server"]["backoffPlan"] = {"type": "fixed", "maxRetries": 0}
                    tool["function"]["strict"] = False
        self.vapi_readback_modifier = augment
        self.assertEqual(self.client.post(self.admin("/publish-tools")).status_code, 200)

    def test_tool_publication_requires_secret_and_timeout_readback(self):
        for field in ("headers", "timeoutSeconds"):
            def remove_field(assistant):
                for tool in assistant["model"]["tools"]:
                    if tool.get("server"):
                        tool["server"].pop(field, None)
            self.vapi_readback_modifier = remove_field
            response = self.client.post(self.admin("/publish-tools"))
            self.assertEqual(response.status_code, 502)
            self.assertNotIn(self.secret, response.text)

    def test_oversized_body_rejected(self):
        response = self.client.post("/vapi/travel-helper/test-helper", content="x" * 1048577, headers={"x-travel-helper-secret": self.secret})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.sms_calls)


class TransferAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.call = {"id": "call-1", "status": "in-progress", "type": "inboundPhoneCall", "assistantId": "parent-id", "phoneNumberId": "phone-id", "customer": {"number": "+353871234567"}, "monitor": {"controlUrl": "https://phone-call-websocket.aws-us-west-2-backend-production1.vapi.ai/call-1/control"}}
        self.provider = AsyncMock(return_value=copy.deepcopy(self.call))
        self.client = AsyncMock()
        self.client.post.return_value = httpx.Response(200)
        self.factory = patch.object(helper.httpx, "AsyncClient")
        self.mock_factory = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.mock_factory.return_value.__aenter__.return_value = self.client
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transfer = helper.control_transfer_adapter(self.provider)

    async def test_refreshes_owned_call_before_one_no_redirect_post(self):
        self.provider.return_value["assistantId"] = "helper-id"
        result = await self.transfer(self.call, "+442071234567")
        self.assertIs(result["accepted"], True)
        self.provider.assert_awaited_once_with("call/call-1")
        self.mock_factory.assert_called_once_with(timeout=10, follow_redirects=False)
        self.client.post.assert_awaited_once_with(self.call["monitor"]["controlUrl"], headers={}, json={"type": "transfer", "destination": {"type": "number", "number": "+442071234567"}, "content": "I'll connect you to the booking office now."})

    async def test_stale_or_changed_call_never_transfers(self):
        for key, value in (("status", "ended"), ("id", "other-call"), ("phoneNumberId", "other-phone"), ("customer", {"number": "+447911123456"})):
            self.provider.return_value = {**copy.deepcopy(self.call), key: value}
            with self.assertRaises(ValueError):
                await self.transfer(self.call, "+442071234567")
        self.client.post.assert_not_awaited()

    async def test_refresh_uses_fresh_trusted_control_url(self):
        fresh = "https://phone-call-websocket.other-production.vapi.ai/call-1/control"
        self.provider.return_value["monitor"]["controlUrl"] = fresh
        await self.transfer(self.call, "+442071234567")
        self.assertEqual(self.client.post.call_args.args[0], fresh)

    async def test_required_control_auth_never_uses_private_key(self):
        self.provider.return_value["monitorPlan"] = {"controlAuthenticationEnabled": True}
        os.environ["VAPI_API_KEY"] = "private-api-key-must-not-reach-control"
        with self.assertRaisesRegex(ValueError, "call_control_public_key_required"):
            await self.transfer(self.call, "+442071234567")
        self.client.post.assert_not_awaited()
        os.environ["VAPI_PUBLIC_KEY"] = "public-control-key"
        await self.transfer(self.call, "+442071234567")
        self.assertEqual(self.client.post.call_args.kwargs["headers"], {"Authorization": "Bearer public-control-key"})

    async def test_non_success_is_uncertain_without_retry(self):
        self.client.post.return_value = httpx.Response(502)
        result = await self.transfer(self.call, "+442071234567")
        self.assertIs(result["accepted"], False)
        self.assertEqual(result["status"], "uncertain")
        self.assertIs(result["retry_allowed"], False)
        self.client.post.assert_awaited_once()

    async def test_transfer_timeout_is_not_retried(self):
        self.client.post.side_effect = httpx.ReadTimeout("response lost")
        with self.assertRaises(httpx.ReadTimeout):
            await self.transfer(self.call, "+442071234567")
        self.client.post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
