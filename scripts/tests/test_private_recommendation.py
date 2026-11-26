"""Offline recommendation and Maps regressions at both validation boundaries."""
from copy import deepcopy
import unittest

from fastapi import HTTPException

import test_travel_research_backend as existing
from travel_research_contract import (
    RESULT_SCHEMA, digest, failed_result, validate_verifier_output, verifier_output,
)


class PrivateRecommendationTests(unittest.TestCase):
    # Reuse the existing isolated store, mocked providers and receipt lifecycle
    # without inheriting or rerunning the whole BackendTests suite.
    intake = existing.BackendTests.intake
    claimed = existing.BackendTests.claimed
    receipt = existing.BackendTests.receipt
    sourced = existing.BackendTests.sourced

    def setUp(self):
        existing.BackendTests.setUp(self)
        self.quotes = {
            "other_fact": "City Light is an exhibition of paintings of London.",
            "dated_availability": "26 November 2026: City Light is open from 6pm to 9pm.",
            "visitor_address": "Example Gallery, Example Street, London EC3N 4AB.",
            "visitor_entrance": "The visitor entrance is on Example Street.",
            "price": "Admission is free.",
        }
        self.page = " ".join(self.quotes.values())
        self.fetch.return_value["data"]["markdown"] = self.page

    def tearDown(self):
        self.sms.assert_not_called()
        self.vapi.assert_not_called()

    def candidate(self):
        field = self.args["required_fields"][0]
        return {
            "visit_date": self.args["visit_date"],
            "claims": [{"field": field, "text": self.quotes[field],
                        "qualifiers": "Published programme",
                        "proposed_source_urls": ["https://venue.example/visit"]}],
            "proposed_urls": ["https://venue.example/visit"],
            "uncertainties": [], "tool_errors": [],
        }

    def prepare_result(self, fields, present, *, maps=False, failed=False):
        self.args.update(question="Recommend a named exhibition for tonight.",
                         required_fields=fields)
        claim, source_request, sources = self.sourced()
        if failed:
            result = failed_result(claim["brief"], "No dated programme could be verified.")
            raw = verifier_output(result)
        else:
            missing = [{"field": field, "reason": "Not verified."}
                       for field in fields if field not in present]
            raw = {
                "status": "partial" if missing else "complete",
                "claims": [{"field": field, "text": self.quotes[field],
                            "qualifiers": "Published programme" if field in
                            ("other_fact", "dated_availability") else "Published visit details",
                            "source_reference": "source-1", "quote": self.quotes[field]}
                           for field in present],
                "missing_coverage": missing, "uncertainties": [],
                "destination": {"text": self.quotes["visitor_address"].rstrip("."),
                                "source_reference": "source-1",
                                "quote": self.quotes["visitor_address"]} if maps else None,
            }
            result = {"schema": RESULT_SCHEMA, **deepcopy(raw), "verification": {
                "candidate_hash": source_request["candidate_hash"],
                "source_manifest_hash": sources["source_manifest_hash"],
                "verification_input_hash": sources["verification_input_hash"],
                "native_output_hash": digest(raw),
            }}
        self.receipt(claim, "verification", raw)
        envelope = {**existing.binding(claim), "result": result, "result_hash": digest(result)}
        return claim, sources, raw, envelope

    def assert_both_accept(self, prepared):
        claim, sources, raw, envelope = prepared
        self.assertIs(validate_verifier_output(raw, claim["brief"], sources["snapshots"]), raw)
        self.assertTrue(self.service.result(envelope)["stored"])
        self.assertEqual(self.service.read(claim["request_id"])["result"], envelope["result"])

    def assert_both_reject(self, prepared, contract_error, backend_error=None):
        claim, sources, raw, envelope = prepared
        with self.assertRaisesRegex(ValueError, "^" + contract_error + "$"):
            validate_verifier_output(raw, claim["brief"], sources["snapshots"])
        with self.assertRaises(HTTPException) as rejected:
            self.service.result(envelope)
        self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(rejected.exception.detail, backend_error or contract_error)
        self.assertNotIn("result", self.service.read(claim["request_id"]))

    def test_logistics_only_recommendation_rejected_at_both_boundaries(self):
        self.assert_both_reject(self.prepare_result(
            ["other_fact", "dated_availability", "visitor_address"], ["visitor_address"]),
            "recommendation_requires_programme_and_dated_availability")

    def test_named_programme_without_dated_suitability_rejected_at_both_boundaries(self):
        self.assert_both_reject(self.prepare_result(
            ["other_fact", "dated_availability"], ["other_fact"]),
            "recommendation_requires_programme_and_dated_availability")

    def test_dated_suitability_without_named_programme_rejected_at_both_boundaries(self):
        self.assert_both_reject(self.prepare_result(
            ["other_fact", "dated_availability"], ["dated_availability"]),
            "recommendation_requires_programme_and_dated_availability")

    def test_core_recommendation_with_missing_optional_field_accepts_partial(self):
        self.assert_both_accept(self.prepare_result(
            ["other_fact", "dated_availability", "price"], ["other_fact", "dated_availability"]))

    def test_core_recommendation_accepts_complete(self):
        self.assert_both_accept(self.prepare_result(
            ["other_fact", "dated_availability"], ["other_fact", "dated_availability"]))

    def test_unverified_recommendation_accepts_honest_failed_result(self):
        self.assert_both_accept(self.prepare_result(
            ["other_fact", "dated_availability", "visitor_address"], [], failed=True))

    def test_sourced_address_allows_maps_without_unrequested_entrance(self):
        self.assert_both_accept(self.prepare_result(["visitor_address"], ["visitor_address"], maps=True))

    def test_explicitly_requested_missing_entrance_blocks_maps(self):
        self.assert_both_reject(self.prepare_result(
            ["visitor_address", "visitor_entrance"], ["visitor_address"], maps=True),
            "destination_needs_address_and_requested_entrance",
            "destination_requires_address_and_requested_entrance")

    def test_explicitly_requested_missing_entrance_allows_honest_null_maps(self):
        self.assert_both_accept(self.prepare_result(
            ["visitor_address", "visitor_entrance"], ["visitor_address"]))

    def test_verified_requested_entrance_and_address_allow_maps(self):
        self.assert_both_accept(self.prepare_result(
            ["visitor_address", "visitor_entrance"], ["visitor_address", "visitor_entrance"], maps=True))

    def test_maps_still_requires_verified_address_claim(self):
        self.assert_both_reject(self.prepare_result(["other_fact"], ["other_fact"], maps=True),
            "destination_needs_address_and_requested_entrance",
            "destination_requires_address_and_requested_entrance")


if __name__ == "__main__":
    unittest.main()
