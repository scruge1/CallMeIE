"""Portable, offline checks for bounded, source-grounded verifier evidence choices."""
from copy import deepcopy
import unittest

from travel_research_contract import digest, result_schema, validate_verifier_output


class QuoteEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fields = ["other_fact", "dated_availability", "closing_time", "price", "visitor_address", "route"]
        self.title = "# Example Exhibition"
        self.dates = "Example Exhibition runs from 7 October 2026 to 14 February 2027."
        self.hours = "| Thursday | 11am–9pm |"
        self.address = "Example Gallery\n\nExample Street\n\nLondon\n\nEC3N 4AB"
        self.price = "Pay What You Can every Thursday 6–9pm."
        self.route = "Less than one minute from Example Station."
        self.sources = [
            {"reference": "source-1", "url": "https://venue.example/show", "page_text":
             self.title + "\n\n" + self.dates + "\n\n| Day | Hours |\n| --- | --- |\n" + self.hours},
            {"reference": "source-2", "url": "https://venue.example/visit", "page_text":
             self.price + "\n\n### Address\n\n" + self.address + "\n\n" + self.route},
        ]
        texts = [self.title, self.dates, "Closes tonight at 21:00.", self.price, self.address, self.route]
        self.candidate = {"visit_date": "2026-11-26", "claims": [
            {"field": field, "text": text, "proposed_source_urls": [self.sources[0 if index < 3 else 1]["url"]]}
            for index, (field, text) in enumerate(zip(self.fields, texts))]}

    def schema(self):
        return result_schema(self.fields, snapshots=self.sources, candidate=self.candidate)

    def banks(self, schema=None):
        return {branch["properties"]["source_reference"]["enum"][0]: branch["properties"]["quote"]["enum"]
                for branch in (schema or self.schema())["properties"]["claims"]["items"]["anyOf"]}

    def test_exact_spans_include_title_dates_weekday_address_price_and_route(self):
        banks = self.banks()
        for text in (self.title, self.dates, self.hours):
            self.assertIn(text, banks["source-1"])
        for text in (self.address, self.price, self.route):
            self.assertIn(" ".join(text.split()), banks["source-2"])
        for source in self.sources:
            for quote in banks[source["reference"]]:
                self.assertIn(quote, " ".join(source["page_text"].split()))
                self.assertLessEqual(len(quote), 500)
                self.assertNotIn('"', quote)
                self.assertNotIn("\n", quote)

    def test_quote_choices_are_bound_to_their_source(self):
        schema = self.schema()
        banks = self.banks(schema)
        self.assertNotIn(self.title, banks["source-2"])
        self.assertNotIn(" ".join(self.address.split()), banks["source-1"])
        destinations = schema["properties"]["destination"]["anyOf"][1:]
        for branch in destinations:
            props = branch["properties"]
            self.assertEqual(props["quote"]["enum"], banks[props["source_reference"]["enum"][0]])

    def test_malformed_quote_is_never_repaired_or_accepted(self):
        quote = self.title + "被"
        raw = {"status": "complete", "claims": [{"field": "other_fact", "text": "Example Exhibition.",
               "qualifiers": "Named programme", "source_reference": "source-1", "quote": quote}],
               "missing_coverage": [], "uncertainties": [], "destination": None}
        before = digest(raw)
        self.assertNotIn(quote, self.banks()["source-1"])
        with self.assertRaisesRegex(ValueError, "verifier_quote_not_in_snapshot"):
            validate_verifier_output(raw, {"required_fields": ["other_fact"]}, self.sources)
        self.assertEqual(digest(raw), before)

    def test_exact_title_does_not_bypass_dated_recommendation_gate(self):
        raw = {"status": "partial", "claims": [{"field": "other_fact", "text": "Example Exhibition.",
               "qualifiers": "Named programme", "source_reference": "source-1", "quote": self.title}],
               "missing_coverage": [{"field": "dated_availability", "reason": "Not verified."}],
               "uncertainties": [], "destination": None}
        with self.assertRaisesRegex(ValueError, "recommendation_requires_programme_and_dated_availability"):
            validate_verifier_output(raw, {"required_fields": ["other_fact", "dated_availability"]}, self.sources)

    def test_schema_is_deterministic_and_does_not_modify_input_or_receipt_data(self):
        before = deepcopy((self.sources, self.candidate))
        self.assertEqual(self.schema(), self.schema())
        self.assertEqual((self.sources, self.candidate), before)
        ordinary = result_schema(self.fields)
        self.assertNotIn("enum", ordinary["properties"]["claims"]["items"]["properties"]["quote"])

    def test_ascii_double_quotes_are_excluded_and_whitespace_collapses(self):
        self.sources[0]["page_text"] += '\n\nA title with "literal quotes" is excluded.\n\nAnother\t source   line.'
        banks = self.banks()
        self.assertIn("Another source line.", banks["source-1"])
        for quotes in banks.values():
            for quote in quotes:
                self.assertEqual(quote, " ".join(quote.split()))
                self.assertNotIn('"', quote)

    def test_normalized_multiline_address_passes_unchanged_validator(self):
        quote = " ".join(self.address.split())
        raw = {"status": "complete", "claims": [{"field": "visitor_address", "text": quote,
               "qualifiers": "Published visitor address", "source_reference": "source-2", "quote": quote}],
               "missing_coverage": [], "uncertainties": [],
               "destination": {"text": quote, "source_reference": "source-2", "quote": quote}}
        self.assertIs(validate_verifier_output(raw, {"required_fields": ["visitor_address"]}, self.sources), raw)

    def test_large_pages_keep_total_enum_and_character_budgets(self):
        self.sources = [{"reference": "source-" + str(i), "url": "https://venue.example/" + str(i),
                         "page_text": "\n\n".join("Example exhibition paragraph " + str(n) + ": " + "paintings " * 12 + "."
                                                   for n in range(160))} for i in range(4)]
        schema = self.schema()
        banks = self.banks(schema)
        self.assertLessEqual(sum(map(len, banks.values())), 96)
        self.assertLessEqual(sum(len(quote) for values in banks.values() for quote in values), 24000)
        def enums(value):
            if isinstance(value, dict):
                return sum(len(v) if k == "enum" else enums(v) for k, v in value.items())
            if isinstance(value, list):
                return sum(enums(v) for v in value)
            return 0
        self.assertLess(enums(schema), 500)


if __name__ == "__main__":
    unittest.main()
