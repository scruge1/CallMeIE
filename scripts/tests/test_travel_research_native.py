"""No-network native lifecycle regressions: exact execution, evidence and return."""
import copy
from concurrent.futures import Future
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import travel_research_native as native
from travel_research_contract import canonical, digest, failed_result, source_manifest, verification_input


class ImmediateExecutor:
    def submit(self, function, *args):
        result = Future()
        try:
            result.set_result(function(*args))
        except BaseException as exc:
            result.set_exception(exc)
        return result

    def shutdown(self, *args, **kwargs):
        pass


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.clock = patch.object(native.time, "time", side_effect=lambda: self.now)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.sleep = patch.object(native.time, "sleep")
        self.sleep.start(); self.addCleanup(self.sleep.stop)
        self.executor = patch.object(native, "ThreadPoolExecutor", side_effect=lambda **kwargs: ImmediateExecutor())
        self.executor.start(); self.addCleanup(self.executor.stop)
        self.brief = {"question": "What are the opening and closing times?", "visit_date": "2026-11-26",
                      "location": "Tower of London", "travel_mode": "walking",
                      "required_fields": ["opening_time", "closing_time"], "official_hosts": ["venue.example"]}
        self.request = {"request_id": "travel-research:fixture", "revision": 1,
                        "brief": self.brief, "brief_hash": digest(self.brief), "deadline": self.now + 120}
        self.claim = {**self.request, "execution_id": "fixture-execution"}
        self.cfg = {"output_root": str(self.root), "python": "fixture-python", "node": "node.exe",
                    "cli": "codex.js", "profile_pin": "fixture"}
        self.calls = []
        self.handlers = {}
        self.transport = Mock()
        self.transport.post.side_effect = self.post
        self.candidate = {"visit_date": self.brief["visit_date"], "claims": [
            {"field": "opening_time", "text": "Opens at 09:00.", "qualifiers": "Dated schedule",
             "proposed_source_urls": ["https://venue.example/schedule"]}],
            "proposed_urls": ["https://venue.example/schedule"], "uncertainties": [], "tool_errors": []}
        page = "26 November 2026: opens 09:00 and closes 16:30."
        self.snapshots = [{"reference": "source-1", "url": "https://venue.example/schedule", "kind": "page",
                           "retrieved_at": "2026-11-25T20:00:00+00:00", "page_text": page, "content_hash": digest(page)}]
        self.verified = {"status": "complete", "claims": [
            {"field": "opening_time", "text": "Opens at 09:00.", "qualifiers": "26 November 2026",
             "source_reference": "source-1", "quote": page},
            {"field": "closing_time", "text": "Closes at 16:30.", "qualifiers": "26 November 2026",
             "source_reference": "source-1", "quote": page}],
            "missing_coverage": [], "uncertainties": [], "destination": None}

    def post(self, operation, body):
        self.calls.append((operation, copy.deepcopy(body)))
        if operation in self.handlers:
            return self.handlers[operation](body)
        if operation == "pending":
            return {"request": None}
        if operation == "claim":
            return {**self.request, **body, "launch_allowed": True}
        if operation == "sources":
            inputs = verification_input(self.brief, body["candidate"], self.snapshots, [])
            return {"status": "ready", "fetch_attempt_id": body["fetch_attempt_id"], "candidate_hash": body["candidate_hash"],
                    "snapshots": self.snapshots, "fetch_failures": [],
                    "source_manifest_hash": digest(source_manifest(self.snapshots, [])), "verification_input_hash": digest(inputs)}
        return {"stored": True}

    def tick(self, prepare=False, permitted=False, job=None):
        result = native.Tick(self.cfg, self.transport, lambda: job or Mock())
        result.next_heartbeat = self.now + 1000
        if prepare:
            result.prepare_claim(self.request)
        if permitted:
            result.accept_claim_response({**result.claim, "launch_allowed": True})
        return result

    def drain(self, tick, steps=20):
        for _ in range(steps):
            tick.poll()

    def fake_stage(self, cfg, claim, directory, stage, prompt, transport, job_class):
        receipt = native.empty_receipt()
        native.record_receipt(directory, claim, stage, receipt)
        native.flush_journal(directory, claim, transport)
        value = copy.deepcopy(self.candidate if stage == "research" else self.verified)
        receipt.update(state="exited", pid=100 if stage == "research" else 101, process_created="123",
                       thread_id=stage + "-thread", turn_id=stage + "-turn", exit_code=0, completed=True, output_hash=digest(value))
        native.record_receipt(directory, claim, stage, receipt)
        native.flush_journal(directory, claim, transport)
        return value

    def evidence(self, stage="research"):
        return {"thread_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()), "turn_completed": True,
                "rejected_tool_calls": [], "web_actions": [] if stage == "verification" else [{"status": "completed"}],
                "public_wrapper_calls": [], "tool_failures_observed": [],
                "turn_context": [{"model": "gpt-6.1-sol", "approval_policy": "never", "effort": "high", "sandbox_policy": {"type": "read-only"}}],
                "terminal_usage": {"input_tokens": 20, "output_tokens": 10}, "usage_currency_cost": None}

    def stage_process(self, directory, value=None):
        process = Mock(pid=123, returncode=0)
        process.poll.return_value = 0
        identity = {"pid": 456, "process_created": "98765"}
        def spawn(*args, **kwargs):
            native.store(directory / "research.process.json", identity, exclusive=True)
            native.store(directory / "research.exit.json", {**identity, "exit_code": 0}, exclusive=True)
            native.store(directory / "research.output.json", value or self.candidate, exclusive=True)
            return process
        return process, spawn

    def test_disabled_tick_has_no_config_or_network_io(self):
        with patch.object(native, "PROFILE_PIN", None), patch.dict(os.environ, {}, clear=True), patch.object(native, "config") as cfg, patch.object(native, "Transport") as transport:
            native.optional_tick("unused", Mock())
        cfg.assert_not_called(); transport.assert_not_called()

    def test_profile_pin_cannot_be_enabled_by_configuration(self):
        path = self.root / "config.json"
        native.store(path, {"enabled": True, "profile_pin": "invented", "node": "x", "node_sha256": "x", "cli": "x",
            "cli_sha256": "x", "native_binary": "x", "native_sha256": "x", "worker_path": "x", "output_root": "x", "python": "x"})
        with self.assertRaisesRegex(ValueError, "not_qualified"):
            native.config(path)

    def test_profile_uses_qualified_dotted_overrides_without_model_or_auth_change(self):
        command = native.command(self.cfg, self.root, "research")
        self.assertEqual(command[:2], ["node.exe", "codex.js"])
        self.assertIn("notify=[]", command)
        for name in native.MCP:
            self.assertIn("mcp_servers." + name + ".enabled=false", command)
        for name in native.DISABLED:
            self.assertIn("features." + name + "=false", command)
        for argument in ("--model", "--ephemeral", "--ignore-rules", "--ignore-user-config", "resume", "--last", "mcp_servers={}"):
            self.assertNotIn(argument, command)
        self.assertIn('web_search="disabled"', native.command(self.cfg, self.root, "verification"))
        self.assertEqual(command[-1], "-")

    def test_utf8_gate_payload_survives_windows_cp1252_text_stdin(self):
        payload = {"config": {"profile_pin": "fixture"}, "prompt": "Adult £38.00; King’s Cross – London."}
        stream = io.TextIOWrapper(io.BytesIO((canonical(payload) + "\n").encode("utf-8")), encoding="cp1252")
        with patch.object(native, "PROFILE_PIN", "fixture"), patch.object(native.sys, "argv", ["native", "--native-gate"]), \
                patch.object(native.sys, "stdin", stream), patch.object(native, "native_gate", return_value=0) as gate:
            with self.assertRaises(SystemExit) as exited:
                native.main()
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(gate.call_args.args[0], payload)

    def test_only_request_gate_receives_scoped_backend_secret(self):
        env = {"TRAVEL_RESEARCH_WORKER_SECRET": "scope", "VAPI_API_KEY": "vapi", "TWILIO_AUTH_TOKEN": "twilio",
               "COOLIFY_API_TOKEN": "admin", "FIRECRAWL_API_KEY": "search", "CODEX_HOME": "native-auth", "PATH": "runtime"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(native.model_environment(), {"CODEX_HOME": "native-auth", "PATH": "runtime"})
            self.assertEqual(native.worker_environment(), {"CODEX_HOME": "native-auth", "PATH": "runtime", "TRAVEL_RESEARCH_WORKER_SECRET": "scope"})

    def test_transport_cannot_deliver_or_address_arbitrary_endpoint(self):
        client = native.Transport("fixture-secret")
        with patch.object(native.urllib.request, "build_opener") as opener:
            for operation in ("deliver", "https://evil.example", "feedback", "exec", "sources/../../deliver"):
                with self.assertRaises(ValueError): client.post(operation, {})
        opener.assert_not_called()

    def test_source_http_timeout_differs_from_owner_poll_timeout(self):
        response = Mock()
        response.read.return_value = b'{"stored":true}'
        manager = Mock();manager.__enter__ = Mock(return_value=response);manager.__exit__ = Mock(return_value=False)
        opener = Mock();opener.open.return_value = manager
        with patch.object(native.urllib.request, "build_opener", return_value=opener):
            client = native.Transport("fixture-secret")
            client.post("sources", {});self.assertEqual(opener.open.call_args.kwargs["timeout"], 35)
            client.post("heartbeat", {});self.assertEqual(opener.open.call_args.kwargs["timeout"], 2)

    def test_web_wrapper_audit_allows_only_literal_public_research(self):
        parsed = native.literal_web_arguments("text(await tools.web__run({system2_search_query:[{q:'Tower hours'}],response_length:'short'}));")
        self.assertEqual(parsed["system2_search_query"][0]["q"], "Tower hours")
        for source in ("text(await tools.exec_command({cmd:'echo nope'}))", "text(await tools.web__run(JSON.parse(x)))",
                       "let w=tools.web__run; text(await w({open:[]}))", "text(await tools.web__run({finance:[]}))",
                       "text(await tools.web__run({open:[] })); text('extra')"):
            with self.assertRaises(ValueError): native.literal_web_arguments(source)

    def test_claim_identity_is_durable_before_http_post(self):
        tick = self.tick(prepare=True)
        def claim(body):
            self.assertTrue(tick.cursor.exists())
            self.assertEqual(native.read(tick.directory / "claim.json")["execution_id"], body["execution_id"])
            raise TimeoutError("lost response")
        self.handlers["claim"] = claim
        tick.poll();tick.poll()
        self.assertTrue(tick.cursor.exists())
        self.assertEqual([op for op,_ in self.calls], ["claim"])

    def test_lost_claim_ack_replays_same_execution_without_model_launch(self):
        tick = self.tick(prepare=True)
        count = 0
        def claim(body):
            nonlocal count
            count += 1
            if count == 1: raise TimeoutError("server accepted, response lost")
            return {**self.request, **body, "launch_allowed": False}
        self.handlers["claim"] = claim
        with patch.object(native.subprocess, "Popen") as spawn:
            tick.poll();tick.poll();self.now += 6;self.drain(tick)
        spawn.assert_not_called()
        bodies = [body for op,body in self.calls if op == "claim"]
        self.assertEqual(len(bodies), 2);self.assertEqual(bodies[0], bodies[1])
        result = next(body["result"] for op,body in self.calls if op == "result")
        self.assertEqual(result["status"], "failed");self.assertEqual(result["claims"], [])
        self.assertFalse((self.root / "active-request.json").exists())

    def test_launch_is_contained_before_payload_and_duplicate_is_forbidden(self):
        order=[];job=Mock();process=Mock(pid=321);process.poll.return_value=None
        job.assign.side_effect=lambda p:order.append("contained")
        process.stdin.write.side_effect=lambda value:order.append("released")
        tick=self.tick(prepare=True,permitted=True,job=job)
        with patch.object(native.subprocess,"Popen",return_value=process) as spawn, patch.object(native.subprocess,"CREATE_NO_WINDOW",0,create=True), patch.object(native,"process_identity",return_value={"pid":321,"process_created":"123"}):
            tick.launch()
            self.assertEqual(order,["contained","released"])
            self.assertTrue((tick.directory/"launch.json").exists())
            with self.assertRaises(FileExistsError):tick.launch()
            spawn.assert_called_once()
        job.close.assert_not_called()
        process.poll.return_value=0;process.returncode=0
        tick.poll()
        job.close.assert_called_once_with()
        self.assertIsNotNone(tick.claim)
        self.assertTrue((tick.directory/"return.json").exists())

    def test_ambiguous_launch_recovers_without_second_process(self):
        job=Mock();job.assign.side_effect=RuntimeError("assign failure")
        process=Mock(pid=321);process.poll.return_value=None
        process.kill.side_effect=lambda:setattr(process.poll,"return_value",-9)
        tick=self.tick(prepare=True,permitted=True,job=job)
        with patch.object(native.subprocess,"Popen",return_value=process) as spawn, patch.object(native.subprocess,"CREATE_NO_WINDOW",0,create=True):
            with self.assertRaises(RuntimeError):tick.launch()
            resumed=self.tick()
            self.drain(resumed)
            spawn.assert_called_once()
        process.kill.assert_called_once();process.stdin.write.assert_not_called();job.close.assert_called_once()
        self.assertFalse(resumed.cursor.exists())

    def test_process_exit_does_not_discard_unacknowledged_return(self):
        tick=self.tick(prepare=True,permitted=True)
        directory=tick.directory
        envelope=native.persist_return(directory,tick.claim,failed_result(self.brief,"Fixture unavailable."))
        receipt=native.empty_receipt();receipt["state"]="unknown"
        native.record_receipt(directory,tick.claim,"research",receipt)
        native.flush_journal(directory,tick.claim,self.transport)
        count=0
        def result(body):
            nonlocal count
            count+=1
            if count==1:raise TimeoutError("accepted, ack lost")
            return {"stored":True}
        self.handlers["result"]=result
        process=Mock(returncode=0);process.poll.return_value=0
        job,handle=Mock(),Mock();tick.active=(process,job,directory,handle);tick.launch_ready=False
        with patch.object(native.subprocess,"Popen") as spawn:
            tick.poll();tick.poll()
            self.assertIsNotNone(tick.claim)
            resumed=self.tick();self.drain(resumed)
        spawn.assert_not_called();job.close.assert_called_once();handle.close.assert_called_once()
        bodies=[body for op,body in self.calls if op=="result"]
        self.assertEqual(bodies,[envelope,envelope]);self.assertFalse(resumed.cursor.exists())

    def test_restart_replays_receipts_before_identical_late_return(self):
        tick=self.tick(prepare=True,permitted=True);directory=tick.directory;claim=tick.claim
        native.store(directory/"launch.json",{**native.binding(claim),"state":"attempted"},exclusive=True)
        receipt=native.empty_receipt();native.record_receipt(directory,claim,"research",receipt)
        receipt.update(state="running",pid=44,process_created="123")
        native.record_receipt(directory,claim,"research",receipt)
        self.now=claim["deadline"]+1
        self.handlers["result"]=lambda body:{"stored":True,"expired":True,"no_effects":True}
        with patch.object(native.subprocess,"Popen") as spawn:
            resumed=self.tick();self.drain(resumed)
        spawn.assert_not_called()
        stages=[body["receipt"] for op,body in self.calls if op=="receipt"]
        self.assertEqual([x["state"] for x in stages],["attempted","running","unknown"])
        self.assertEqual(len({x["attempt_id"] for x in stages}),1)
        self.assertEqual(stages[-1]["pid"],44)
        self.assertTrue(native.read(directory/"return.ack.json")["response"]["no_effects"])
        self.assertFalse(resumed.cursor.exists())

    def test_explicit_never_claimed_expiry_closes_cursor_without_spawn(self):
        tick=self.tick(prepare=True)
        directory=tick.directory
        def denied(body):raise native.WorkerHTTPError("claim",409,"request_expired")
        self.handlers["claim"]=denied
        with patch.object(native.subprocess,"Popen") as spawn:self.drain(tick)
        spawn.assert_not_called();self.assertFalse(tick.cursor.exists())
        self.assertEqual(native.read(directory/"claim-rejected.json")["reason"],"request_expired")

    def test_tampered_return_is_not_replayed(self):
        tick=self.tick(prepare=True,permitted=True)
        body=native.persist_return(tick.directory,tick.claim,failed_result(self.brief,"Fixture unavailable."))
        body["result_hash"]="0"*64;native.store(tick.directory/"return.json",body)
        with self.assertRaisesRegex(ValueError,"return_binding"):
            self.tick()
        self.assertFalse(self.calls)

    def test_initial_receipt_timeout_never_launches_native_and_can_reconcile(self):
        self.handlers["receipt"]=lambda body:(_ for _ in ()).throw(TimeoutError("lost"))
        with patch.object(native.subprocess,"Popen") as spawn:
            with self.assertRaises(TimeoutError):native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,Mock())
            with self.assertRaises(FileExistsError):native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,Mock())
        spawn.assert_not_called()
        self.handlers.pop("receipt")
        native.flush_journal(self.root,self.claim,self.transport)
        states=[body["receipt"]["state"] for op,body in self.calls if op=="receipt"]
        self.assertEqual(states,["attempted","attempted","unknown"])
        self.assertEqual(native.read(self.root/"research.receipt.json")["state"],"unknown")

    def test_completed_stage_retains_usage_identity_and_exact_output(self):
        process,spawn=self.stage_process(self.root);job=Mock()
        with patch.object(native.subprocess,"Popen",side_effect=spawn) as create, patch.object(native.subprocess,"CREATE_NO_WINDOW",0,create=True), patch.object(native,"process_identity",return_value={"pid":123,"process_created":"gate"}), patch.object(native,"native_evidence",return_value=self.evidence()):
            value=native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,lambda:job)
            with self.assertRaises(FileExistsError):native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,lambda:job)
            create.assert_called_once()
        self.assertEqual(value,self.candidate)
        receipt=native.read(self.root/"research.receipt.json")
        self.assertTrue(receipt["completed"]);self.assertEqual(receipt["pid"],456);self.assertEqual(receipt["output_hash"],digest(value))
        audit=native.read(self.root/"research.audit.json")
        self.assertEqual(audit["native_evidence"]["terminal_usage"],{"input_tokens":20,"output_tokens":10})
        self.assertIsNone(audit["native_evidence"]["usage_currency_cost"])
        job.close.assert_called_once()

    def test_terminal_receipt_ack_loss_does_not_change_completed_native_evidence(self):
        process,spawn=self.stage_process(self.root);job=Mock()
        def receipt(body):
            if body["receipt"]["state"]=="exited":raise TimeoutError("lost final receipt ack")
            return {"stored":True}
        self.handlers["receipt"]=receipt
        with patch.object(native.subprocess,"Popen",side_effect=spawn) as create,patch.object(native.subprocess,"CREATE_NO_WINDOW",0,create=True),patch.object(native,"process_identity",return_value={"pid":123,"process_created":"gate"}),patch.object(native,"native_evidence",return_value=self.evidence()):
            with self.assertRaises(TimeoutError):native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,lambda:job)
        self.assertTrue(native.read(self.root/"research.receipt.json")["completed"])
        self.handlers.pop("receipt");native.flush_journal(self.root,self.claim,self.transport)
        create.assert_called_once()
        terminal=[body for op,body in self.calls if op=="receipt" and body["receipt"]["state"]=="exited"]
        self.assertEqual(len(terminal),2);self.assertEqual(terminal[0],terminal[1])

    def test_observed_sandbox_drift_prevents_successful_return(self):
        process,spawn=self.stage_process(self.root);job=Mock()
        evidence=self.evidence()
        evidence["turn_context"][0]["sandbox_policy"]={"type":"workspace-write"}
        with patch.object(native.subprocess,"Popen",side_effect=spawn) as create,patch.object(native.subprocess,"CREATE_NO_WINDOW",0,create=True),patch.object(native,"process_identity",return_value={"pid":123,"process_created":"gate"}),patch.object(native,"native_evidence",return_value=evidence):
            with self.assertRaisesRegex(ValueError,"native_profile_drift"):
                native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,lambda:job)
        create.assert_called_once()
        receipt=native.read(self.root/"research.receipt.json")
        self.assertFalse(receipt["completed"]);self.assertIsNone(receipt["output_hash"])
        job.close.assert_called_once()

    def test_expired_stage_records_failure_before_any_process(self):
        claim={**self.claim,"deadline":self.now-1}
        with patch.object(native.subprocess,"Popen") as spawn:
            with self.assertRaisesRegex(ValueError,"deadline_before_native_stage"):
                native.run_stage(self.cfg,claim,self.root,"research","brief",self.transport,Mock())
        spawn.assert_not_called();self.assertEqual(native.read(self.root/"research.receipt.json")["state"],"unknown")

    def test_late_stage_cannot_consume_the_reserved_delivery_minute(self):
        claim={**self.claim,"deadline":self.now+69}
        with patch.object(native.subprocess,"Popen") as spawn:
            with self.assertRaisesRegex(ValueError,"deadline_before_native_stage"):
                native.run_stage(self.cfg,claim,self.root,"verification","brief",self.transport,Mock())
        spawn.assert_not_called()
        audit=native.read(self.root/"verification.audit.json")
        self.assertEqual(audit["maximum_seconds"],9)
        self.assertEqual(native.RETURN_RESERVE_SECONDS,60)
        self.assertEqual(native.read(self.root/"verification.receipt.json")["state"],"unknown")

    def test_receipt_http_delay_cannot_start_native_after_deadline(self):
        def delayed(body):
            self.now=self.claim["deadline"]+1
            return {"stored":True}
        self.handlers["receipt"]=delayed
        with patch.object(native.subprocess,"Popen") as spawn:
            with self.assertRaisesRegex(ValueError,"deadline_before_native_stage"):
                native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,Mock())
        spawn.assert_not_called()

    def test_adapter_input_is_not_released_if_assignment_crosses_deadline(self):
        job=Mock();process=Mock(pid=321);process.poll.return_value=None
        tick=self.tick(prepare=True,permitted=True,job=job)
        job.assign.side_effect=lambda child:setattr(self,"now",tick.claim["deadline"]+1)
        with patch.object(native.subprocess,"Popen",return_value=process),patch.object(native.subprocess,"CREATE_NO_WINDOW",0,create=True),patch.object(native,"process_identity",return_value={"pid":321,"process_created":"123"}):
            with self.assertRaisesRegex(ValueError,"expired_before_adapter_release"):tick.launch()
        process.stdin.write.assert_not_called();process.kill.assert_called_once();job.close.assert_called_once()

    def test_owner_deadline_terminates_only_active_owned_job_and_heartbeat_continues(self):
        tick=self.tick(prepare=True,permitted=True)
        process=Mock();process.poll.return_value=None
        job,handle=Mock(),Mock();tick.active=(process,job,tick.directory,handle)
        self.now=tick.claim["deadline"]+1;tick.next_heartbeat=0
        with patch.object(native.subprocess,"Popen") as spawn:tick.poll()
        spawn.assert_not_called();job.terminate.assert_called_once()
        self.assertIn("heartbeat",[op for op,_ in self.calls]);self.assertIsNotNone(tick.claim)

    def test_native_stage_timeout_terminates_owned_job_and_keeps_attempt_identity(self):
        process=Mock(pid=123,returncode=124);process.poll.return_value=None
        process.wait.side_effect=lambda timeout:setattr(process.poll,"return_value",124)
        job=Mock()
        def spawn(*args,**kwargs):
            native.store(self.root/"research.process.json",{"pid":456,"process_created":"987"},exclusive=True)
            return process
        with patch.object(native.subprocess,"Popen",side_effect=spawn),patch.object(native.subprocess,"CREATE_NO_WINDOW",0,create=True),patch.object(native,"process_identity",return_value={"pid":123,"process_created":"gate"}),patch.object(native.time,"monotonic",side_effect=[10,10,999,1000]):
            with self.assertRaisesRegex(TimeoutError,"native_stage_deadline"):
                native.run_stage(self.cfg,self.claim,self.root,"research","brief",self.transport,lambda:job)
        job.terminate.assert_called_once();job.close.assert_called_once()
        receipt=native.read(self.root/"research.receipt.json")
        self.assertEqual(receipt["state"],"unknown");self.assertEqual(receipt["pid"],456)
        self.assertFalse(receipt["completed"])

    def test_original_brief_and_backend_snapshots_reach_separate_verifier(self):
        prompts=[]
        def stage(*args):
            prompts.append((args[3],args[4]));return self.fake_stage(*args)
        with patch.object(native,"run_stage",side_effect=stage):
            envelope=native.run_request(self.cfg,self.claim,self.root,self.transport,Mock())
        self.assertEqual([stage for stage,_ in prompts],["research","verification"])
        data=json.loads(prompts[1][1].removeprefix(native.VERIFY_PROMPT))
        self.assertEqual(data["brief"],self.brief);self.assertEqual(data["trusted_snapshots"],self.snapshots)
        self.assertEqual(data["candidate"],self.candidate)
        verification=envelope["result"]["verification"]
        self.assertEqual(verification["verification_input_hash"],digest(data))
        self.assertEqual(verification["native_output_hash"],digest(self.verified))
        self.assertNotIn("sources",envelope["result"])
        self.assertTrue((self.root/"return.ack.json").exists())

    def test_changed_trusted_source_hash_prevents_verifier_and_returns_failure(self):
        original=self.post
        def bad_sources(body):
            result={"status":"ready","fetch_attempt_id":body["fetch_attempt_id"],"candidate_hash":body["candidate_hash"],
                "snapshots":self.snapshots,"fetch_failures":[],"source_manifest_hash":"0"*64,"verification_input_hash":"0"*64}
            return result
        self.handlers["sources"]=bad_sources
        with patch.object(native,"run_stage",side_effect=self.fake_stage) as stage:
            envelope=native.run_request(self.cfg,self.claim,self.root,self.transport,Mock())
        self.assertEqual(stage.call_count,1);self.assertEqual(envelope["result"]["status"],"failed")
        self.assertEqual(len(envelope["result"]["missing_coverage"]),2)

    def test_pending_fetch_replays_exact_reservation_without_model_retry(self):
        self.handlers["sources"]=lambda body:{"status":"pending","fetch_attempt_id":body["fetch_attempt_id"]}
        with patch.object(native,"run_stage",side_effect=self.fake_stage) as stage:
            envelope=native.run_request(self.cfg,self.claim,self.root,self.transport,Mock())
        bodies=[body for op,body in self.calls if op=="sources"]
        self.assertEqual(len(bodies),4);self.assertTrue(all(body==bodies[0] for body in bodies))
        self.assertEqual(stage.call_count,1);self.assertEqual(envelope["result"]["status"],"failed")

    def test_verifier_missing_requested_field_becomes_honest_failure(self):
        self.verified["claims"].pop()
        with patch.object(native,"run_stage",side_effect=self.fake_stage):
            envelope=native.run_request(self.cfg,self.claim,self.root,self.transport,Mock())
        self.assertEqual(envelope["result"]["status"],"failed");self.assertIsNone(envelope["result"]["verification"])
        self.assertEqual({x["field"] for x in envelope["result"]["missing_coverage"]},set(self.brief["required_fields"]))

    def test_failed_verifier_has_no_verification_success_envelope(self):
        self.verified={key:value for key,value in failed_result(self.brief,"No supported facts.").items() if key not in ("schema","verification")}
        with patch.object(native,"run_stage",side_effect=self.fake_stage):
            envelope=native.run_request(self.cfg,self.claim,self.root,self.transport,Mock())
        self.assertEqual(envelope["result"]["status"],"failed");self.assertIsNone(envelope["result"]["verification"])
        self.assertEqual(envelope["result"]["uncertainties"],["No supported facts."])

    def test_exact_native_session_audit_retains_usage_and_rejects_verifier_tools(self):
        thread,turn=str(uuid.uuid4()),str(uuid.uuid4())
        events=[{"type":"thread.started","thread_id":thread},{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":7}}]
        (self.root/"verification.events.jsonl").write_text("\n".join(json.dumps(x) for x in events))
        directory=self.root/"sessions"/"2026"/"11"/"25";directory.mkdir(parents=True)
        rows=[{"type":"session_meta","payload":{"id":thread,"source":"exec"}},
              {"type":"event_msg","payload":{"type":"task_started","turn_id":turn}},
              {"type":"turn_context","payload":{"turn_id":turn,"model":"gpt-6.1-sol","effort":"high","approval_policy":"never"}},
              {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"total_tokens":19}}}},
              {"type":"response_item","payload":{"type":"custom_tool_call","name":"exec","call_id":"tool-1","input":"text(await tools.web__run({open:[{ref_id:'https://venue.example'}]}))"}},
              {"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"tool-1","output":"public page"}}]
        (directory/("rollout-"+thread+".jsonl")).write_text("\n".join(json.dumps(x) for x in rows))
        evidence=native.native_evidence(self.root,"verification",home=self.root)
        self.assertEqual(evidence["thread_id"],thread);self.assertEqual(evidence["turn_id"],turn)
        self.assertEqual(evidence["terminal_usage"],{"input_tokens":12,"output_tokens":7})
        self.assertEqual(evidence["session_total_token_usage"],{"total_tokens":19})
        self.assertEqual(len(evidence["rejected_tool_calls"]),1)
        self.assertFalse(evidence["full_builtin_tool_manifest_proven"])


if __name__ == "__main__":
    unittest.main()
