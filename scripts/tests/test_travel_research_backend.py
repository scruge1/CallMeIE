"""Offline request, evidence, expiry and side-effect boundaries; no live I/O."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import travel_research as research
from travel_research_contract import (RESULT_SCHEMA, digest, failed_result, validate_verifier_output,
                                      verification_input, verifier_output, result_schema, candidate_schema, validate_candidate)


import travel_helper as helper


def binding(claim):
    return {k: claim[k] for k in ("request_id", "revision", "brief_hash", "execution_id")}


def empty_receipt(stage="research"):
    return {"attempt_id": stage + "-attempt", "state": "attempted", "pid": None, "process_created": None,
            "thread_id": None, "turn_id": None, "exit_code": None, "completed": False, "output_hash": None}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.db"
        @contextmanager
        def db():
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback(); raise
            finally:
                connection.close()
        self.db = db
        with db() as connection:
            connection.execute("CREATE TABLE call_events (id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, event_type TEXT, assistant TEXT, summary TEXT, detail TEXT, event_key TEXT UNIQUE)")
        helper.initialise_store(db)
        self.config = {"assistant_id": "helper-id", "parent_assistant_id": "parent-id", "phone_number_id": "phone-id", "label": "Travel", "area": "London", "enabled": True}
        helper.save_config(db, "fixture", self.config, 0)
        self.clock = datetime(2026, 11, 25, 12, tzinfo=timezone.utc).timestamp()
        self.policy = research.Policy(enabled=True, qualified=True, worker_secret="worker-test", delivery_secret="delivery-test", helper="fixture", profile_pin="fixture-profile", official_hosts=("venue.example",))
        self.call = {"id": "inbound-1", "type": "inboundPhoneCall", "assistantId": "parent-id", "phoneNumberId": "phone-id", "customer": {"number": "+447700900123", "name": "Example"}, "status": "ended", "endedAt": "2026-11-25T12:00:00Z"}
        self.profile = {"id": "helper-id", "voice": {"provider": "11labs", "voiceId": "existing-voice", "model": "eleven_v4_turbo"}, "model": {"provider": "openai", "model": "gpt-4.1-mini", "tools": [{"type": "function"}]}, "serverUrl": "https://must-not-inherit.example", "serverMessages": ["tool-calls"]}
        async def provider(path, method="GET", payload=None):
            if path == "call/inbound-1" and method == "GET": return deepcopy(self.call)
            if path == "assistant/helper-id" and method == "GET": return deepcopy(self.profile)
            if path == "call" and method == "POST": return {"id": "callback-id"}
            raise AssertionError((path, method))
        self.vapi = AsyncMock(side_effect=provider)
        self.sms = AsyncMock(return_value={"sid": "SM" + "a" * 32, "status": "queued", "ok": True, "http_status": 201})
        self.page = "26 November 2026: opens 09:00. Entrance and address: Example Gate, Example Street, London EC3N 4AB."
        self.fetch = AsyncMock(return_value={"success": True, "data": {"markdown": self.page, "metadata": {"url": "https://venue.example/visit", "statusCode": 200}}, "creditsUsed": 1})
        self.service = research.Research(db, self.policy, sms=self.sms, vapi=self.vapi, fetch=self.fetch, clock=lambda: self.clock)
        self.service.heartbeat({"healthy": True, "profile_pin": self.policy.profile_pin})
        self.args = {"question": "Check opening, entrance, address and dated availability.", "visit_date": "2026-11-26", "location": "Waterloo, London", "travel_mode": "walking", "required_fields": ["opening_time", "visitor_entrance", "visitor_address", "dated_availability"], "expected_revision": 0, "sms_consent": True, "callback_consent": True, "consent_evidence": "Yes, text me directions and call once."}
        app = FastAPI(); helper.install(app, db, lambda token: None, research=self.service)
        self.client = TestClient(app)

    def intake(self, args=None, call=None):
        return self.service.intake("fixture", self.config, call or self.call, args or self.args)

    def claimed(self):
        saved = self.intake()
        return self.service.claim({**{k: saved[k] for k in ("request_id", "revision")}, "brief_hash": self.service.read(saved["request_id"])["brief_hash"], "execution_id": "exec-1"})

    def receipt(self, claim, stage, value):
        receipt = empty_receipt(stage)
        self.service.receipt({**binding(claim), "stage": stage, "receipt": receipt})
        receipt.update(state="exited", pid=101 if stage == "research" else 102, process_created="123", thread_id=stage + "-thread", turn_id=stage + "-turn", exit_code=0, completed=True, output_hash=digest(value))
        self.service.receipt({**binding(claim), "stage": stage, "receipt": receipt})
        return receipt

    def candidate(self):
        return {"visit_date": "2026-11-26", "claims": [{"field": "opening_time", "text": "Opens at 09:00.", "qualifiers": "26 November schedule", "proposed_source_urls": ["https://venue.example/visit"]}], "proposed_urls": ["https://venue.example/visit"], "uncertainties": [], "tool_errors": []}

    def sourced(self):
        claim = self.claimed(); candidate = self.candidate(); self.receipt(claim, "research", candidate)
        request = {**binding(claim), "fetch_attempt_id": "fetch-1", "candidate": candidate, "candidate_hash": digest(candidate)}
        return claim, request, asyncio.run(self.service.sources(request))

    def ready(self):
        claim, source_request, sources = self.sourced()
        raw = {"status": "partial", "claims": [
            {"field": "opening_time", "text": "Opens at 09:00.", "qualifiers": "26 November schedule", "source_reference": "source-1", "quote": "26 November 2026: opens 09:00."},
            {"field": "visitor_entrance", "text": "Use Example Gate.", "qualifiers": "Official entrance", "source_reference": "source-1", "quote": "Entrance and address: Example Gate, Example Street, London EC3N 4AB."},
            {"field": "visitor_address", "text": "Example Street, London EC3N 4AB.", "qualifiers": "Published address", "source_reference": "source-1", "quote": "Example Gate, Example Street, London EC3N 4AB."}],
            "missing_coverage": [{"field": "dated_availability", "reason": "Dated ticket inventory was not obtained."}], "uncertainties": ["Ticket availability remains unverified."],
            "destination": {"text": "Example Gate, Example Street, London EC3N 4AB", "source_reference": "source-1", "quote": "Example Gate, Example Street, London EC3N 4AB."}}
        validate_verifier_output(raw, claim["brief"], sources["snapshots"])
        self.receipt(claim, "verification", raw)
        result = {"schema": RESULT_SCHEMA, **raw, "verification": {"candidate_hash": source_request["candidate_hash"], "source_manifest_hash": sources["source_manifest_hash"], "verification_input_hash": sources["verification_input_hash"], "native_output_hash": digest(raw)}}
        return claim, {**binding(claim), "result": result, "result_hash": digest(result)}

    def deliver(self, claim):
        return asyncio.run(self.service.deliver(self.service.delivery_body(self.service.read(claim["request_id"]))))

    def test_committed_intake_preserves_original_identity_and_fields(self):
        saved = self.intake(); stored = self.service.read(saved["request_id"])
        self.assertEqual(stored["identity"]["customer"], self.call["customer"])
        self.assertEqual(stored["identity"]["original_customer_number"], self.call["customer"]["number"])
        self.assertEqual(stored["brief"]["required_fields"], self.args["required_fields"])
        self.assertEqual(stored["brief"]["official_hosts"], list(self.policy.official_hosts))
        self.assertNotIn("consent", saved)

    def test_default_sms_on_original_landline_keeps_requested_callback(self):
        for number in ("+35361788870", "+442079460123"):
            call = {**self.call, "id": "landline-" + number[-4:], "customer": {"number": number}}
            saved = self.intake(call=call)
            record = self.service.read(saved["request_id"])
            self.assertEqual(record["identity"]["customer"]["number"], number)
            self.assertTrue(record["consent"]["callback"])
            self.assertTrue(record["consent"]["sms_requested"])
            self.assertFalse(record["consent"]["sms"])
            self.assertEqual(record["consent"]["sms_unavailable_reason"], "original_caller_is_landline_callback_preserved")

    def test_landline_sms_only_does_not_infer_callback_or_another_number(self):
        call = {**self.call, "customer": {"number": "+35361788870"}}
        with self.assertRaisesRegex(ValueError, "ask_for_and_confirm_mobile"):
            self.intake(args={**self.args, "callback_consent": False}, call=call)
        with self.assertRaisesRegex(ValueError, "ask_for_and_confirm_mobile"):
            self.intake(args={**self.args, "confirmed_mobile": "+447700900123", "mobile_confirmed": False}, call=call)

    def test_callback_only_is_allowed_but_no_consent_is_rejected(self):
        saved = self.intake({**self.args, "sms_consent": False})
        self.assertFalse(self.service.read(saved["request_id"])["consent"]["sms"])
        with self.assertRaises(HTTPException): self.intake({**self.args, "sms_consent": False, "callback_consent": False})

    def test_callback_only_uses_original_ie_or_uk_landline_without_sms_bypass(self):
        for number in ("+35361788870", "+35312345678", "+442079460123", "+441234567890"):
            call = {**self.call, "id": "line-" + number[1:], "customer": {"number": number}}
            saved = self.intake({**self.args, "sms_consent": False}, call)
            self.assertEqual(self.service.read(saved["request_id"])["identity"]["customer"]["number"], number)
            with self.assertRaises((HTTPException, ValueError)):
                self.intake({**self.args, "sms_consent": True, "callback_consent": False}, {**call, "id": "sms-" + number[1:]})
            with self.assertRaises((HTTPException, ValueError)):
                self.intake({**self.args, "sms_consent": False, "confirmed_mobile": "+447700900999", "mobile_confirmed": True}, {**call, "id": "alternate-" + number[1:]})

    def test_callback_only_rejects_premium_foreign_and_anonymous_without_existing_fallback(self):
        for number in ("+449001234567", "+19001234567", "anonymous", "+353818123456"):
            with self.subTest(number=number), self.assertRaises((HTTPException, ValueError)):
                self.intake({**self.args, "sms_consent": False}, {**self.call, "customer": {"number": number}})

    def test_original_valid_mobile_cannot_be_replaced(self):
        with self.assertRaises((HTTPException, ValueError)):
            self.intake({**self.args, "confirmed_mobile": "+447700900999", "mobile_confirmed": True})

    def test_fallback_requires_confirmation_and_freezes_original_context(self):
        call = {**self.call, "customer": {"number": "anonymous"}}
        with self.assertRaises((HTTPException, ValueError)): self.intake(call=call)
        saved = self.intake({**self.args, "confirmed_mobile": "+447700900999", "mobile_confirmed": True}, call)
        record = self.service.read(saved["request_id"])
        self.assertEqual(record["identity"]["customer"]["number"], "+447700900999")
        self.assertEqual(record["identity"]["original_customer_number"], "anonymous")

    def test_inbound_scope_and_current_health_remain_required(self):
        for change in ({"type": "webCall"}, {"type": "outboundPhoneCall"}, {"phoneNumberId": "other"}, {"assistantId": "other"}):
            with self.subTest(change=change), self.assertRaises(HTTPException): self.intake(call={**self.call, **change})
        self.clock += 46
        with self.assertRaises(HTTPException): self.intake()

    def test_profile_and_source_scope_remain_inactive_by_default(self):
        with patch.object(research, "PROFILE_PIN", None), patch.dict(os.environ, {"TRAVEL_RESEARCH_ENABLED": "1", "TRAVEL_RESEARCH_QUALIFIED": "1"}):
            self.assertFalse(research.Policy.environment().qualified)
        self.assertFalse(research.Research(self.db, replace(self.policy, official_hosts=())).admitted("fixture"))

    def test_competing_claims_have_one_owner(self):
        saved = self.intake(); current = self.service.read(saved["request_id"])
        def take(execution):
            try: return self.service.claim({**{k: current[k] for k in ("request_id", "revision", "brief_hash")}, "execution_id": execution})
            except HTTPException: return None
        with ThreadPoolExecutor(2) as pool: results = list(pool.map(take, ("a", "b")))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_late_same_claim_replay_does_not_launch(self):
        claim = self.claimed(); self.clock += 481
        self.service.heartbeat({"healthy": True, "profile_pin": self.policy.profile_pin})
        replay = self.service.claim(binding(claim))
        self.assertFalse(replay["launch_allowed"]); self.assertTrue(replay["expired"])
        with self.assertRaises(HTTPException): self.service.claim({**binding(claim), "execution_id": "new"})

    def test_unknown_never_launched_failure_can_be_recorded_after_expiry(self):
        claim = self.claimed(); self.clock += 481
        unknown = {**empty_receipt(), "state": "unknown"}
        self.assertTrue(self.service.receipt({**binding(claim), "stage": "research", "receipt": unknown})["expired"])
        result = failed_result(claim["brief"], "Launch permission was not confirmed; no replacement was started.")
        ack = self.service.result({**binding(claim), "result": result, "result_hash": digest(result)})
        self.assertTrue(ack["no_effects"]); self.assertEqual(ack["delivery_state"], "expired")

    def test_actual_native_receipt_journal_replays_after_expiry_without_new_attempt_or_effect(self):
        import travel_research_native as native
        claim = self.claimed(); directory = Path(self.temp.name) / "late-journal"; directory.mkdir()
        attempted = empty_receipt()
        running = {**attempted, "state": "running", "pid": 101, "process_created": "123"}
        terminal = {**running, "state": "unknown"}
        for receipt in (attempted, running, terminal): native.record_receipt(directory, claim, "research", receipt)
        native.persist_return(directory, claim, failed_result(claim["brief"], "Research deadline exceeded."))
        self.clock += 481
        observed = []
        class BackendTransport:
            def post(inner, operation, body):
                reply = getattr(self.service, operation)(body)
                observed.append((operation, reply))
                return reply
        native.flush_journal(directory, claim, BackendTransport(), include_result=True)
        self.assertEqual(len(observed), 4)
        self.assertTrue(all(reply["stored"] and reply["expired"] and reply["no_effects"] for _, reply in observed))
        self.assertIsNone(native.pending_message(directory, claim))
        self.assertEqual(self.service.read(claim["request_id"])["delivery_state"], "expired")
        self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_source_proof_is_backend_fetched_once_and_bound_to_original_brief(self):
        claim, request, response = self.sourced()
        self.assertEqual(response, asyncio.run(self.service.sources(request)))
        self.fetch.assert_awaited_once_with("https://venue.example/visit")
        self.assertEqual(response["snapshots"][0]["page_text"], self.page)
        self.assertEqual(response["verification_input_hash"], digest(verification_input(claim["brief"], request["candidate"], response["snapshots"], response["fetch_failures"])))
        self.assertNotIn("customer", json.dumps(response))

    def test_model_page_text_and_social_urls_cannot_become_proof(self):
        claim = self.claimed(); candidate = self.candidate(); candidate["sources"] = [{"page_text": "invented"}]
        self.receipt(claim, "research", candidate)
        with self.assertRaises(HTTPException):
            asyncio.run(self.service.sources({**binding(claim), "fetch_attempt_id": "fetch-1", "candidate": candidate, "candidate_hash": digest(candidate)}))
        for url in ("https://facebook.com/venue", "http://venue.example/visit", "https://venue.example@evil.example/x", "https://venue.example:444/x", "https://127.0.0.1/x"):
            with self.subTest(url=url), self.assertRaises(HTTPException): research.official_url(url, self.policy.official_hosts)
        self.fetch.assert_not_called()

    def test_concurrent_fetch_replay_is_pending_and_never_refetches(self):
        claim = self.claimed(); candidate = self.candidate(); self.receipt(claim, "research", candidate)
        request = {**binding(claim), "fetch_attempt_id": "fetch-1", "candidate": candidate, "candidate_hash": digest(candidate)}
        async def exercise():
            started, release = asyncio.Event(), asyncio.Event()
            async def fetch(url):
                started.set(); await release.wait(); return self.fetch.return_value
            self.fetch.side_effect = fetch
            active = asyncio.create_task(self.service.sources(request)); await started.wait()
            duplicate = await self.service.sources(request)
            self.assertEqual(duplicate["status"], "pending")
            release.set(); return await active
        self.assertEqual(asyncio.run(exercise())["status"], "ready")
        self.fetch.assert_awaited_once()

    def test_fetch_failure_remains_evidence_not_invented_absence(self):
        self.fetch.side_effect = asyncio.TimeoutError()
        claim, request, sources = self.sourced()
        self.assertEqual(sources["snapshots"], []); self.assertEqual(len(sources["fetch_failures"]), 1)
        with self.assertRaises(HTTPException): self.service.receipt({**binding(claim), "stage": "verification", "receipt": empty_receipt("verification")})
        self.fetch.assert_awaited_once()

    def test_off_domain_resolved_page_is_rejected(self):
        self.fetch.return_value["data"]["metadata"]["url"] = "https://evil.example/visit"
        _, _, sources = self.sourced()
        self.assertFalse(sources["snapshots"]); self.assertTrue(sources["fetch_failures"])

    def test_modified_result_source_and_brief_bindings_are_rejected(self):
        claim, envelope = self.ready()
        changes = [lambda r: r["verification"].update(candidate_hash="a" * 64),
                   lambda r: r["verification"].update(source_manifest_hash="a" * 64),
                   lambda r: r["verification"].update(verification_input_hash="a" * 64),
                   lambda r: r["claims"][0].update(text="Invented hours"),
                   lambda r: r["claims"][0].update(quote="Invented source text"),
                   lambda r: r.update(missing_coverage=[]), lambda r: r.update(status="complete"),
                   lambda r: r.update(sources=[{"page_text": "forged"}])]
        for change in changes:
            altered = deepcopy(envelope); change(altered["result"]); altered["result_hash"] = digest(altered["result"])
            with self.subTest(change=change), self.assertRaises(HTTPException): self.service.result(altered)

    def test_actual_qualification_five_url_failure_remains_rejected_and_schema_now_bounds_it(self):
        # Exact URL list from failed native qualification v1, raw output SHA
        # b1c26106deeb664def7d1f3633294afcf180264b20ce4b350a3f5ef09cca29c5.
        # This is a structural regression fixture, never verified travel data.
        urls = ["https://www.hrp.org.uk/tower-of-london/visit/opening-and-closing-times/",
                "https://www.hrp.org.uk/tower-of-london/visit/tickets-and-prices/",
                "https://www.hrp.org.uk/tower-of-london/visit/",
                "https://www.hrp.org.uk/tower-of-london/whats-on/ceremony-of-the-keys/",
                "https://booking.hrp.org.uk/TOL/tickets/select?is-return=false&market=HRP&product=TOL"]
        candidate = self.candidate(); candidate["proposed_urls"] = urls
        candidate["claims"][0]["proposed_source_urls"] = [urls[0]]
        brief = {"visit_date": "2026-11-26", "required_fields": self.args["required_fields"],
                 "official_hosts": ["hrp.org.uk", "www.hrp.org.uk"]}
        with self.assertRaisesRegex(ValueError, "invalid_proposed_urls"):
            validate_candidate(candidate, brief)
        schema = candidate_schema(brief["required_fields"])
        self.assertGreater(len(urls), schema["properties"]["proposed_urls"]["maxItems"])
        self.assertIn("official_hosts", schema["description"])
        # Staying within four URLs must not permit the failed, unapproved booking host.
        candidate["proposed_urls"] = [urls[0], urls[1], urls[2], urls[4]]
        with self.assertRaisesRegex(ValueError, "candidate_url_outside_trusted_hosts"):
            validate_candidate(candidate, brief)

    def test_candidate_membership_and_trusted_host_boundaries_remain_enforced(self):
        claim = self.claimed(); candidate = self.candidate()
        candidate["claims"][0]["proposed_source_urls"].append("https://venue.example/unselected")
        with self.assertRaisesRegex(ValueError, "claim_url_not_proposed"):
            validate_candidate(candidate, claim["brief"])
        for url in ("https://booking.venue.example/tickets", "https://venue.example@evil.example/", "https://venue.example/visit#invented"):
            candidate = self.candidate(); candidate["proposed_urls"] = [url]
            candidate["claims"][0]["proposed_source_urls"] = [url]
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "candidate_url_outside_trusted_hosts"):
                validate_candidate(candidate, claim["brief"])

    def test_output_schemas_expose_runtime_bounds_before_generation(self):
        fields = self.args["required_fields"]
        candidate = candidate_schema(fields)["properties"]
        result = result_schema(fields)["properties"]
        self.assertEqual(candidate["claims"]["maxItems"], len(fields))
        self.assertEqual(candidate["claims"]["items"]["properties"]["field"]["enum"], fields)
        self.assertEqual(candidate["claims"]["items"]["properties"]["text"]["maxLength"], 500)
        self.assertEqual(candidate["claims"]["items"]["properties"]["qualifiers"]["maxLength"], 300)
        self.assertEqual(candidate["uncertainties"]["maxItems"], 6)
        self.assertEqual(candidate["tool_errors"]["items"]["maxLength"], 300)
        self.assertEqual(result["claims"]["maxItems"], len(fields))
        for name, limit in (("text", 220), ("qualifiers", 180), ("quote", 500)):
            self.assertEqual(result["claims"]["items"]["properties"][name]["maxLength"], limit)
        self.assertEqual(result["uncertainties"]["maxItems"], 6)
        self.assertIn("trusted_snapshots", result["claims"]["items"]["properties"]["source_reference"]["description"])

    def test_maps_rejects_bare_gate_and_unanchored_postcode_at_both_boundaries(self):
        self.page += " Other labels: West Gate, London EC3N 4AB. Main Entrance, London EC3N 4AB."
        self.fetch.return_value["data"]["markdown"] = self.page
        claim, envelope = self.ready()
        record = self.service.read(claim["request_id"])
        proof = self.service.read(record["source_proof_key"])
        cases = [("West Gate", "destination_requires_full_uk_postcode"),
                 ("EC3N 4AB", "destination_requires_sourced_place_or_street"),
                 ("London EC3N 4AB", "destination_requires_sourced_place_or_street"),
                 ("West Gate, London EC3N 4AB", "destination_requires_sourced_place_or_street"),
                 ("Main Entrance, London EC3N 4AB", "destination_requires_sourced_place_or_street")]
        for text, reason in cases:
            with self.subTest(destination=text):
                altered = deepcopy(envelope)
                altered["result"]["destination"].update(text=text, quote=text)
                # Even an address quote containing the generic gate label must not suffice.
                altered["result"]["claims"][2]["quote"] = self.page.split("Other labels: ", 1)[1]
                altered["result_hash"] = digest(altered["result"])
                with self.assertRaisesRegex(ValueError, reason):
                    validate_verifier_output(verifier_output(altered["result"]), claim["brief"], proof["snapshots"])
                with self.assertRaises(HTTPException) as rejected:
                    self.service.result(altered)
                self.assertEqual(rejected.exception.detail, reason)
        self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_maps_postcode_must_be_in_visitor_address_evidence(self):
        self.fetch.return_value["data"]["markdown"] += " Another venue: Example Street, London SW7 5BD."
        claim, envelope = self.ready()
        record = self.service.read(claim["request_id"])
        proof = self.service.read(record["source_proof_key"])
        for address_quote in ("Example Street", "Example Street, London SW7 5BD"):
            with self.subTest(address_quote=address_quote):
                altered = deepcopy(envelope)
                altered["result"]["claims"][2]["quote"] = address_quote
                altered["result_hash"] = digest(altered["result"])
                with self.assertRaisesRegex(ValueError, "destination_postcode_not_in_address_evidence"):
                    validate_verifier_output(verifier_output(altered["result"]), claim["brief"], proof["snapshots"])
                with self.assertRaises(HTTPException) as rejected:
                    self.service.result(altered)
                self.assertEqual(rejected.exception.detail, "destination_postcode_not_in_address_evidence")

    def test_maps_accepts_exact_sourced_address_and_full_postcode(self):
        claim, envelope = self.ready()
        record = self.service.read(claim["request_id"])
        proof = self.service.read(record["source_proof_key"])
        validate_verifier_output(verifier_output(envelope["result"]), claim["brief"], proof["snapshots"])
        self.assertTrue(self.service.result(envelope)["stored"])
        self.assertIn("EC3N 4AB", self.service.read(claim["request_id"])["result"]["destination"]["text"])
        description = result_schema(claim["brief"]["required_fields"])["properties"]["destination"]["description"]
        self.assertIn("full UK postcode", description)
        self.assertIn("return null", description)

    def test_maps_null_with_honest_uncertainty_remains_valid(self):
        claim, envelope = self.ready()
        record = self.service.read(claim["request_id"])
        proof = self.service.read(record["source_proof_key"])
        result = deepcopy(envelope["result"])
        result["destination"] = None
        result["uncertainties"].append("A sufficiently specific Maps destination could not be verified.")
        raw = verifier_output(result)
        validate_verifier_output(raw, claim["brief"], proof["snapshots"])
        result["verification"]["native_output_hash"] = digest(raw)
        record["stages"]["verification"]["output_hash"] = digest(raw)
        research.validate_result(result, record, self.clock, proof)

    def test_same_result_and_terminal_receipt_replay_after_expiry_acknowledge_no_effects(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        record = self.service.read(claim["request_id"]); self.clock += 481
        self.assertTrue(self.service.result(envelope)["no_effects"])
        self.assertTrue(self.service.receipt({**binding(claim), "stage": "verification", "receipt": record["stages"]["verification"]})["expired"])
        self.assertEqual(asyncio.run(self.service.advance_pending())["processed"], 0)
        self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_late_new_result_is_retained_for_audit_without_delivery(self):
        claim, envelope = self.ready(); self.clock += 481
        self.assertEqual(self.service.result(envelope)["delivery_state"], "expired")
        self.assertEqual(self.service.read(claim["request_id"])["result_hash"], envelope["result_hash"])
        self.deliver(claim); self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_superseded_revision_retains_old_return_as_audit_without_delivery(self):
        claim, envelope = self.ready()
        self.intake({**self.args, "expected_revision": 1, "question": "Revised question"})
        acknowledgement = self.service.result(envelope)
        self.assertTrue(acknowledgement["superseded"]); self.assertTrue(acknowledgement["no_effects"])
        current = self.service.read(claim["request_id"])
        self.assertEqual(current["revision"], 2); self.assertNotIn("result", current)
        archive = self.service.read("travel-research-revision:" + digest([claim["request_id"], 1]))
        self.assertEqual(archive["result_hash"], envelope["result_hash"])
        asyncio.run(self.service.advance_pending()); self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_call_must_actually_be_ended_with_matching_original_identity(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        self.call["status"] = "in-progress"; self.call.pop("endedAt")
        self.assertEqual(self.deliver(claim)["status"], "waiting_for_inbound_end"); self.sms.assert_not_called()
        self.call.update(status="ended", endedAt="2026-11-25T12:01:00Z", phoneNumberId="other")
        with self.assertRaises(HTTPException): self.deliver(claim)
        self.sms.assert_not_called()

    def test_deadline_rechecked_after_provider_get(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        async def delayed(path, method="GET", payload=None):
            self.clock += 481; return deepcopy(self.call)
        self.vapi.side_effect = delayed
        with self.assertRaises(HTTPException): self.deliver(claim)
        self.sms.assert_not_called()

    def test_delivery_reserves_exact_effect_before_provider_and_preserves_voice(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        async def sms(*args, **kwargs):
            record = self.service.read(claim["request_id"]); reservation = record["delivery"]["sms"]
            self.assertEqual(reservation["status"], "unknown"); self.assertEqual(reservation["result_hash"], envelope["result_hash"])
            self.assertEqual(kwargs["from_number"], "CALLMEIE"); self.assertNotIn("origin=", args[1])
            self.assertNotIn("https://venue.example", args[1]); self.assertLessEqual(research.sms_segments(args[1])["segments"], 2)
            return {"sid": "SM" + "a" * 32, "status": "queued", "ok": True, "http_status": 201}
        self.sms.side_effect = sms
        result = self.deliver(claim); self.assertEqual(result["status"], "complete")
        post = next(call for call in self.vapi.call_args_list if len(call.args) > 1 and call.args[1] == "POST")
        payload = post.args[2]; assistant = payload["assistant"]
        self.assertEqual(assistant["voice"], self.profile["voice"]); self.assertEqual(assistant["model"]["model"], "gpt-4.1-mini")
        self.assertEqual(assistant["model"]["tools"], [{"type": "endCall"}]); self.assertEqual(assistant["serverMessages"], [])
        self.assertNotIn("serverUrl", assistant); self.assertNotIn("metadata", payload)
        self.assertEqual(assistant["metadata"]["result_hash"], envelope["result_hash"])
        self.assertEqual(assistant["metadata"]["request_id"], claim["request_id"])
        self.assertIn("Delivery isn't confirmed yet", assistant["firstMessage"])
        self.assertFalse(assistant["firstMessageInterruptionsEnabled"])
        self.assertNotIn("Thursday 26 November 2026", assistant["firstMessage"])
        self.assertTrue(assistant["firstMessage"].endswith("Bye for now."))
        self.assertEqual(assistant["endCallPhrases"], ["bye for now"])
        self.assertEqual(self.service.read(claim["request_id"])["brief"]["visit_date"], "2026-11-26")
        self.deliver(claim); self.sms.assert_awaited_once()
        self.assertEqual(sum(len(c.args) > 1 and c.args[1] == "POST" for c in self.vapi.call_args_list), 1)

    def test_callback_explains_bare_times_without_changing_default_summary_or_result(self):
        claim, envelope = self.ready()
        result = deepcopy(envelope["result"])
        # Deliberately reorder the fields: their meaning comes from the typed
        # field, not from a caller remembering the order of three bare times.
        result["claims"] = [{"field": name, "text": value, "qualifiers": "Daytime visit.",
                             "source_reference": "source-1", "quote": value}
                            for name, value in (("last_admission", "15:00."),
                                ("opening_time", "09:00."), ("closing_time", "16:30."))]
        record = self.service.read(claim["request_id"])
        record.update(result=result, result_hash=digest(result))
        reservation = {**binding(claim), "result_hash": record["result_hash"], "attempt_id": "render-only"}
        before = deepcopy(record)
        expected = ("15:00. Daytime visit. 09:00. Daytime visit. 16:30. Daytime visit. "
                    "I could not verify dated availability. Unverified: Ticket availability remains unverified..")
        self.assertEqual(research.render_summary(result), expected)
        payload = research.callback_payload(record, self.profile, reservation)
        speech = payload["assistant"]["firstMessage"]
        self.assertIn("Last admission is at 15:00.", speech)
        self.assertIn("It opens at 09:00.", speech)
        self.assertIn("It closes at 16:30.", speech)
        self.assertNotIn("dated availability", speech)
        self.assertEqual(speech.count("Ticket availability remains unverified."), 1)
        self.assertTrue(speech.endswith("Bye for now."))
        self.assertTrue(payload["assistant"]["model"]["messages"][0]["content"].endswith(speech))
        self.assertLessEqual(len(speech), 1200)
        self.assertEqual(record, before)
        self.assertEqual(payload["customer"], self.call["customer"])
        self.assertEqual(payload["assistant"]["model"]["tools"], [{"type": "endCall"}])
        self.assertEqual(research.render_summary(result), expected)
        self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_callback_only_result_uses_no_sms(self):
        self.args["sms_consent"] = False; claim, envelope = self.ready(); self.service.result(envelope)
        self.deliver(claim); self.sms.assert_not_called()
        post = next(c for c in self.vapi.call_args_list if len(c.args) > 1 and c.args[1] == "POST")
        self.assertNotIn("text", post.args[2]["assistant"]["firstMessage"].casefold())

    def test_callback_only_landline_uses_the_same_production_delivery_path(self):
        self.args["sms_consent"] = False
        self.call["customer"]["number"] = "+35361788870"
        claim, envelope = self.ready(); self.service.result(envelope); self.deliver(claim)
        self.sms.assert_not_called()
        post = next(c for c in self.vapi.call_args_list if len(c.args) > 1 and c.args[1] == "POST")
        self.assertEqual(post.args[2]["customer"]["number"], "+35361788870")
        self.assertEqual(post.args[2]["phoneNumberId"], "phone-id")

    def test_failed_research_returns_one_truthful_consented_failure_callback(self):
        claim = self.claimed(); self.service.receipt({**binding(claim), "stage": "research", "receipt": {**empty_receipt(), "state": "unknown"}})
        result = failed_result(claim["brief"], "The source lookup timed out; no facts were verified.")
        self.service.result({**binding(claim), "result": result, "result_hash": digest(result)})
        self.deliver(claim); self.sms.assert_not_called()
        post = next(c for c in self.vapi.call_args_list if len(c.args) > 1 and c.args[1] == "POST")
        self.assertIn("could not verify", post.args[2]["assistant"]["firstMessage"])
        self.assertNotIn("was sent", post.args[2]["assistant"]["firstMessage"])

    def test_unknown_provider_outcome_never_repeats_effect(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        self.sms.side_effect = TimeoutError("unknown submission")
        original = self.vapi.side_effect
        async def provider(path, method="GET", payload=None):
            if method == "POST": raise TimeoutError("unknown call creation")
            return await original(path, method, payload)
        self.vapi.side_effect = provider
        first = self.deliver(claim); self.assertEqual(first["status"], "unknown")
        for _ in range(3): self.deliver(claim); asyncio.run(self.service.advance_pending())
        self.sms.assert_awaited_once()
        self.assertEqual(sum(len(c.args) > 1 and c.args[1] == "POST" for c in self.vapi.call_args_list), 1)

    def test_sid_without_real_supported_acceptance_is_not_spoken_as_submitted(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        self.sms.return_value = {"sid": "SM" + "a" * 32, "status": "queued"}  # Missing actual adapter ok/HTTP evidence.
        response = self.deliver(claim)
        self.assertEqual(response["delivery"]["sms"]["status"], "unknown")
        post = next(c for c in self.vapi.call_args_list if len(c.args) > 1 and c.args[1] == "POST")
        self.assertIn("can't confirm", post.args[2]["assistant"]["firstMessage"])
        self.assertNotIn("I've submitted", post.args[2]["assistant"]["firstMessage"])

    def test_unrecognised_sms_status_cannot_claim_submission_even_with_ok_and_sid(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        self.sms.return_value = {"sid": "SM" + "a" * 32, "ok": True, "http_status": 201, "status": "unknown-invented-status"}
        self.assertEqual(self.deliver(claim)["delivery"]["sms"]["status"], "unknown")

    def test_duplicate_delivery_during_sms_cannot_create_callback(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        async def sms(*args, **kwargs):
            duplicate = await self.service.deliver(self.service.delivery_body(self.service.read(claim["request_id"])))
            self.assertIn("in_progress", duplicate["status"])
            self.assertFalse(any(len(c.args) > 1 and c.args[1] == "POST" for c in self.vapi.call_args_list))
            return {"sid": "SM" + "b" * 32, "status": "queued", "ok": True, "http_status": 201}
        self.sms.side_effect = sms; self.deliver(claim); self.sms.assert_awaited_once()

    def test_withdrawn_consent_suppresses_all_effects(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        old = self.service.read(claim["request_id"])
        self.service.change(claim["request_id"], old, {**old, "consent": {**old["consent"], "sms": False, "callback": False}})
        self.assertEqual(self.deliver(claim)["status"], "withdrawn"); self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_durable_pending_survives_lost_wakeup_without_duplicate_effect(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        self.sms.assert_not_called()  # Persisted result alone has not sent anything.
        asyncio.run(self.service.advance_pending()); asyncio.run(self.service.advance_pending())
        self.assertEqual(self.service.read(claim["request_id"])["delivery_state"], "complete")
        self.sms.assert_awaited_once()

    def test_existing_worker_poll_wakes_only_backend_owned_pending_delivery(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        response = self.client.post("/internal/travel-research/worker/pending",
            headers={"x-research-worker-secret": self.policy.worker_secret}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"request": None})
        self.assertEqual(self.service.read(claim["request_id"])["delivery_state"], "complete")
        self.sms.assert_awaited_once()

    def test_result_return_wakes_backend_after_commit_and_replay_does_not_send_again(self):
        claim, envelope = self.ready()
        headers = {"x-research-worker-secret": self.policy.worker_secret}
        for _ in range(2):
            response = self.client.post("/internal/travel-research/worker/result", headers=headers, json=envelope)
            self.assertEqual(response.status_code, 200); self.assertTrue(response.json()["stored"])
        self.sms.assert_awaited_once()
        self.assertEqual(sum(len(c.args) > 1 and c.args[1] == "POST" for c in self.vapi.call_args_list), 1)

    def test_oversize_sms_does_not_block_the_consented_callback(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        self.assertGreater(research.sms_segments(research.render_sms(envelope["result"], "walking"))["segments"], 1)
        self.service.policy = replace(self.policy, max_sms_segments=1)
        result = self.deliver(claim)
        self.assertEqual(result["delivery"]["sms"]["reason"], "sms_segment_limit")
        self.sms.assert_not_called()
        self.assertEqual(sum(len(c.args) > 1 and c.args[1] == "POST" for c in self.vapi.call_args_list), 1)

    def test_crashed_effect_reservation_becomes_unknown_without_replaying(self):
        claim, envelope = self.ready(); self.service.result(envelope)
        old = self.service.read(claim["request_id"])
        receipt = research.effect_reservation(old, "sms", self.clock)
        self.service.change(claim["request_id"], old, {**old, "delivery": {"sms": receipt}})
        self.clock += self.policy.provider_seconds + 6
        self.deliver(claim)
        self.assertEqual(self.service.read(claim["request_id"])["delivery_state"], "unknown")
        self.sms.assert_not_called()
        self.assertFalse(any(len(c.args) > 1 and c.args[1] == "POST" for c in self.vapi.call_args_list))

    def test_worker_cannot_select_delivery_and_source_auth_is_required(self):
        h = {"x-research-worker-secret": self.policy.worker_secret}
        self.assertEqual(self.client.post("/internal/travel-research/worker/deliver", headers=h, json={}).status_code, 404)
        self.assertEqual(self.client.post("/internal/travel-research/deliver", headers={"x-research-delivery-secret": self.policy.worker_secret}, json={}).status_code, 401)
        self.assertEqual(self.client.post("/internal/travel-research/worker/sources", json={}).status_code, 401)

    def test_tool_schema_omits_unsupported_uniqueness_but_intake_rejects_duplicates(self):
        definition = research.tool_definition("fixture", "secret")
        fields = definition["function"]["parameters"]["properties"]["required_fields"]
        self.assertNotIn("uniqueItems", fields)
        self.assertEqual(fields["minItems"], 1)
        self.assertEqual(fields["items"]["enum"], list(research.FIELDS))
        duplicated = {**self.args, "required_fields": ["opening_time", "opening_time"]}
        with self.assertRaisesRegex(ValueError, "invalid_required_fields"):
            research.required_fields(duplicated)
        with self.assertRaises(HTTPException) as rejected:
            self.intake(duplicated)
        self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(rejected.exception.detail, "invalid_required_fields")
        self.assertIsNone(self.service.pending())
        self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_fixed_intake_ack_only_selects_completion_after_durable_store(self):
        messages = {m["type"]: m for m in research.tool_definition("fixture", "secret")["messages"]}
        self.assertTrue(messages["request-complete"]["endCallAfterSpokenEnabled"])
        self.assertFalse(messages["request-failed"]["endCallAfterSpokenEnabled"])
        secret = helper.settings(self.db, "fixture")["webhook_secret"]
        body = {"message": {"type": "tool-calls", "call": self.call, "toolCallList": [{"id": "tool-1", "function": {"name": "requestTravelResearch", "arguments": self.args}}]}}
        url = "/vapi/travel-helper/fixture"; headers = {"x-travel-helper-secret": secret}
        result = self.client.post(url, headers=headers, json=body).json()["results"][0]
        self.assertIn("result", result); self.assertEqual(json.loads(result["result"])["status"], "stored")
        self.clock += 46; body["message"]["toolCallList"][0]["id"] = "tool-2"
        failed = self.client.post(url, headers=headers, json=body).json()["results"][0]
        self.assertIn("error", failed); self.assertNotIn("result", failed)

    def test_commit_failure_cannot_claim_successful_intake(self):
        real = self.db
        class Failure:
            def __init__(self, db): self.db = db
            def execute(self, *args): return self.db.execute(*args)
            def commit(self): raise OSError("commit failed")
        @contextmanager
        def bad_db():
            with real() as db: yield Failure(db)
        service = research.Research(bad_db, self.policy, clock=lambda: self.clock)
        with self.assertRaises(OSError): service.intake("fixture", self.config, self.call, self.args)
        self.sms.assert_not_called(); self.vapi.assert_not_called()

    def test_final_sms_encoding_includes_url_and_unicode(self):
        self.assertEqual(research.sms_segments("a" * 161)["segments"], 2)
        self.assertEqual(research.sms_segments("£" * 160)["segments"], 1)
        self.assertEqual(research.sms_segments("^" * 81)["segments"], 2)
        self.assertEqual(research.sms_segments("“" * 71)["encoding"], "UCS-2")
        self.assertEqual(research.sms_segments("😀" * 36)["segments"], 2)


if __name__ == "__main__":
    unittest.main()
