"""Focused offline validation and speech checks; no delivery adapters."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from fastapi import HTTPException
import travel_research as research
from travel_research_contract import (
    RESULT_SCHEMA, digest, result_schema, source_manifest,
    validate_verifier_output, verification_input,
)


class NaturalSpeechTests(unittest.TestCase):
    def prepare(self, qualifiers="", text="Adult tickets cost £16.50 without a donation."):
        now = datetime(2026, 11, 26, 19, tzinfo=timezone.utc).timestamp()
        brief = {"required_fields": ["price"], "visit_date": "2026-11-26"}
        page = "Adult tickets cost £16.50 without a donation. Last admission is at 8.30pm."
        snapshot = {"reference": "source-1", "url": "https://venue.example/visit", "page_text": page,
                    "content_hash": digest(page), "retrieved_at": "2026-11-26T19:00:00+00:00"}
        candidate = {"visit_date": brief["visit_date"], "claims": [],
                     "proposed_urls": [snapshot["url"]], "uncertainties": [], "tool_errors": []}
        raw = {"status": "complete", "claims": [{"field": "price", "text": text, "qualifiers": qualifiers,
               "source_reference": "source-1", "quote": page}], "missing_coverage": [],
               "uncertainties": [], "destination": None}
        binding = {"request_id": "test-request", "revision": 1,
                   "brief_hash": digest(brief), "execution_id": "test-execution"}
        proof = {**binding, "state": "ready", "candidate": candidate, "candidate_hash": digest(candidate),
                 "snapshots": [snapshot], "fetch_failures": []}
        proof["source_manifest_hash"] = digest(source_manifest([snapshot], []))
        proof["verification_input_hash"] = digest(verification_input(brief, candidate, [snapshot], []))
        record = {**binding, "brief": brief, "claimed_at": now, "stages": {
            stage: {"state": "exited", "completed": True, "exit_code": 0, "thread_id": stage,
                    "output_hash": digest(candidate if stage == "research" else raw)}
            for stage in ("research", "verification")}}
        result = {"schema": RESULT_SCHEMA, **deepcopy(raw), "verification": {
            **{key: proof[key] for key in ("candidate_hash", "source_manifest_hash", "verification_input_hash")},
            "native_output_hash": digest(raw)}}
        return raw, brief, snapshot, result, record, proof, now

    def test_empty_qualifier_passes_both_validators_and_speaks_one_sentence(self):
        raw, brief, snapshot, result, record, proof, now = self.prepare()
        before = digest(result)
        validate_verifier_output(raw, brief, [snapshot])
        research.validate_result(result, record, now, proof)
        expected = "Adult tickets cost £16.50 without a donation."
        self.assertEqual(research.render_summary(result), expected)
        self.assertEqual(research.render_callback_summary(result), expected)
        self.assertEqual(digest(result), before)

    def test_material_qualifier_remains_spoken_in_both_renderers(self):
        raw, brief, snapshot, result, record, proof, now = self.prepare(
            "Without a donation; last admission is at 8.30pm.", "Adult tickets cost £16.50.")
        validate_verifier_output(raw, brief, [snapshot])
        research.validate_result(result, record, now, proof)
        expected = "Adult tickets cost £16.50. Without a donation; last admission is at 8.30pm."
        self.assertEqual(research.render_summary(result), expected)
        self.assertEqual(research.render_callback_summary(result), expected)

    def test_whitespace_only_or_oversized_qualifiers_are_rejected(self):
        for qualifier in (" ", "\n\t", "x" * 181):
            with self.subTest(qualifier=repr(qualifier[:8])):
                raw, brief, snapshot, result, record, proof, now = self.prepare(qualifier)
                with self.assertRaises(ValueError):
                    validate_verifier_output(raw, brief, [snapshot])
                with self.assertRaises(HTTPException):
                    research.validate_result(result, record, now, proof)

    def test_key_stays_required_in_plain_and_source_bound_schemas(self):
        raw, brief, snapshot, result, record, proof, now = self.prepare()
        for schema in (result_schema(["price"]), result_schema(["price"], snapshots=[snapshot], candidate=proof["candidate"])):
            items = schema["properties"]["claims"]["items"]
            for claim in items.get("anyOf", [items]):
                self.assertIn("qualifiers", claim["required"])
                self.assertEqual(claim["properties"]["qualifiers"]["minLength"], 0)
                self.assertEqual(claim["properties"]["qualifiers"]["maxLength"], 180)
        del raw["claims"][0]["qualifiers"]
        with self.assertRaises(ValueError):
            validate_verifier_output(raw, brief, [snapshot])


if __name__ == "__main__":
    unittest.main()
