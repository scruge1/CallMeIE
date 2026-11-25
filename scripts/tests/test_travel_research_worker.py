"""Test the separately staged patch when its artifact directory is supplied."""
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import travel_research_native as native

ARTIFACT = os.getenv("TRAVEL_RESEARCH_WORKER_PATCH_DIR")


@unittest.skipUnless(ARTIFACT, "staged worker patch directory not supplied")
class WorkerPatchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(ARTIFACT)
        self.original = self.root / "worker-preimage.py"
        self.candidate = self.root / "windows_drive_owner_worker.candidate.py"
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def load(self, path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exact_preimage_and_unchanged_authority_functions(self):
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).hexdigest(), "e80542c812cf1f8995116660c3ca81fd259ebd00f47bfeca59d388f474ac2144")
        def functions(path):
            return {node.name: ast.dump(node) for node in ast.parse(path.read_text("utf-8")).body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        before, after = functions(self.original), functions(self.candidate)
        self.assertEqual(set(before), set(after))
        self.assertEqual([key for key in before if before[key] != after[key]], ["main"])

    def main_trace(self, path, once=False, enabled=False):
        module = self.load(path, "offline_worker_fixture")
        calls = []
        module.ROOT = Path(self.temp.name)
        module.LOGS = module.ROOT / "logs"
        module.owner_config = lambda: calls.append("owner_config") or {"inbox_id": "fixture"}
        module.db_open = lambda: calls.append("db_open") or Mock(execute=lambda *args: Mock(fetchall=lambda: []))
        module.service = lambda: calls.append("service") or "fixture-service"
        module.jobs_in_inbox = lambda *args: calls.append("inbox") or []
        def sleep(seconds):
            calls.append(("sleep", seconds))
            raise KeyboardInterrupt()
        module.time = Mock(sleep=sleep)
        with patch.object(sys, "argv", [str(path)] + (["--once"] if once else [])), patch.dict(os.environ, {"TRAVEL_RESEARCH_CONFIG": "fixture"} if enabled else {}, clear=True), patch("msvcrt.locking"), patch.object(native, "optional_tick", side_effect=lambda *args: calls.append("research_tick")):
            module.main()
        return calls

    def test_disabled_continuous_mode_matches_original_without_research_io(self):
        self.assertEqual(self.main_trace(self.original), self.main_trace(self.candidate))

    def test_once_mode_is_unchanged_even_with_research_config(self):
        self.assertEqual(self.main_trace(self.original, once=True, enabled=True), self.main_trace(self.candidate, once=True, enabled=True))

    def test_optional_tick_runs_once_without_owner_backoff(self):
        trace = self.main_trace(self.candidate, enabled=True)
        self.assertEqual(trace.count("research_tick"), 1)
        self.assertEqual(trace[-1], ("sleep", 5))

    @unittest.skipUnless(os.name == "nt", "Windows containment primitive")
    def test_real_job_contains_and_terminates_only_its_owned_fixture(self):
        module = self.load(self.candidate, "offline_job_fixture")
        job = module.WindowsCommandJob()
        process = None
        try:
            # Harmless child waits at the gate; no model or external operation.
            process = subprocess.Popen([sys.executable, "-u", "-c", "import sys,time; sys.stdin.readline(); print('released',flush=True); time.sleep(60)"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process)
            identity = native.process_identity(process)
            self.assertEqual(identity["pid"], process.pid)
            self.assertTrue(identity["process_created"].isdigit())
            process.stdin.write(b"go\n")
            process.stdin.flush()
            self.assertEqual(process.stdout.readline().strip(), b"released")
            job.terminate()
            self.assertEqual(process.wait(timeout=3), 124)
        finally:
            job.close()
            if process is not None:
                process.communicate(timeout=3)
