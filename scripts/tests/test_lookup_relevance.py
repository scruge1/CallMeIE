"""Exercise the actual rehearsal failure against local webhook/provider fakes."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

_spec = importlib.util.spec_from_file_location("travel_lookup_test_fixture", Path(__file__).with_name("test_travel_helper.py"))
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

helper = harness.helper


class LookupRelevanceTests(unittest.TestCase):
    setUp = harness.TravelHelperTests.setUp
    admin = harness.TravelHelperTests.admin
    event = harness.TravelHelperTests.event
    post = harness.TravelHelperTests.post
    result = harness.TravelHelperTests.result

    def ask(self, query="Sample History Museum visitor entrance address and opening hours", visit="2026-11-26", tool_id="lookup-test"):
        return self.result(self.event("lookupTravel", {"query": query, "visit_date": visit}, tool_id))

    def valid_page(self):
        return {"url": "https://museum.example/visit", "title": "Sample History Museum — Visit",
                "markdown": "Sample History Museum\nVisitor entrance: North Gate.\n10 Foundry Road, London AB1 2CD.\nOpen daily 10:00–17:00.\nLast admission 16:30.\nAdult tickets £20.\nBooking line: 020 7123 4567."}

    def fixture(self):
        return json.loads((Path(__file__).resolve().parents[2] / "tests/fixtures/ack_rehearsal_irrelevant_sources.json").read_text())

    def test_actual_rehearsal_pages_are_insufficient_without_more_provider_calls(self):
        fixture = self.fixture()
        self.search_response = fixture["search_items"]
        result = self.ask(**{"query": fixture["request"]["query"], "visit": fixture["request"]["visit_date"]})
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["sources"], [])
        self.assertEqual([s["reason"] for s in result["rejected_sources"]], ["dated_page_does_not_match_visit", "dated_page_does_not_match_visit", "page_error_or_access_wall"])
        self.assertEqual(len(self.search_calls), 1)
        self.assertIn("remains unverified", result["guidance"])
        self.assertIn("do not repeat", result["guidance"].lower())
        self.assertEqual(self.sms_calls, [])
        self.assertEqual(self.transfer_calls, [])

    def test_rejected_reference_cannot_authorise_bound_call_directions(self):
        fixture = self.fixture()
        self.search_response = fixture["search_items"]
        self.ask(fixture["request"]["query"], fixture["request"]["visit_date"])
        result = self.result(self.event("sendDirections", fixture["unsupported_directions"], "bad-send"))
        self.assertEqual(result["error"], "lookup_destination_again")
        self.assertEqual(self.sms_calls, [])

    def test_valid_page_preserves_reference_and_source_shape(self):
        self.search_response = [self.valid_page()]
        result = self.ask()
        self.assertEqual(result["status"], "ok")
        source = result["sources"][0]
        self.assertEqual(set(source), {"reference", "url", "title", "text", "checked_at"})
        self.assertEqual(source["reference"], hashlib.sha256(source["url"].encode()).hexdigest()[:20])
        self.assertEqual(source["text"], self.search_response[0]["markdown"])

    def test_valid_source_still_allows_existing_bound_sms_contract(self):
        self.search_response = [self.valid_page()]
        source = self.ask()["sources"][0]
        result = self.result(self.event("sendDirections", {"source_reference": source["reference"], "destination": "Sample History Museum, 10 Foundry Road, London AB1 2CD", "travel_mode": "walking"}, "good-send"))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(self.sms_calls), 1)

    def test_description_snippet_is_not_fetched_page_evidence(self):
        self.search_response = [{"url": "https://museum.example/visit", "title": "Sample History Museum", "description": "Open daily 10:00–17:00 at 10 Foundry Road."}]
        result = self.ask()
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["rejected_sources"][0]["reason"], "fetched_page_text_missing")

    def test_other_venue_hours_do_not_satisfy_named_place(self):
        page = self.valid_page()
        page["title"] = "Different Art Gallery"
        page["markdown"] = page["markdown"].replace("Sample History Museum", "Different Art Gallery")
        self.search_response = [page]
        result = self.ask()
        self.assertEqual(result["rejected_sources"][0]["reason"], "requested_place_not_in_page")

    def test_name_and_booking_widget_are_not_hours_or_address(self):
        page = self.valid_page()
        page["markdown"] = "Sample History Museum\nChoose an entry time. Select a ticket quantity. Virtual school visit sessions last 60 minutes."
        self.search_response = [page]
        result = self.ask()
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["rejected_sources"][0]["reason"], "requested_information_not_in_page")

    def test_wrong_dated_heading_rejected_even_when_place_and_hours_match(self):
        for heading in ("# Sample History Museum, 18 March 2027", "# Sample History Museum, March 18, 2027", "# Sample History Museum, 2027-03-18"):
            with self.subTest(heading=heading):
                page = self.valid_page()
                page["markdown"] = heading + "\n" + page["markdown"]
                review = helper.lookup_source_review("Sample History Museum opening hours", "2026-11-26", "London", page)
                self.assertEqual(review["reason"], "dated_page_does_not_match_visit")

    def test_matching_date_heading_is_eligible_but_not_called_verified(self):
        page = self.valid_page()
        page["markdown"] = "# Sample History Museum, 26 November 2026\n" + page["markdown"]
        review = helper.lookup_source_review("Sample History Museum opening hours", "2026-11-26", "London", page)
        self.assertTrue(review["usable"])
        self.assertIs(review["exact_visit_date_verified"], False)

    def test_open_today_alone_does_not_establish_requested_date(self):
        page = self.valid_page()
        page["markdown"] = "Sample History Museum\nOpen today 10:00–17:00."
        review = helper.lookup_source_review("Sample History Museum opening hours", "2026-11-26", "London", page)
        self.assertEqual(review["reason"], "requested_information_not_in_page")

    def test_weekly_schedule_and_other_closure_notice_are_not_whole_page_date_mismatch(self):
        page = self.valid_page()
        page["markdown"] += "\nClosed on 25 December 2026."
        review = helper.lookup_source_review("Sample History Museum opening hours", "2026-11-26", "London", page)
        self.assertTrue(review["usable"])

    def test_rejected_refresh_invalidates_prior_reference_for_both_outbound_tools(self):
        self.search_response = [self.valid_page()]
        source = self.ask()["sources"][0]
        self.search_response[0]["markdown"] = "# Page Not Found"
        self.assertEqual(self.ask(tool_id="bad-refresh")["status"], "insufficient_evidence")
        args = {"source_reference": source["reference"], "destination": "Sample History Museum, London", "travel_mode": "walking"}
        self.assertEqual(self.result(self.event("sendDirections", args, "blocked-refresh-send"))["error"], "lookup_destination_again")
        args = {"source_reference": source["reference"], "phone_number": "020 7123 4567", "confirmed": True}
        self.assertEqual(self.result(self.event("transferToBooking", args, "blocked-refresh-transfer"))["error"], "lookup_booking_number_again")
        self.assertEqual(self.sms_calls, [])
        self.assertEqual(self.transfer_calls, [])

    def test_availability_selector_without_dated_status_stays_unknown(self):
        page = self.valid_page()
        page["markdown"] += "\nSelect your tickets. Choose an entry time."
        review = helper.lookup_source_review("Sample History Museum ticket availability", "2026-11-26", "London", page)
        self.assertEqual(review["reason"], "requested_information_not_in_page")

    def test_empty_provider_result_keeps_existing_no_results_contract(self):
        self.search_response = []
        result = self.ask()
        self.assertEqual(result["status"], "no_results")
        self.assertEqual(result["sources"], [])


if __name__ == "__main__":
    unittest.main()
