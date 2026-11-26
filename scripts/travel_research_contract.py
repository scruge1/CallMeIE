"""Shared typed travel research contract; no provider or model capability."""
import hashlib
import json
import re
from urllib.parse import urlsplit

PROFILE_PIN = "9b0f1f28c66ad143b2ed3877b45c3944b6300e69b510630df7d709ce08a2f2ac"  # Reviewed native profile; runtime enablement and health remain separate gates.
RESULT_SCHEMA = "travel-research.result/v2"
FIELDS = ("opening_time", "closing_time", "last_admission", "adult_price_without_donation",
          "optional_donation_price", "visitor_entrance", "visitor_address", "dated_availability",
          "booking_phone", "route", "price", "other_fact")
UK_POSTCODE = re.compile(r"\b(?:GIR\s*0AA|[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)
DESTINATION_RULE = (
    "Return a destination only when its exact sourced text contains one full UK postcode "
    "also present in the visitor_address quote, plus a distinctive sourced venue or street "
    "component from that address quote. A postcode alone, London plus a postcode, or a "
    "generic or directional gate/entrance label is insufficient. Confirm the address "
    "concerns the venue selected to answer the original brief. A verified visitor entrance is "
    "additionally required only when visitor_entrance is explicitly requested. If these "
    "requirements are not met, return null and explain "
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
    hosts = brief.get("official_hosts")
    require(isinstance(hosts, list) and hosts and all(isinstance(host, str) and host for host in hosts),
            "trusted_source_policy_required")
    for url in urls:
        short(url, 1800)
        try:
            parsed = urlsplit(url)
            allowed = (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in (None, 443)
                       and not parsed.username and not parsed.password and not parsed.fragment)
        except ValueError:
            allowed = False
        require(allowed, "candidate_url_outside_trusted_hosts")
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
    """Expose existing validator bounds before generation, including URL policy."""
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    def string(maximum):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    url = {**string(1800), "description": "HTTPS public information page on an exact hostname in the original brief's official_hosts; no credentials, fragment, or port except 443. Subdomains are not implicitly trusted."}
    claim = obj({"field": {"type": "string", "enum": fields}, "text": string(500), "qualifiers": string(300),
                 "proposed_source_urls": {"type": "array", "minItems": 1, "maxItems": 4, "items": url,
                     "description": "A nonempty subset of the exact URLs selected in proposed_urls. Never cite a failed or unfetched booking page as support."}})
    notes = {"type": "array", "maxItems": 6, "items": string(300)}
    schema = obj({"visit_date": {**string(10), "minLength": 10, "description": "Copy the exact original brief visit_date."},
                  "claims": {"type": "array", "maxItems": len(fields), "items": claim,
                             "description": "At most one claim per requested field; do not duplicate or invent fields."},
                  "proposed_urls": {"type": "array", "minItems": 1, "maxItems": 4, "items": url,
                      "description": "Select only 1 to 4 distinct successfully retrieved official information pages in total. These are the sole pages the trusted backend will fetch. Omit failed, unfetched, checkout, login or booking-transaction URLs; put failures in tool_errors and uncertainty instead. Prefer pages covering several requested fields."},
                  "uncertainties": notes, "tool_errors": notes})
    schema["description"] = ("Bounded research candidate, not verified facts. Respect original brief official_hosts exactly. "
        "Choose no more than four source URLs for the entire result, and make every claim source URL a member of that chosen list. "
        "A failed booking fetch proves neither availability nor absence; record the failure and keep availability unverified. "
        "All text must be nonblank and within the stated limits.")
    return schema


def result_schema(fields):
    """Schema for the verifier's raw output; trusted adapter adds hash envelope."""
    def obj(properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    def string(maximum):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    reference = {**string(64), "description": "An exact reference from the supplied trusted_snapshots; a candidate URL or failed fetch is not a snapshot."}
    quote = {**string(500), "description": (
        "Copy the shortest contiguous supporting span exactly from this snapshot. "
        "Do not rewrite, splice, pad or invent characters to fit the limit. For a table, "
        "use the relevant complete row or the header through that row, preserving column "
        "meaning; do not clip into an unrelated row. If no intact span fits, mark the field unverified.")}
    claim = obj({"field": {"type": "string", "enum": fields}, "text": string(220),
                 "qualifiers": {**string(180), "description": "A concise applicable qualification, including relevant age, ticket and visit scope; never a punctuation-only placeholder."},
                 "source_reference": reference, "quote": quote})
    missing = obj({"field": {"type": "string", "enum": fields}, "reason": string(300)})
    destination = obj({"text": string(200), "source_reference": reference, "quote": quote})
    schema = obj({"status": {"type": "string", "enum": ["complete", "partial", "failed"]},
                "claims": {"type": "array", "maxItems": len(fields), "items": claim},
                "missing_coverage": {"type": "array", "maxItems": len(fields), "items": missing},
                "uncertainties": {"type": "array", "maxItems": 6, "items": string(300)},
                "destination": {"description": DESTINATION_RULE,
                                "anyOf": [{"type": "null"}, destination]}})
    schema["description"] = ("Every original requested field appears exactly once in claims or missing_coverage. "
        "Use only supplied trusted snapshots and exact quotes; unsupported or failed-fetch facts belong in missing_coverage. "
        "A visitor entrance must apply to the requested visit; an evening-event meeting point does not establish ordinary daytime admission. "
        "Complete means no missing fields; partial means useful supported facts plus missing fields; failed has no claims and null destination. "
        "Keep spoken claim text, qualifiers, missing-field explanations and uncertainties together within 750 characters, "
        "including brief punctuation and labels, so the backend's 900-character spoken-summary budget is respected.")
    if {"other_fact", "dated_availability"} <= set(fields):
        schema["description"] += (" For this recommendation request, complete or partial requires both a verified "
            "named programme and its relevance in other_fact, and dated suitability in dated_availability. "
            "Venue logistics alone are insufficient. If either is missing, return failed with no claims, "
            "all requested fields missing, null destination and an honest uncertainty explaining the failure.")
    return schema


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
    if {"other_fact", "dated_availability"} <= set(fields):
        require({"other_fact", "dated_availability"} <= present,
                "recommendation_requires_programme_and_dated_availability")
    destination = value["destination"]
    if destination is not None:
        exact(destination, {"text", "source_reference", "quote"})
        require("visitor_address" in present
                and ("visitor_entrance" not in fields or "visitor_entrance" in present),
                "destination_needs_address_and_requested_entrance")
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
