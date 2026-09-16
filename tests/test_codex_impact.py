"""Offline worker transport, input integrity, session and approval regression tests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_impact as worker
import slack_approval as approval
import task_storage as storage

ROOT = Path(__file__).resolve().parents[1]
SESSION = "0199a213-81c0-7800-8aa1-bbab2a035a53"
OTHER = "0199a213-81c0-7800-8aa1-bbab2a035a54"


class CodexImpactTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture = storage.load_json(ROOT / "tests/fixtures/impact/sstc-required.json")
        self.path = self.root / "tasks" / (fixture["task"]["task_id"] + ".json")
        self.script("create_task.py", "--source-type", "SSTC_FEATURE",
                    "--source-reference", fixture["task"]["source_reference"],
                    "--risk-level", "HIGH", "--task-id", self.path.stem,
                    "--task-directory", str(self.path.parent))
        self.move("ANALYZING")
        self.inputs = self.root / "inputs"
        (self.inputs / "evidence").mkdir(parents=True)
        self.manifest, self.result = fixture["manifest"], fixture["result"]
        for item in self.manifest["evidence"]:
            raw = ("Synthetic " + item["evidence_id"]).encode()
            item["content_sha256"] = hashlib.sha256(raw).hexdigest()
            (self.inputs / "evidence" / (item["evidence_id"] + ".txt")).write_bytes(raw)
            if item["source"] == "REQUEST":
                item["request_sha256"] = item["content_sha256"]
                self.manifest["input_context"]["request_sha256"] = item["content_sha256"]
        self.result["input_context"] = copy.deepcopy(self.manifest["input_context"])
        self.write_manifest()

    def write_manifest(self):
        storage.atomic_json(self.inputs / "manifest.json", self.manifest)

    def script(self, name, *args, success=True):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / name), *args],
                                capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def move(self, status, *args):
        return self.script("update_task_state.py", "--task-file", str(self.path),
                           "--status", status, *args)

    def pair(self):
        return storage.load_json(self.path), storage.load_json(storage.companion(self.path, "log"))

    def events(self, result=None):
        return b"\n".join(worker.canonical(event) for event in [
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "item.completed", "item": {"type": "agent_message",
                "text": json.dumps(self.result if result is None else result)}},
            {"type": "turn.completed"}])

    def execute(self, *, approved=False, outcome=None, result=None, session=SESSION):
        def fake(args, prompt, cwd, on_session):
            self.last_args = args
            self.assertIn(b"untrusted data", prompt)
            self.assertEqual(list(cwd.iterdir()), [cwd / "schema.json"])
            on_session(session)
            state, log = self.pair()
            self.assertEqual(log["codex_analysis"]["session_id"], session)
            self.assertEqual(storage.load_json(storage.companion(self.path, "codex-checkpoint"))["log"], log)
            if outcome == "RATE_LIMIT":
                return 1, b'{"type":"turn.failed","error":{"message":"usage limit reached"}}', None
            return 0, self.events(result), outcome
        with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=fake):
            return worker.run(self.path, self.inputs, "codex", approved)

    def approve(self, reject=False):
        self.move("WAITING_APPROVAL", "--reason", "UI_CHANGE")
        request = approval.prepare_request(self.path, "T1", "C1", "A1")
        approval.bind_message(self.path, request["nonce"], "123.456")
        payload = {"type": "block_actions", "team": {"id": "T1"}, "api_app_id": "A1",
                   "user": {"id": "U1"}, "channel": {"id": "C1"},
                   "container": {"message_ts": "123.456"},
                   "actions": [{"action_id": "ax_reject" if reject else "ax_approve",
                                "value": request["nonce"]}]}
        return approval.apply_decision(self.path, payload, team_id="T1", channel_id="C1",
                                       app_id="A1", approver_ids={"U1"})

    def test_analysis_validates_and_preserves_authority(self):
        before, _ = self.pair()
        self.assertEqual(self.execute(), "VALID")
        state, log = self.pair()
        for name in ("status", "risk_level", "approval_reason"):
            self.assertEqual(state.get(name), before.get(name))
        record = log["codex_analysis"]
        self.assertEqual(record["session_id"], SESSION)
        self.script("validate_impact_result.py", "--task-file", str(self.path),
                    "--manifest-file", str(self.inputs / "manifest.json"),
                    "--result-file", str(self.path.parent / record["result_file"]))
        self.assertEqual(self.execute(), "VALID")
        self.assertIn("resume", self.last_args)
        self.assertIn(SESSION, self.last_args)
        self.assertNotIn("--last", self.last_args)

    def test_evidence_tampering_refuses_before_launch(self):
        (self.inputs / "evidence/source.txt").write_text("changed", encoding="utf-8")
        with patch.object(worker, "invoke") as invoke, self.assertRaises(ValueError):
            worker.run(self.path, self.inputs, "codex")
        invoke.assert_not_called()
        self.assertEqual(self.pair()[0]["attempt"], 0)

    def test_foreign_manifest_and_traversal_refused(self):
        for key, value in (("task_id", "sstc-feature-20260912-9999"), ("evidence", [
                dict(self.manifest["evidence"][0], evidence_id="../escape")])):
            original = copy.deepcopy(self.manifest)
            self.manifest[key] = value
            self.write_manifest()
            with self.assertRaises(ValueError):
                self.execute()
            self.manifest = original

    def test_invalid_json_result_never_authorizes(self):
        self.assertEqual(self.execute(result={"unexpected": True}), "INVALID_RESULT")
        state, log = self.pair()
        self.assertEqual(state["status"], "ANALYSIS_FAILED")
        self.assertIsNone(log["codex_analysis"]["result_file"])
        self.assertTrue(storage.companion(self.path, "codex-checkpoint").exists())

    def test_escaped_secret_never_persisted(self):
        result = copy.deepcopy(self.result)
        result["summary"] = "ghp_" + "A" * 30
        self.assertEqual(self.execute(result=result), "EXECUTION_OR_RESULT_ERROR")
        for file in self.path.parent.glob("*.json"):
            self.assertNotIn(result["summary"], file.read_text(encoding="utf-8"))

    def test_approved_resume_uses_exact_session_and_stays_read_only(self):
        self.execute()
        self.assertEqual(self.approve(), "IMPLEMENTING")
        self.assertEqual(self.execute(approved=True), "VALID")
        self.assertIn(SESSION, self.last_args)
        self.assertIn("read-only", self.last_args)
        self.assertEqual(self.pair()[0]["status"], "IMPLEMENTING")
        with self.assertRaises(ValueError):
            self.execute(approved=True)

    def test_rejected_or_unapproved_task_cannot_resume(self):
        self.execute()
        with self.assertRaises(ValueError):
            self.execute(approved=True)
        self.approve(reject=True)
        with self.assertRaises(ValueError):
            self.execute(approved=True)

    def test_changed_result_or_session_invalidates_approval(self):
        self.execute()
        self.approve()
        state, log = self.pair()
        log["codex_analysis"]["session_id"] = OTHER
        storage.save_pair(self.path, state, log)
        with self.assertRaises(ValueError):
            self.execute(approved=True)

    def test_result_hash_checked_again_after_approval(self):
        self.execute()
        self.approve()
        _, log = self.pair()
        result_path = self.path.parent / log["codex_analysis"]["result_file"]
        changed = copy.deepcopy(self.result)
        changed["summary"] = "Changed after approval"
        storage.atomic_json(result_path, changed)
        with self.assertRaises(ValueError):
            self.execute(approved=True)

    def test_analysis_resume_requires_session_checkpoint(self):
        self.execute()
        state, log = self.pair()
        log["codex_analysis"]["session_id"] = OTHER
        storage.save_pair(self.path, state, log)
        with self.assertRaises(ValueError):
            self.execute()

    def test_protocol_request_requires_separate_decision(self):
        self.result["impacts"]["protocol_contract"]["status"] = "PRESENT"
        self.result["approval_reasons"].append("PROTOCOL_CHANGE")
        self.assertEqual(self.execute(), "PROTOCOL_APPROVAL_REQUIRED")
        self.assertEqual(self.pair()[0]["status"], "PROTOCOL_APPROVAL_REQUIRED")
        with self.assertRaises(ValueError):
            self.execute(approved=True)

    def test_rate_limit_saves_checkpoint_and_requires_observed_reset(self):
        self.assertEqual(self.execute(outcome="RATE_LIMIT"), "RATE_LIMIT")
        state, log = self.pair()
        self.assertEqual(state["status"], "DEFERRED_RATE_LIMIT")
        self.assertIsNone(state["deferred_until"])
        storage.load_checkpoint(self.path, state, log)
        with self.assertRaises(ValueError):
            self.execute()
        self.script("update_task_state.py", "--task-file", str(self.path),
                    "--record-reset-at", "2999-01-01T00:00:00Z")
        self.script("update_task_state.py", "--task-file", str(self.path),
                    "--status", "ANALYZING", success=False)
        self.script("update_task_state.py", "--task-file", str(self.path),
                    "--record-reset-at", "2000-01-01T00:00:00Z")
        self.move("ANALYZING")
        self.assertEqual(self.execute(), "VALID")
        self.assertIn(SESSION, self.last_args)

    def test_timeout_checkpoint_and_attempt_budget(self):
        for _ in range(3):
            self.assertEqual(self.execute(outcome="TIMEOUT"), "TIMEOUT")
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.pair()[0]["attempt"], 3)

    def test_session_mismatch_is_failure_without_new_session_fallback(self):
        self.execute()
        self.assertEqual(self.execute(session=OTHER), "EXECUTION_OR_RESULT_ERROR")
        self.assertEqual(self.pair()[1]["codex_analysis"]["session_id"], SESSION)

    def test_pending_pair_or_running_receipt_blocks_duplicate_execution(self):
        self.execute()
        state, log = self.pair()
        log["codex_analysis"]["outcome"] = "RUNNING"
        storage.save_pair(self.path, state, log)
        with self.assertRaises(ValueError):
            self.execute()
        storage.atomic_json(storage.companion(self.path, "pending"), {"state": state, "log": log})
        with self.assertRaises(ValueError):
            self.execute()

    def test_process_transport_records_session_and_discards_stderr(self):
        code = "import sys;sys.stdin.buffer.read();sys.stderr.write('sensitive stderr');print(" + repr(self.events().decode()) + ")"
        sessions = []
        result, raw, fault = worker.invoke([sys.executable, "-c", code], b"input", self.root, sessions.append)
        self.assertEqual((result, fault, sessions), (0, None, [SESSION]))
        self.assertNotIn(b"sensitive stderr", raw)
        self.assertIsNone(worker.parse_events(raw)[1])

    def test_process_timeout_and_output_budget(self):
        with patch.object(worker, "TIMEOUT", 0.05):
            _, _, fault = worker.invoke([sys.executable, "-c", "import time;time.sleep(5)"], b"", self.root, lambda _: None)
        self.assertEqual(fault, "TIMEOUT")
        with patch.object(worker, "MAX_EVENTS", 10):
            _, _, fault = worker.invoke([sys.executable, "-c", "print('x'*100)"], b"", self.root, lambda _: None)
        self.assertEqual(fault, "EVENT_STREAM_OR_SESSION")

    def test_flags_and_capability_refusal(self):
        args = worker.command("codex", Path("schema.json"), SESSION)
        for flag in ("read-only", "--ignore-user-config", "--ignore-rules",
                     "features.shell_tool=false", 'approval_policy="never"'):
            self.assertIn(flag, args)
        with patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"old CLI")):
            with self.assertRaises(ValueError):
                worker.preflight("codex", self.root)

    def test_controller_secrets_are_not_inherited(self):
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "private", "GITHUB_TOKEN": "private",
                                    "CODEX_API_KEY": "private", "GIT_CONFIG_COUNT": "1"}):
            environment = worker.worker_environment()
        self.assertTrue({"SLACK_BOT_TOKEN", "GITHUB_TOKEN", "CODEX_API_KEY", "GIT_CONFIG_COUNT"}.isdisjoint(environment))

    def test_sstd_analysis_uses_same_validator(self):
        fixture = storage.load_json(ROOT / "tests/fixtures/impact/sstd-required.json")
        self.path = self.path.parent / (fixture["task"]["task_id"] + ".json")
        self.script("create_task.py", "--source-type", "SSTD_CHANGE",
                    "--source-reference", fixture["task"]["source_reference"],
                    "--task-id", self.path.stem, "--task-directory", str(self.path.parent))
        self.move("ANALYZING")
        self.manifest, self.result = fixture["manifest"], fixture["result"]
        for item in self.manifest["evidence"]:
            raw = ("Synthetic " + item["evidence_id"]).encode()
            item["content_sha256"] = hashlib.sha256(raw).hexdigest()
            (self.inputs / "evidence" / (item["evidence_id"] + ".txt")).write_bytes(raw)
        self.write_manifest()
        self.assertEqual(self.execute(), "VALID")

    def test_incomplete_evidence_requires_undetermined(self):
        self.manifest["evidence"][1]["truncated"] = True
        self.write_manifest()
        self.assertEqual(self.execute(), "INVALID_RESULT")

    def test_tool_events_and_duplicate_json_keys_are_refused(self):
        for raw in (b'{"type":"item.completed","item":{"type":"mcp_tool_call"}}',
                    b'{"type":"error","type":"turn.completed"}'):
            with self.assertRaises(ValueError):
                worker.parse_events(raw)

    def test_cli_errors_do_not_echo_arguments(self):
        result = self.script("run_codex_impact.py", "--secret=do-not-echo", success=False)
        self.assertNotIn("do-not-echo", result.stderr)


if __name__ == "__main__":
    unittest.main()
