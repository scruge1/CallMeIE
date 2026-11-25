"""Shared typed travel research contract; no provider or model capability."""
import hashlib
import json
import re

PROFILE_PIN = "9f85d5e789e0e6ec2a1946660c528a47ea80bb6978c549cedba3a62933118665"  # Reviewed native profile; runtime enablement and health remain separate gates.
RESULT_SCHEMA = "travel-research.result/v2"
FIELDS = ("opening_time", "closing_time", "last_admission", "adult_price_without_donation",
          "optional_donation_price", "visitor_entrance", "visitor_address", "dated_availability",
          "booking_phone", "route", "price", "other_fact")
UK_POSTCODE = re.compile(r"\b(?:GIR\s*0AA|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)
DESTINATION_RULE = (
    "Return a destination only when its exact sourced text contains one full UK postcode "
    "also present in the visitor_address quote, plus a distinctive sourced venue or street "
    "component from that address quote. A postcode alone, London plus a postcode, or a "
    "generic or directional gate/entrance label is insufficient. Confirm the address and "
    "entrance concern the venue in the original brief. Otherwise return null and explain "
    "in uncertainties that a sufficiently specific Maps destination could not be verified."
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), "invalid_contract_fields")


def short(value, maximum, empty=False):
    require(isinstance(value, str) and len(value) <= maximum and (empty or bool(value.strip())), "invalid_contract_text")
    return value


def required_fields(brief):
    fields = brief.get("required_fields")
    require(isinstance(fields, list) and 1 <= len(fields) <= len(FIELDS)
            and len(fields) == len(set(fields)) and all(field in FIELDS for field in fields), "invalid_required_fields")
    return fields


def validate_candidate(candidate, brief):
    exact(candidate, {"visit_date", "claims", "proposed_urls", "uncertainties", "tool_errors"})
    require(candidate["visit_date"] == brief["visit_date"], "wrong_candidate_date")
    fields = required_fields(brief)
    urls = candidate["proposed_urls"]
    require(isinstance(urls, list) and 1 <= len(urls) <= 4 and len(set(urls)) == len(urls), "invalid_proposed_urls")
    for url in urls:
        short(url, 1800)
    require(isinstance(candidate["claims"], list) and len(candidate["claims"]) <= len(fields), "candidate_claim_limit")
    seen = set()
    for claim in candidate["claims"]:
        exact(claim, {"field", "text", "qualifiers", "proposed_source_urls"})
        require(claim["field"] in fields and claim["field"] not in seen, "unrequested_or_duplicate_candidate_field")
        seen.add(claim["field"])
        short(claim["text"], 500)
        short(claim["qualifiers"], 300)
        require(isinstance(claim["proposed_source_urls"], list) and claim["proposed_source_urls"]
                and all(url in urls for url in claim["proposed_source_urls"]), "claim_url_not_proposed")
    for name in ("uncertainties", "tool_errors"):
        require(isinstance(candidate[name], list) and len(candidate[name]) <= 6, "candidate_note_limit")
        for item in candidate[name]:
            short(item, 300)
    return candidate


def verification_input(brief, candidate, snapshots, fetch_failures):
    return {"brief": brief, "candidate": candidate, "trusted_snapshots": snapshots,
            "fetch_failures": fetch_failures}


def source_manifest(snapshots, fetch_failures):
    return {"snapshots": snapshots, "fetch_failures": fetch_failures}


def verifier_output(result):
    return {key: result[key] for key in ("status", "claims", "missing_coverage", "uncertainties", "destination")}


def validate_maps_anchor(destination, address_quote):
    """Require a grounded location anchor; postcode syntax is not a postal lookup."""
    canonical_postcode = lambda value: re.sub(r"\s", "", value).upper()
    codes = {canonical_postcode(value) for value in UK_POSTCODE.findall(destination)}
    address_codes = {canonical_postcode(value) for value in UK_POSTCODE.findall(address_quote)}
    require(len(codes) == 1, "destination_requires_full_uk_postcode")
    require(codes <= address_codes, "destination_postcode_not_in_address_evidence")
    words = lambda value: " ".join(re.findall(r"[^\W_]+", value.casefold()))
    evidence = " " + words(UK_POSTCODE.sub("", address_quote)) + " "
    generic = set("the a an and of at in to on for london uk united kingdom england great britain "
                  "postcode postal code address venue museum station park hotel restaurant shop "
                  "street road avenue square lane place north south east west northeast northwest "
                  "southeast southwest main side front rear back upper lower public visitor visitors "
                  "entrance entry gate gates door doors exit reception".split())
    for part in re.split(r"[,;\n]", UK_POSTCODE.sub("", destination)):
        component = words(part)
        distinctive = any(word.isalpha() and len(word) >= 3 and word not in generic
                          for word in component.split())
        if distinctive and " " + component + " " in evidence:
            return
    raise ValueError("destination_requires_sourced_place_or_street")


def candidate_schema(fields):
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    claim = obj({"field": {"type": "string", "enum": fields}, "text": string, "qualifiers": string,
                 "proposed_source_urls": strings})
    return obj({"visit_date": string, "claims": {"type": "array", "items": claim}, "proposed_urls": strings,
                "uncertainties": strings, "tool_errors": strings})


def result_schema(fields):
    """Schema for the verifier's raw output; trusted adapter adds hash envelope."""
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    string = {"type": "string"}
    claim = obj({"field": {"type": "string", "enum": fields}, "text": string, "qualifiers": string,
                 "source_reference": string, "quote": string})
    missing = obj({"field": {"type": "string", "enum": fields}, "reason": string})
    destination = obj({"text": string, "source_reference": string, "quote": string})
    return obj({"status": {"type": "string", "enum": ["complete", "partial", "failed"]},
                "claims": {"type": "array", "items": claim}, "missing_coverage": {"type": "array", "items": missing},
                "uncertainties": {"type": "array", "items": string},
                "destination": {"description": DESTINATION_RULE,
                                "anyOf": [{"type": "null"}, destination]}})


def failed_result(brief, reason):
    reason = short(reason, 300)
    return {"schema": RESULT_SCHEMA, "status": "failed", "claims": [],
            "missing_coverage": [{"field": field, "reason": reason} for field in required_fields(brief)],
            "uncertainties": [reason], "destination": None, "verification": None}


def validate_verifier_output(value, brief, snapshots):
    """Fail before persisting a return which the independent backend must reject."""
    exact(value, {"status", "claims", "missing_coverage", "uncertainties", "destination"})
    fields = required_fields(brief)
    require(value["status"] in ("complete", "partial", "failed"), "invalid_verifier_status")
    require(isinstance(value["claims"], list) and len(value["claims"]) <= len(fields)
            and isinstance(value["missing_coverage"], list), "invalid_verifier_claims")
    sources = {source["reference"]: source for source in snapshots}
    normalize = lambda text: " ".join(text.split()).casefold()
    present, missing = set(), set()
    for claim in value["claims"]:
        exact(claim, {"field", "text", "qualifiers", "source_reference", "quote"})
        require(claim["field"] in fields and claim["field"] not in present, "duplicate_or_unrequested_verifier_field")
        present.add(claim["field"])
        for field, limit in (("text", 220), ("qualifiers", 180), ("quote", 500)):
            short(claim[field], limit)
        source = sources.get(claim["source_reference"])
        require(source is not None and normalize(claim["quote"]) in normalize(source["page_text"]), "verifier_quote_not_in_snapshot")
    for item in value["missing_coverage"]:
        exact(item, {"field", "reason"})
        require(item["field"] in fields and item["field"] not in present and item["field"] not in missing, "duplicate_or_unrequested_missing_field")
        missing.add(item["field"])
        short(item["reason"], 300)
    require(present | missing == set(fields), "incomplete_verifier_coverage")
    require(isinstance(value["uncertainties"], list) and len(value["uncertainties"]) <= 6, "verifier_uncertainty_limit")
    for text in value["uncertainties"]:
        short(text, 300)
    if value["status"] == "failed":
        require(not present and value["destination"] is None and value["uncertainties"], "dishonest_failed_verifier_result")
        return value
    require(present and (value["status"] == "complete") == (not missing), "verifier_status_coverage_mismatch")
    destination = value["destination"]
    if destination is not None:
        exact(destination, {"text", "source_reference", "quote"})
        require({"visitor_entrance", "visitor_address"} <= present, "destination_needs_entrance_and_address")
        short(destination["text"], 200)
        short(destination["quote"], 500)
        source = sources.get(destination["source_reference"])
        require(source is not None and normalize(destination["text"]) in normalize(destination["quote"])
                and normalize(destination["quote"]) in normalize(source["page_text"]), "verifier_destination_not_supported")
        address = next(claim for claim in value["claims"] if claim["field"] == "visitor_address")
        validate_maps_anchor(destination["text"], address["quote"])
    speech = " ".join(c["text"].rstrip(" .") + ". " + c["qualifiers"].rstrip(" .") + "." for c in value["claims"])
    if value["missing_coverage"]:
        speech += " I could not verify " + ", ".join(c["field"].replace("_", " ") for c in value["missing_coverage"]) + "."
    if value["uncertainties"]:
        speech += " Unverified: " + "; ".join(value["uncertainties"]) + "."
    require(len(speech) <= 900, "verifier_spoken_summary_budget")
    return value
