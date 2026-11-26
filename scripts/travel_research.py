"""Bounded deferred research. call_events is the sole queue and effect owner.

Worker credentials can claim, bind and return research, but cannot deliver it.
Production admission stays closed until the native retrieval profile is qualified.
"""
from dataclasses import dataclass
from datetime import date, datetime
import asyncio
from copy import deepcopy
import hmac
import json
import os
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
import httpx

from travel_helper import (MOBILE_RE, decode, invoke, maps_url, recipient, settings,
                           text_arg, validate_call)
from travel_research_contract import (PROFILE_PIN, RESULT_SCHEMA, FIELDS, canonical, digest,
                                      required_fields, validate_candidate, verification_input,
                                      source_manifest, verifier_output, validate_maps_anchor)

KIND = "travel-research"
HEX = re.compile(r"[0-9a-f]{64}\Z")
OPAQUE = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
CALLBACK_LANDLINE_RE = re.compile(r"(?:\+353(?:1\d{7}|[2-79]\d{6,8})|\+44[12]\d{9})\Z")


def callback_recipient(call, args):
    """Callback-only may return to an ordinary-format original IE/UK landline.

    This is not caller authentication or carrier classification. SMS still uses
    the unchanged mobile-only recipient function. An unsuitable original number
    uses only that existing explicitly confirmed mobile fallback policy.
    """
    current = (call.get("customer") or {}).get("number")
    if isinstance(current, str) and (MOBILE_RE.fullmatch(current) or CALLBACK_LANDLINE_RE.fullmatch(current)):
        supplied = args.get("confirmed_mobile")
        if supplied and supplied != current:
            raise ValueError("recipient_must_match_caller")
        return current
    return recipient(call, args)


def require(condition, reason, code=422):
    if not condition:
        raise HTTPException(code, reason)


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), "invalid_fields")


def epoch(value):
    require(isinstance(value, str), "invalid_timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "timezone_required")
        return stamp.timestamp()
    except ValueError:
        raise HTTPException(422, "invalid_timestamp")


def audit_ack(record, now):
    expired = record["deadline"] <= now
    superseded = record.get("state") == "superseded"
    return {"stored": True, "expired": expired, "superseded": superseded,
            "no_effects": expired or superseded}


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    qualified: bool = False
    worker_secret: str = ""
    delivery_secret: str = ""
    helper: str = ""
    deadline_seconds: int = 480
    heartbeat_seconds: int = 45
    profile_pin: str = ""
    official_hosts: tuple = ()
    source_seconds: int = 25
    provider_seconds: int = 12
    max_sms_segments: int = 2

    @classmethod
    def environment(cls):
        # No environment boolean can substitute for qualification evidence.
        return cls(enabled=os.getenv("TRAVEL_RESEARCH_ENABLED") == "1",
                   qualified=PROFILE_PIN is not None,
                   worker_secret=os.getenv("TRAVEL_RESEARCH_WORKER_SECRET", ""),
                   delivery_secret=os.getenv("TRAVEL_RESEARCH_DELIVERY_SECRET", ""),
                   helper=os.getenv("TRAVEL_RESEARCH_HELPER", ""),
                   profile_pin=PROFILE_PIN or "",
                   official_hosts=tuple(x.strip().lower() for x in os.getenv("TRAVEL_RESEARCH_OFFICIAL_HOSTS", "").split(",") if x.strip()))


class Research:
    def __init__(self, get_db, policy=None, *, sms=None, vapi=None, fetch=None, clock=time.time):
        self.db = get_db
        self.policy = policy or Policy.environment()
        self.sms, self.vapi, self.clock = sms, vapi, clock
        self.fetch = fetch or firecrawl_page

    def read(self, key):
        with self.db() as db:
            row = db.execute("SELECT detail FROM call_events WHERE event_key=? AND event_type=?",
                             (key, KIND)).fetchone()
        require(row is not None, "request_not_found", 404)
        return decode(row["detail"])

    def insert(self, key, call_id, assistant, record):
        with self.db() as db:
            n = db.execute("INSERT INTO call_events (call_id,event_type,assistant,summary,detail,event_key) "
                           "VALUES (?,?,?,?,?,?) ON CONFLICT (event_key) DO NOTHING",
                           (call_id, KIND, assistant, "Deferred travel research", canonical(record), key)).rowcount
            db.commit()  # Do not acknowledge a context-manager-only commit.
        return n == 1

    def change(self, key, before, after):
        with self.db() as db:
            n = db.execute("UPDATE call_events SET detail=? WHERE event_key=? AND event_type=? AND detail=?",
                           (canonical(after), key, KIND, canonical(before))).rowcount
            require(n == 1, "concurrent_update", 409)
            db.commit()
        return after

    def auth(self, supplied, delivery=False):
        expected = self.policy.delivery_secret if delivery else self.policy.worker_secret
        require(bool(expected) and hmac.compare_digest(supplied, expected), "unauthorized", 401)
        if delivery:
            require(expected != self.policy.worker_secret, "credentials_must_be_separate", 503)

    def admitted(self, slug):
        p = self.policy
        return (p.enabled and p.qualified and slug == p.helper and bool(p.profile_pin)
                and bool(p.official_hosts)
                and bool(p.worker_secret) and bool(p.delivery_secret)
                and p.worker_secret != p.delivery_secret)

    def available(self, slug):
        if not self.admitted(slug):
            return False
        try:
            beat = self.read("travel-research-heartbeat:" + slug)
            return (beat["profile_pin"] == self.policy.profile_pin
                    and beat["healthy"] is True
                    and 0 <= self.clock() - beat["at"] <= self.policy.heartbeat_seconds)
        except (HTTPException, KeyError):
            return False

    def heartbeat(self, body):
        exact(body, {"healthy", "profile_pin"})
        require(type(body["healthy"]) is bool and body["profile_pin"] == self.policy.profile_pin,
                "unqualified_profile", 403)
        require(self.admitted(self.policy.helper), "research_disabled", 503)
        key = "travel-research-heartbeat:" + self.policy.helper
        value = {**body, "at": self.clock()}
        if not self.insert(key, "research-worker", "research-worker", value):
            old = self.read(key)
            self.change(key, old, value)
        return {"stored": True}

    def intake(self, slug, config, call, args):
        require(self.available(slug), "research_unavailable_no_callback_promised", 503)
        validate_call(call, config)
        require(call.get("type") == "inboundPhoneCall"
                and call.get("phoneNumberId") == config["phone_number_id"], "inbound_call_required", 403)
        allowed = {"question", "visit_date", "location", "travel_mode", "required_fields", "expected_revision",
                   "sms_consent", "callback_consent", "consent_evidence",
                   "confirmed_mobile", "mobile_confirmed"}
        require(isinstance(args, dict) and not set(args) - allowed, "invalid_intake_fields")
        require(type(args.get("sms_consent")) is bool and type(args.get("callback_consent")) is bool
                and (args["sms_consent"] or args["callback_consent"]),
                "explicit_delivery_consent_required")
        evidence = text_arg(args, "consent_evidence", 400)
        number = recipient(call, args) if args["sms_consent"] else callback_recipient(call, args)
        revision = args.get("expected_revision")
        require(type(revision) is int and revision >= 0, "invalid_revision")
        visit = text_arg(args, "visit_date", 10)
        try:
            parsed = date.fromisoformat(visit)
            today = datetime.fromtimestamp(self.clock(), ZoneInfo("Europe/London")).date()
            require(parsed.isoformat() == visit and 0 <= (parsed - today).days <= 366, "invalid_visit_date")
        except ZoneInfoNotFoundError:
            raise HTTPException(503, "london_timezone_data_unavailable")
        except ValueError:
            raise HTTPException(422, "invalid_visit_date")
        mode = args.get("travel_mode")
        require(mode in ("walking", "transit", "driving", "bicycling"), "invalid_travel_mode")
        key = "travel-research:" + digest([slug, call["id"]])
        # The backend alone freezes the recipient and the original call identity.
        identity = {"call_id": call["id"], "helper": slug, "assistant": config["assistant_id"],
                    "inbound_assistant": call["assistantId"], "phone_number_id": config["phone_number_id"],
                    "original_customer_number": (call.get("customer") or {}).get("number"),
                    "customer": {"number": number, "name": str((call.get("customer") or {}).get("name", ""))[:80]}}
        old = self.read(key) if revision else None
        if old:
            require(old["revision"] == revision and old["identity"] == identity, "revision_or_identity_conflict", 409)
            require(not old.get("delivery"), "delivery_already_reserved", 409)
        try:
            fields = required_fields(args)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        brief = {"schema": "travel-research.brief/v2", "timezone": "Europe/London",
                 "visit_date": visit, "requested_at": datetime.fromtimestamp(self.clock(), ZoneInfo("Europe/London")).isoformat(),
                 "question": text_arg(args, "question", 800),
                 "location": text_arg(args, "location", 200), "travel_mode": mode,
                 "required_fields": fields, "official_hosts": list(self.policy.official_hosts)}
        record = {"schema": "travel-research.request/v1", "request_id": key, "revision": revision + 1,
                  "identity": identity, "brief": brief, "brief_hash": digest(brief), "state": "queued",
                  "created_at": self.clock(), "deadline": self.clock() + self.policy.deadline_seconds,
                  "consent": {"sms": args["sms_consent"], "callback": args["callback_consent"], "evidence": evidence,
                              "token": secrets.token_urlsafe(32), "at": self.clock()},
                  "supersedes": {"revision": old["revision"], "execution_id": old.get("execution_id")} if old else None}
        if old:
            archive_key = "travel-research-revision:" + digest([key, old["revision"]])
            archived = {**old, "state": "superseded", "superseded_state": old["state"],
                        "delivery_state": "superseded", "superseded_at": self.clock()}
            with self.db() as db:
                archived_count = db.execute("INSERT INTO call_events (call_id,event_type,assistant,summary,detail,event_key) VALUES (?,?,?,?,?,?) ON CONFLICT (event_key) DO NOTHING",
                    (call["id"], KIND, config["assistant_id"], "Superseded research revision audit", canonical(archived), archive_key)).rowcount
                require(archived_count == 1, "revision_archive_conflict", 409)
                changed = db.execute("UPDATE call_events SET detail=? WHERE event_key=? AND event_type=? AND detail=?",
                    (canonical(record), key, KIND, canonical(old))).rowcount
                require(changed == 1, "concurrent_update", 409)
                db.commit()
        else:
            require(self.insert(key, call["id"], config["assistant_id"], record), "revision_conflict", 409)
        return {"status": "stored", "request_id": key, "revision": revision + 1,
                "deadline": record["deadline"], "message": "Request saved. Follow-up is subject to verified useful information and the delivery deadline."}

    def public(self, record):
        return {k: record[k] for k in ("request_id", "revision", "brief_hash", "brief", "deadline")}

    def pending(self):
        require(self.available(self.policy.helper), "research_unavailable", 503)
        with self.db() as db:
            rows = db.execute("SELECT detail FROM call_events WHERE event_type=? AND assistant=? ORDER BY id DESC LIMIT 100",
                              (KIND, decode(settings(self.db, self.policy.helper)["config"])["assistant_id"])).fetchall()
        for row in rows:
            value = decode(row["detail"])
            if value.get("state") == "queued" and value["identity"]["helper"] == self.policy.helper and value["deadline"] > self.clock():
                return self.public(value)
        return None

    def bound(self, body, *, live=True, historical=False):
        key = body.get("request_id", "")
        require(isinstance(key, str) and key.startswith("travel-research:"), "invalid_request_id")
        record = self.read(key)
        require(type(body.get("revision")) is int, "stale_request", 409)
        if historical and body["revision"] != record["revision"]:
            require(0 < body["revision"] < record["revision"], "stale_request", 409)
            key = "travel-research-revision:" + digest([body["request_id"], body["revision"]])
            record = self.read(key)
            require(record["state"] == "superseded", "stale_request", 409)
        require(record["identity"]["helper"] == self.policy.helper, "wrong_scope", 403)
        require(type(body.get("revision")) is int and body.get("revision") == record["revision"]
                and body.get("brief_hash") == record["brief_hash"], "stale_request", 409)
        if live:
            require(record["deadline"] > self.clock(), "request_expired", 409)
        return key, record

    def claim(self, body):
        exact(body, {"request_id", "revision", "brief_hash", "execution_id"})
        require(self.available(self.policy.helper), "research_unavailable", 503)
        require(isinstance(body["execution_id"], str) and bool(OPAQUE.fullmatch(body["execution_id"])), "invalid_execution")
        key, old = self.bound(body, live=False)
        if old.get("execution_id") == body["execution_id"]:
            return {**self.public(old), "execution_id": body["execution_id"], "launch_allowed": False,
                    "expired": old["deadline"] <= self.clock(), "no_effects": old["deadline"] <= self.clock()}
        require(old["deadline"] > self.clock(), "request_expired", 409)
        require(old["state"] == "queued", "already_owned_no_lease_replay", 409)
        after = {**old, "state": "claimed", "execution_id": body["execution_id"],
                 "claimed_at": self.clock(), "lease_until": min(old["deadline"], self.clock() + 60), "stages": {}}
        with self.db() as db:
            inserted = db.execute("INSERT INTO call_events (call_id,event_type,assistant,summary,detail,event_key) VALUES (?,?,?,?,?,?) ON CONFLICT (event_key) DO NOTHING",
                                  (old["identity"]["call_id"], KIND, old["identity"]["assistant"], "Research execution ownership",
                                   canonical({"request_id": key, "revision": old["revision"]}), "travel-research-execution:" + body["execution_id"])).rowcount
            require(inserted == 1, "execution_identity_already_used", 409)
            changed = db.execute("UPDATE call_events SET detail=? WHERE event_key=? AND event_type=? AND detail=?",
                                 (canonical(after), key, KIND, canonical(old))).rowcount
            require(changed == 1, "concurrent_update", 409)
            db.commit()
        return {**self.public(after), "execution_id": body["execution_id"], "launch_allowed": True}

    def execution(self, body, *, live=True, historical=False):
        key, old = self.bound(body, live=live, historical=historical)
        require(body.get("execution_id") == old.get("execution_id") and bool(old.get("execution_id")), "wrong_execution", 409)
        return key, old

    def receipt(self, body):
        exact(body, {"request_id", "revision", "brief_hash", "execution_id", "stage", "receipt"})
        key, old = self.execution(body, live=False, historical=True)
        stage, receipt = body["stage"], body["receipt"]
        require(stage in ("research", "verification"), "invalid_stage")
        exact(receipt, {"attempt_id", "state", "pid", "process_created", "thread_id", "turn_id", "exit_code", "completed", "output_hash"})
        require(isinstance(receipt["attempt_id"], str) and OPAQUE.fullmatch(receipt["attempt_id"]), "invalid_attempt")
        require(receipt["state"] in ("attempted", "running", "exited", "unknown"), "invalid_receipt")
        require(type(receipt["completed"]) is bool, "invalid_completion")
        if receipt["state"] in ("running", "exited"):
            require(type(receipt["pid"]) is int and receipt["pid"] > 0 and isinstance(receipt["process_created"], str)
                    and bool(receipt["process_created"]), "process_identity_required")
        if receipt["state"] == "exited":
            require(type(receipt["exit_code"]) is int, "terminal_exit_required")
        if receipt["completed"]:
            require(receipt["state"] == "exited" and receipt["exit_code"] == 0
                    and all(isinstance(receipt[k], str) and OPAQUE.fullmatch(receipt[k]) for k in ("thread_id", "turn_id")),
                    "native_completion_binding_required")
            require(isinstance(receipt["output_hash"], str) and HEX.fullmatch(receipt["output_hash"]), "output_hash_required")
        previous = old["stages"].get(stage)
        receipt_hash = digest(receipt)
        history = old.get("receipt_hashes", {}).get(stage, [])
        if previous == receipt or receipt_hash in history:
            return audit_ack(old, self.clock())
        require(old["state"] in ("claimed", "launching", "superseded"), "result_already_stored", 409)
        if previous:
            require(previous["attempt_id"] == receipt["attempt_id"], "duplicate_launch_forbidden", 409)
            require(previous["state"] not in ("exited", "unknown"), "terminal_receipt_immutable", 409)
            require(receipt["state"] != "attempted", "receipt_regression", 409)
            if previous["pid"]:
                require(all(previous[k] == receipt[k] for k in ("pid", "process_created")), "process_binding_conflict", 409)
        else:
            no_launch_failure = (receipt["state"] == "unknown" and receipt["completed"] is False
                                 and all(receipt[k] is None for k in ("pid", "process_created", "thread_id", "turn_id", "exit_code", "output_hash")))
            require(receipt["state"] == "attempted" or no_launch_failure, "attempt_must_precede_spawn", 409)
            if stage == "verification":
                require(old["stages"].get("research", {}).get("completed") is True, "research_not_complete", 409)
                proof = self.read(old.get("source_proof_key", ""))
                require(proof.get("state") == "ready" and proof.get("snapshots"), "trusted_sources_not_ready", 409)
        require(len(history) < 3, "receipt_transition_limit", 409)
        self.change(key, old, {**old, "state": "superseded" if old["state"] == "superseded" else "launching",
                              "stages": {**old["stages"], stage: receipt},
                              "receipt_hashes": {**old.get("receipt_hashes", {}), stage: history + [receipt_hash]}})
        return audit_ack(old, self.clock())

    async def sources(self, body):
        """Only this trusted backend fetch may create source proof."""
        exact(body, {"request_id", "revision", "brief_hash", "execution_id", "fetch_attempt_id", "candidate", "candidate_hash"})
        key, old = self.execution(body, live=False)
        require(isinstance(body["fetch_attempt_id"], str) and OPAQUE.fullmatch(body["fetch_attempt_id"]), "invalid_fetch_attempt")
        require(digest(body["candidate"]) == body["candidate_hash"], "candidate_hash_mismatch")
        try:
            validate_candidate(body["candidate"], old["brief"])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        proof_key = "travel-research-sources:" + digest([key, old["revision"], old["execution_id"]])
        fingerprint = digest(body)
        if old.get("source_proof_key"):
            require(old["source_proof_key"] == proof_key, "source_scope_conflict", 409)
            proof = self.read(proof_key)
            require(proof["request_hash"] == fingerprint, "source_attempt_conflict", 409)
            return self.source_response(proof)
        require(old["deadline"] > self.clock(), "request_expired", 409)
        stage = old.get("stages", {}).get("research", {})
        require(stage.get("completed") is True and stage.get("output_hash") == body["candidate_hash"], "candidate_not_bound_to_research", 409)
        urls = body["candidate"]["proposed_urls"]
        for url in urls:
            official_url(url, self.policy.official_hosts)
        proof = {**{k: body[k] for k in ("request_id", "revision", "brief_hash", "execution_id", "fetch_attempt_id", "candidate", "candidate_hash")},
                 "request_hash": fingerprint, "state": "fetching", "started_at": self.clock(),
                 "snapshots": [], "fetch_failures": []}
        # Request association and the fetch reservation are committed together.
        with self.db() as db:
            inserted = db.execute("INSERT INTO call_events (call_id,event_type,assistant,summary,detail,event_key) VALUES (?,?,?,?,?,?) ON CONFLICT (event_key) DO NOTHING",
                (old["identity"]["call_id"], KIND, old["identity"]["assistant"], "Trusted research source fetch", canonical(proof), proof_key)).rowcount
            require(inserted == 1, "source_fetch_already_reserved", 409)
            changed = db.execute("UPDATE call_events SET detail=? WHERE event_key=? AND event_type=? AND detail=?",
                (canonical({**old, "source_proof_key": proof_key}), key, KIND, canonical(old))).rowcount
            require(changed == 1, "concurrent_update", 409)
            db.commit()
        seconds = min(self.policy.source_seconds, old["deadline"] - self.clock() - 5)
        async def fetch_one(index, url):
            try:
                require(seconds > 0, "request_expired", 409)
                raw = await asyncio.wait_for(invoke(self.fetch, url), timeout=seconds)
                require(isinstance(raw, dict) and raw.get("success") is True, "source_fetch_failed")
                page = raw.get("data") or {}
                metadata = page.get("metadata") or {}
                resolved = metadata.get("url") or metadata.get("sourceURL")
                official_url(resolved, self.policy.official_hosts)
                for reported in (metadata.get("url"), metadata.get("sourceURL")):
                    if reported:
                        official_url(reported, self.policy.official_hosts)
                require(metadata.get("statusCode", 200) == 200, "source_http_failed")
                text = page.get("markdown")
                require(isinstance(text, str) and 0 < len(text.strip()) <= 30000, "source_page_budget_or_content")
                snapshot = {"reference": "source-" + str(index), "url": resolved,
                            "retrieved_at": datetime.fromtimestamp(self.clock(), ZoneInfo("UTC")).isoformat(),
                            "page_text": text, "content_hash": digest(text), "kind": "page"}
                return snapshot, None, {"requested_url": url, "provider_response_hash": digest(raw),
                                       "credits_observed": raw.get("creditsUsed", page.get("creditsUsed"))}
            except Exception as exc:
                # Do not expose credential-bearing exception strings or provider bodies.
                return None, {"url": url, "reason": type(exc).__name__, "observed_at": self.clock()}, None
        try:
            fetched = await asyncio.gather(*(fetch_one(i + 1, url) for i, url in enumerate(urls)))
            snapshots = [source for source, failure, receipt in fetched if source]
            failures = [failure for source, failure, receipt in fetched if failure]
            completed = {**proof, "state": "ready", "finished_at": self.clock(), "snapshots": snapshots,
                         "fetch_failures": failures, "provider_receipts": [r for _, _, r in fetched if r]}
        except BaseException:
            completed = {**proof, "state": "ready", "finished_at": self.clock(), "provider_receipts": [],
                         "fetch_failures": [{"url": url, "reason": "fetch_cancelled_or_unknown", "observed_at": self.clock()} for url in urls]}
            self.change(proof_key, proof, completed)
            raise
        completed["source_manifest_hash"] = digest(source_manifest(completed["snapshots"], completed["fetch_failures"]))
        completed["verification_input_hash"] = digest(verification_input(old["brief"], body["candidate"], completed["snapshots"], completed["fetch_failures"]))
        self.change(proof_key, proof, completed)
        return self.source_response(completed)

    def source_response(self, proof):
        if proof["state"] != "ready":
            return {"status": "pending", "fetch_attempt_id": proof["fetch_attempt_id"], "retry_fetch_allowed": False}
        if "source_manifest_hash" not in proof:
            return {"status": "failed", "fetch_attempt_id": proof["fetch_attempt_id"], "retry_fetch_allowed": False,
                    "error": "source_fetch_cancelled_or_unknown"}
        return {"status": "ready", **{k: proof[k] for k in ("fetch_attempt_id", "candidate_hash", "snapshots", "fetch_failures", "source_manifest_hash", "verification_input_hash")}}

    def result(self, body):
        exact(body, {"request_id", "revision", "brief_hash", "execution_id", "result", "result_hash"})
        key, old = self.execution(body, live=False, historical=True)
        result = body["result"]
        require(digest(result) == body["result_hash"], "result_hash_mismatch")
        if "result_hash" in old:
            require(body["result_hash"] == old["result_hash"], "duplicate_result_conflict", 409)
            return {**audit_ack(old, self.clock()), "status": old["result"]["status"], "delivery_state": old.get("delivery_state", "pending")}
        proof = self.read(old["source_proof_key"]) if old.get("source_proof_key") else None
        validate_result(result, old, self.clock(), proof)
        expired = old["deadline"] <= self.clock()
        superseded = old["state"] == "superseded"
        state = "superseded" if superseded else "expired" if expired else "pending"
        self.change(key, old, {**old, "state": "superseded" if superseded else "returned", "result": result, "result_hash": body["result_hash"],
                              "returned_at": self.clock(), "delivery_state": state})
        return {**audit_ack(old, self.clock()), "status": result["status"], "delivery_state": state}

    def delivery_body(self, record):
        return {**{k: record[k] for k in ("request_id", "revision", "brief_hash", "execution_id", "result_hash")},
                "consent_token": record["consent"]["token"]}

    async def advance_pending(self):
        """Existing backend continuation, woken by its existing scoped poll/return.

        The durable work is still the returned call_events record. The wakeup
        contains no caller-selected recipient or effect arguments. Repeated
        wakeups consume the same CAS reservations and never replay an effect.
        """
        if not self.admitted(self.policy.helper):
            return {"processed": 0}
        with self.db() as db:
            rows = db.execute("SELECT detail FROM call_events WHERE event_type=? ORDER BY id DESC LIMIT 100", (KIND,)).fetchall()
        processed = 0
        for row in reversed(rows):
            record = decode(row["detail"])
            if (record.get("state") != "returned" or record.get("delivery_state") != "pending"
                    or record.get("identity", {}).get("helper") != self.policy.helper):
                continue
            if record["deadline"] <= self.clock():
                try:
                    self.change(record["request_id"], record, {**record, "delivery_state": "expired"})
                except HTTPException:
                    pass
                continue
            try:
                await self.deliver(self.delivery_body(record))
            except (HTTPException, ValueError, asyncio.TimeoutError):
                pass  # Existing durable pending state is rechecked by the next wakeup.
            processed += 1
            if processed >= 1:
                break
        return {"processed": processed}

    async def _provider(self, *args):
        require(self.vapi is not None, "callback_provider_unavailable", 503)
        return await asyncio.wait_for(invoke(self.vapi, *args), timeout=self.policy.provider_seconds)

    async def deliver(self, body):
        exact(body, {"request_id", "revision", "brief_hash", "execution_id", "result_hash", "consent_token"})
        key, old = self.execution(body, live=False)
        require(self.admitted(self.policy.helper), "research_disabled", 503)
        require(hmac.compare_digest(str(body["consent_token"]), old["consent"]["token"]), "consent_required", 403)
        require(old["state"] == "returned" and body["result_hash"] == old.get("result_hash"), "wrong_result", 409)
        if old.get("delivery_state") in ("complete", "unknown", "expired", "withdrawn"):
            return {"delivery": old.get("delivery", {}), "status": old["delivery_state"]}
        require(old["deadline"] > self.clock(), "request_expired", 409)
        if not (old["consent"]["sms"] or old["consent"]["callback"]):
            self.change(key, old, {**old, "delivery_state": "withdrawn"})
            return {"delivery": old.get("delivery", {}), "status": "withdrawn"}
        config = decode(settings(self.db, old["identity"]["helper"])["config"])
        require(config["enabled"] and config["assistant_id"] == old["identity"]["assistant"]
                and config["phone_number_id"] == old["identity"]["phone_number_id"], "helper_configuration_changed", 409)
        proof = self.read(old["source_proof_key"]) if old.get("source_proof_key") else None
        validate_result(old["result"], old, self.clock(), proof, delivery=True)
        # Fresh provider identity, not a caller/model assertion or a stale end webhook.
        call = await self._provider("call/" + old["identity"]["call_id"])
        require(isinstance(call, dict) and call.get("id") == old["identity"]["call_id"]
                and call.get("type") == "inboundPhoneCall"
                and call.get("phoneNumberId") == old["identity"]["phone_number_id"]
                and call.get("assistantId") in (old["identity"]["assistant"], old["identity"]["inbound_assistant"]), "inbound_identity_changed", 409)
        # The chosen fallback is frozen separately; compare the original provider number.
        require((call.get("customer") or {}).get("number") == old["identity"]["original_customer_number"], "inbound_customer_changed", 409)
        if call.get("status") != "ended" or not call.get("endedAt"):
            return {"delivery": old.get("delivery", {}), "status": "waiting_for_inbound_end"}
        # A competing delivery may have reserved an effect while the GET ran.
        current = self.read(key)
        require(current["revision"] == old["revision"] and current.get("result_hash") == old["result_hash"], "stale_request", 409)
        old = current
        require(old["deadline"] > self.clock(), "request_expired", 409)
        require(old["consent"]["sms"] or old["consent"]["callback"], "consent_withdrawn", 403)
        delivery = old.get("delivery", {})
        if any(value.get("status") == "unknown" and "finished_at" not in value for value in delivery.values()):
            if any(value.get("status") == "unknown" and "finished_at" not in value
                   and self.clock() - value.get("reserved_at", self.clock()) > self.policy.provider_seconds + 5
                   for value in delivery.values()):
                self.change(key, old, {**old, "delivery_state": "unknown"})
            return {"delivery": delivery, "status": "submission_in_progress_or_unknown_reconcile"}
        wants_sms = old["consent"]["sms"] and old["result"]["status"] != "failed" and old["result"]["destination"] is not None
        if "sms" not in delivery:
            if not wants_sms:
                sms_receipt = {"status": "not_requested" if not old["consent"]["sms"] else "not_applicable", "finished_at": self.clock()}
                old = self.change(key, old, {**old, "delivery": {**delivery, "sms": sms_receipt}})
            else:
                require(self.sms is not None, "sms_unavailable", 503)
                text = render_sms(old["result"], old["brief"]["travel_mode"])
                encoding = sms_segments(text)
                if encoding["segments"] > self.policy.max_sms_segments:
                    receipt = {"status": "failed", "reason": "sms_segment_limit", "finished_at": self.clock(), **encoding}
                    old = self.change(key, old, {**old, "delivery": {**delivery, "sms": receipt}})
                else:
                    reserved = effect_reservation(old, "sms", self.clock(), {"text_hash": digest(text), **encoding})
                    old = self.change(key, old, {**old, "delivery": {**delivery, "sms": reserved}})
                    try:
                        response = await asyncio.wait_for(invoke(self.sms, old["identity"]["customer"]["number"], text,
                            from_number=os.getenv("PERSONAL_AGENT_SMS_FROM", "CALLMEIE")), timeout=self.policy.provider_seconds)
                        sid = response.get("sid", "") if isinstance(response, dict) else ""
                        require(isinstance(response, dict) and re.fullmatch(r"SM[0-9a-fA-F]{32}", sid)
                                and not response.get("mocked") and response.get("status") != "mocked", "sms_not_accepted")
                        provider_status = response.get("status")
                        if provider_status in ("failed", "undelivered", "canceled"):
                            status = "failed"
                        else:
                            require(response.get("ok") is True and response.get("http_status") in (200, 201)
                                    and provider_status in ("accepted", "queued", "sending", "sent", "delivered"), "sms_acceptance_not_proven")
                            status = "delivered" if provider_status == "delivered" else "accepted"
                        receipt = {**reserved, "status": status, "provider_id": sid, "provider_status": response.get("status", ""), "finished_at": self.clock()}
                    except Exception:
                        receipt = {**reserved, "finished_at": self.clock()}  # Unknown is never resendable.
                    old = self.change(key, old, {**old, "delivery": {**old["delivery"], "sms": receipt}})
        if old["consent"]["callback"] and "callback" not in old["delivery"]:
            # Read only the existing helper's voice/model; no inherited tools/hooks.
            profile = await self._provider("assistant/" + old["identity"]["assistant"])
            require(isinstance(profile, dict) and profile.get("id") == old["identity"]["assistant"]
                    and isinstance(profile.get("voice"), dict) and profile["voice"].get("provider")
                    and (profile.get("model") or {}).get("provider") == "openai"
                    and isinstance(profile["model"].get("model"), str), "callback_profile_unavailable", 503)
            require(old["deadline"] > self.clock(), "request_expired", 409)
            reserved = effect_reservation(old, "callback", self.clock())
            payload = callback_payload(old, profile, reserved)
            reserved["payload_hash"] = digest(payload)
            old = self.change(key, old, {**old, "delivery": {**old["delivery"], "callback": reserved}})
            try:
                response = await self._provider("call", "POST", payload)
                call_id = response.get("id", "") if isinstance(response, dict) else ""
                require(isinstance(call_id, str) and OPAQUE.fullmatch(call_id) and not response.get("mocked")
                        and not response.get("error"), "call_id_required")
                receipt = {**reserved, "status": "accepted", "provider_id": call_id, "finished_at": self.clock()}
            except Exception:
                receipt = {**reserved, "finished_at": self.clock()}
            old = self.change(key, old, {**old, "delivery": {**old["delivery"], "callback": receipt}})
        elif not old["consent"]["callback"] and "callback" not in old["delivery"]:
            old = self.change(key, old, {**old, "delivery": {**old["delivery"], "callback": {"status": "not_requested", "finished_at": self.clock()}}})
        state = "unknown" if any(value["status"] == "unknown" for value in old["delivery"].values()) else "complete"
        old = self.change(key, old, {**old, "delivery_state": state})
        return {"delivery": old["delivery"], "status": state}


def normalized(value):
    return " ".join(value.split()).casefold()


def validate_result(result, record, now, proof=None, *, delivery=False):
    exact(result, {"schema", "status", "claims", "missing_coverage", "uncertainties", "destination", "verification"})
    require(result["schema"] == RESULT_SCHEMA and result["status"] in ("complete", "partial", "failed"), "invalid_result_status")
    fields = required_fields(record["brief"])
    require(isinstance(result["claims"], list) and len(result["claims"]) <= len(fields)
            and isinstance(result["missing_coverage"], list), "result_too_large")
    require(isinstance(result["uncertainties"], list) and len(result["uncertainties"]) <= 6
            and all(isinstance(x, str) and 0 < len(x) <= 300 for x in result["uncertainties"]), "invalid_uncertainties")
    found, missing = [], []
    for item in result["missing_coverage"]:
        exact(item, {"field", "reason"})
        require(item["field"] in fields and item["field"] not in missing
                and isinstance(item["reason"], str) and 0 < len(item["reason"]) <= 300, "invalid_missing_coverage")
        missing.append(item["field"])
    for claim in result["claims"]:
        exact(claim, {"field", "text", "qualifiers", "source_reference", "quote"})
        require(claim["field"] in fields and claim["field"] not in found and claim["field"] not in missing, "duplicate_or_unrequested_claim")
        require(all(isinstance(claim[k], str) and 0 < len(claim[k]) <= limit for k, limit in (("text", 220), ("qualifiers", 180), ("quote", 500))), "claim_qualifiers_required")
        found.append(claim["field"])
    require(set(found + missing) == set(fields), "unaccounted_requested_fields")
    if result["status"] == "failed":
        require(not result["claims"] and result["destination"] is None and result["uncertainties"]
                and result["verification"] is None, "failed_result_must_be_honest")
        require(any(s.get("state") in ("exited", "unknown") for s in record.get("stages", {}).values()), "terminal_failure_receipt_required")
        return
    require(bool(result["claims"]) and (result["status"] == "complete") == (not missing), "result_coverage_status_mismatch")
    if {"other_fact", "dated_availability"} <= set(fields):
        require({"other_fact", "dated_availability"} <= set(found),
                "recommendation_requires_programme_and_dated_availability")
    stages = record.get("stages", {})
    require(all(stages.get(k, {}).get("state") == "exited" and stages[k].get("completed") is True
                and stages[k].get("exit_code") == 0 for k in ("research", "verification")), "two_completed_native_stages_required")
    require(stages["research"]["thread_id"] != stages["verification"]["thread_id"], "separate_verification_required")
    require(isinstance(proof, dict) and proof.get("state") == "ready", "trusted_source_proof_required")
    require(all(proof[k] == record[k] for k in ("request_id", "revision", "brief_hash", "execution_id")), "source_request_binding_mismatch")
    sources = {source["reference"]: source for source in proof["snapshots"]}
    require(len(sources) == len(proof["snapshots"]) and sources, "invalid_trusted_sources")
    for source in sources.values():
        require(source["content_hash"] == digest(source["page_text"]), "stored_source_hash_mismatch")
        checked = epoch(source["retrieved_at"])
        require(record["claimed_at"] - 5 <= checked <= now + 5, "source_time_mismatch")
        if delivery:
            require(now - checked <= 900, "stale_evidence")
    for claim in result["claims"]:
        source = sources.get(claim["source_reference"])
        require(source is not None and normalized(claim["quote"]) in normalized(source["page_text"]), "unsupported_claim")
    destination = result["destination"]
    if destination is not None:
        exact(destination, {"text", "source_reference", "quote"})
        require("visitor_address" in found
                and ("visitor_entrance" not in fields or "visitor_entrance" in found),
                "destination_requires_address_and_requested_entrance")
        require(isinstance(destination["text"], str) and 0 < len(destination["text"]) <= 200
                and isinstance(destination["quote"], str) and 0 < len(destination["quote"]) <= 500, "invalid_destination")
        source = sources.get(destination["source_reference"])
        require(source is not None and normalized(destination["text"]) in normalized(destination["quote"])
                and normalized(destination["quote"]) in normalized(source["page_text"]), "unsupported_maps_destination")
        address = next(claim for claim in result["claims"] if claim["field"] == "visitor_address")
        try:
            validate_maps_anchor(destination["text"], address["quote"])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    verification = result["verification"]
    exact(verification, {"candidate_hash", "source_manifest_hash", "verification_input_hash", "native_output_hash"})
    expected_input = verification_input(record["brief"], proof["candidate"], proof["snapshots"], proof["fetch_failures"])
    require(verification["candidate_hash"] == proof["candidate_hash"] == digest(proof["candidate"])
            == stages["research"]["output_hash"], "candidate_execution_hash_mismatch")
    require(verification["source_manifest_hash"] == proof["source_manifest_hash"]
            == digest(source_manifest(proof["snapshots"], proof["fetch_failures"])), "source_manifest_hash_mismatch")
    require(verification["verification_input_hash"] == proof["verification_input_hash"] == digest(expected_input), "original_brief_verification_mismatch")
    require(verification["native_output_hash"] == stages["verification"]["output_hash"] == digest(verifier_output(result)), "result_execution_hash_mismatch")
    require(len(render_summary(result)) <= 900, "spoken_summary_budget_exceeded")


def official_url(value, hosts):
    require(isinstance(value, str) and len(value) <= 1800, "invalid_official_url")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and not parsed.fragment)
    except ValueError:
        valid = False
    require(valid, "url_outside_scoped_official_hosts")
    return value


async def firecrawl_page(url):
    key = os.getenv("FIRECRAWL_API_KEY") or os.getenv("FIRECRAWL_API")
    require(bool(key), "source_fetch_not_configured", 503)
    async with httpx.AsyncClient(timeout=24, follow_redirects=False) as client:
        response = await client.post("https://api.firecrawl.dev/v2/scrape", headers={"Authorization": "Bearer " + key},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True, "maxAge": 0, "timeout": 20000})
    require(response.status_code == 200 and len(response.content) <= 2000000, "source_provider_response_failed")
    return response.json()


def effect_reservation(record, kind, now, extra=None):
    return {"status": "unknown", "reserved_at": now, "retry_allowed": False, "attempt_id": uuid.uuid4().hex,
            "kind": kind, **{k: record[k] for k in ("request_id", "revision", "brief_hash", "execution_id", "result_hash")},
            **(extra or {})}


def render_summary(result, field_labels=False):
    if result["status"] == "failed":
        return "I could not verify the requested details within this research attempt. I have no confirmed information to add."
    labels = {"opening_time": "Opening time: ", "closing_time": "Closing time: ", "last_admission": "Last admission: "} if field_labels else {}
    text = " ".join(labels.get(c["field"], "") + c["text"].rstrip(" .") + ". " + c["qualifiers"].rstrip(" .") + "." for c in result["claims"])
    if result["missing_coverage"]:
        text += " I could not verify " + ", ".join(c["field"].replace("_", " ") for c in result["missing_coverage"]) + "."
    if result["uncertainties"]:
        text += " Unverified: " + "; ".join(result["uncertainties"]) + "."
    return text


def render_callback_summary(result):
    """Speak verified content, with the requested recommendation before logistics.

    The original renderer still owns result validation and its conservative
    length budget. Structured missing coverage remains in the stored result.
    """
    if result["status"] == "failed":
        return "I could not verify the details you asked for, so I have no confirmed answer."
    claims = sorted(result["claims"], key=lambda claim: claim["field"] != "other_fact")
    bare_time_prefixes = {"opening_time": "It opens at ", "closing_time": "It closes at ",
                          "last_admission": "Last admission is at "}
    parts = []
    for claim in claims:
        text = claim["text"].rstrip(" .")
        if (claim["field"] in bare_time_prefixes
                and re.fullmatch(r"\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?", text, re.I)):
            text = bare_time_prefixes[claim["field"]] + text
        parts.append(text + ". " + claim["qualifiers"].rstrip(" .") + ".")
    spoken = {normalized(claim[key].rstrip(" .")) for claim in claims for key in ("text", "qualifiers")}
    uncertainties = []
    for item in result["uncertainties"]:
        text = item.rstrip(" .")
        if normalized(text) not in spoken:
            uncertainties.append(text + ".")
            spoken.add(normalized(text))
    if uncertainties:
        parts.append(" ".join(uncertainties))
    elif result["missing_coverage"] and not result["uncertainties"]:
        parts.append("Some requested details remain unverified.")
    return " ".join(parts)


def render_sms(result, mode):
    require(result["status"] != "failed" and result["destination"] is not None, "no_verified_maps_destination")
    # Current-location navigation: no stale original location is embedded.
    return "Your checked destination: " + result["destination"]["text"] + "\n" + maps_url(result["destination"]["text"], mode)


def sms_segments(text):
    # GSM7 extension characters use two septets. Count complete final body/URL.
    basic = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    extension = "^{}\\[~]|€"
    if all(c in basic or c in extension for c in text):
        units = sum(2 if c in extension else 1 for c in text)
        return {"encoding": "GSM-7", "units": units, "segments": 1 if units <= 160 else (units + 152) // 153}
    units = len(text.encode("utf-16-be")) // 2
    return {"encoding": "UCS-2", "units": units, "segments": 1 if units <= 70 else (units + 66) // 67}


def callback_payload(record, profile, reservation):
    sms = record.get("delivery", {}).get("sms", {"status": "not_requested"})
    sms_text = ("The provider reports that your directions text was delivered." if sms["status"] == "delivered" else
                "I've submitted your directions text. Delivery isn't confirmed yet." if sms["status"] == "accepted" else
                "I can't confirm that the directions text was sent." if sms["status"] in ("unknown", "failed") else "")
    speech = "Hi, it's your London helper. Here's what I found. " + render_callback_summary(record["result"])
    if sms_text:
        speech += " " + sms_text
    speech += " Bye for now."
    require(len(speech) <= 1200, "callback_speech_budget_exceeded")
    metadata = {"project": "CallMeIE", "purpose": "verified-travel-callback",
                **{k: reservation[k] for k in ("request_id", "revision", "brief_hash", "execution_id", "result_hash", "attempt_id")}}
    assistant = {"firstMessage": speech, "firstMessageMode": "assistant-speaks-first", "firstMessageInterruptionsEnabled": False, "maxDurationSeconds": 90,
                 "endCallPhrases": ["bye for now"],
                 "serverMessages": [], "voice": deepcopy(profile["voice"]),
                 "metadata": metadata,
                 "model": {"provider": profile["model"]["provider"], "model": profile["model"]["model"],
                           "messages": [{"role": "system", "content": "This is a single-message callback. Read only the provided checked summary and actual SMS status, ending with Bye for now. Do not ask for acknowledgement or another question. Do not add facts, make bookings, send messages or promise another call. The closing phrase ends the call after speech finishes; never pronounce tool names. Summary: " + speech}],
                           "tools": [{"type": "endCall"}]}}
    if profile.get("transcriber"):
        assistant["transcriber"] = deepcopy(profile["transcriber"])
    return {"name": "Travel research callback", "phoneNumberId": record["identity"]["phone_number_id"],
            "customer": record["identity"]["customer"], "assistant": assistant}


def tool_definition(slug, secret):
    strings = {k: {"type": "string"} for k in ("question", "visit_date", "location", "consent_evidence", "confirmed_mobile")}
    strings["question"]["description"] = "Preserve the complete original request, including the actual recommendation or named exhibition wanted, visit time, interests and transport preferences or alternatives. Do not replace that goal with a venue logistics checklist."
    strings["visit_date"]["description"] = "Exact YYYY-MM-DD date resolved in Europe/London, including tonight/today/tomorrow. Keep this precision internally; do not read out a long date unless needed to resolve ambiguity."
    strings["location"]["description"] = "Caller-stated location when relevant. If no starting point was supplied, record 'starting point not supplied'; do not invent a location or ask for one solely for a current-location Maps link."
    strings["consent_evidence"]["description"] = "Truthful authorization basis: the actual caller request, any explicitly configured private-helper delivery defaults, and any opt-outs. Do not invent or quote a yes that the caller did not say."
    strings["confirmed_mobile"]["description"] = "Confirmed E.164 fallback mobile only when caller ID is missing or unsuitable. Never replace a suitable original caller number."
    return {"type": "function", "async": False,
            "function": {"name": "requestTravelResearch", "description": "Save the complete London research request once the caller has finished. Preserve the requested recommendation and select only relevant required_fields. Follow this helper's configured delivery policy and all caller opt-outs; do not repeat permission questions for delivery already requested or explicitly configured as a private default. Record the real authorization basis, not invented consent words. At least one delivery channel must be authorized. Use the original suitable caller number; an original landline can receive a callback-only request without collecting a mobile. Confirm an alternate mobile only when genuinely needed. The fixed success message follows durable storage and ends this call; do not add a separate acknowledgement or narrate a tool name.",
                         "parameters": {"type": "object", "additionalProperties": False,
                                        "properties": {**strings, "required_fields": {"type": "array", "description": "Only facts needed to answer the actual request, not every available field. Use other_fact for a recommendation or named exhibition. Add dated_availability, closing_time, price, visitor_address or route only when relevant. Do not substitute logistics for the requested recommendation.", "items": {"type": "string", "enum": list(FIELDS)}, "minItems": 1}, "travel_mode": {"type": "string", "description": "Primary requested mode. Tube or bus with walking uses transit; preserve walking and taxi alternatives in question. Do not ask again for a mode already supplied.", "enum": ["walking", "transit", "driving", "bicycling"]}, "expected_revision": {"type": "integer", "minimum": 0}, "sms_consent": {"type": "boolean", "description": "Directions SMS authorized by the actual request or explicitly configured private-helper default, unless declined. False for an unsuitable SMS number without a confirmed fallback; an original landline can still receive a callback."}, "callback_consent": {"type": "boolean", "description": "One callback authorized by the actual request or explicitly configured private-helper default, unless declined. Do not ask again for an already requested callback."}, "mobile_confirmed": {"type": "boolean", "description": "True only when an alternate mobile was actually read back and confirmed; private delivery defaults do not confirm a new number."}},
                                        "required": ["question", "visit_date", "location", "travel_mode", "required_fields", "expected_revision", "sms_consent", "callback_consent", "consent_evidence"]}},
            "server": {"url": "https://api.callmeie.ie/vapi/travel-helper/" + slug, "headers": {"x-travel-helper-secret": secret}, "timeoutSeconds": 10},
            "messages": [{"type": "request-start", "content": ""},
                         {"type": "request-complete", "role": "assistant", "content": "Saved. I'll check it and get back to you. Bye.", "endCallAfterSpokenEnabled": True},
                         {"type": "request-failed", "role": "assistant", "content": "I can't arrange the follow-up right now. I can help here.", "endCallAfterSpokenEnabled": False}]}


def install(app, service):
    router = APIRouter()

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= 180000, "body_too_large", 413)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "invalid_json")
        require(isinstance(value, dict), "invalid_object")
        return value

    @router.post("/internal/travel-research/worker/{operation}")
    async def worker(operation: str, request: Request, background_tasks: BackgroundTasks):
        service.auth(request.headers.get("x-research-worker-secret", ""))
        require(service.admitted(service.policy.helper), "research_disabled", 503)
        value = await body(request)
        if operation == "pending":
            exact(value, set())
            background_tasks.add_task(service.advance_pending)
            return {"request": service.pending()}
        if operation == "sources":
            return await service.sources(value)
        actions = {"heartbeat": service.heartbeat, "claim": service.claim, "receipt": service.receipt, "result": service.result}
        require(operation in actions, "unsupported_operation", 404)
        result = actions[operation](value)
        if operation in ("heartbeat", "result"):
            background_tasks.add_task(service.advance_pending)
        return result

    @router.post("/internal/travel-research/deliver")
    async def delivery(request: Request):
        service.auth(request.headers.get("x-research-delivery-secret", ""), delivery=True)
        return await service.deliver(await body(request))

    app.include_router(router)
