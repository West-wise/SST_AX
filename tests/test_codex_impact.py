"""Offline worker transport, input integrity, session and approval regression tests."""
import copy
import hashlib
import json
import io
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
        response = self.result if result is None else result
        response = {key: value for key, value in response.items()
                    if key not in ("task_id", "source_type", "input_context")}
        return b"\n".join(worker.canonical(event) for event in [
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "item.completed", "item": {"type": "agent_message",
                "text": json.dumps(response)}},
            {"type": "turn.completed"}])

    def execute(self, *, outcome=None, result=None, session=SESSION):
        def fake(args, prompt, cwd, on_session, **kwargs):
            self.last_args = args
            self.last_prompt = prompt.decode("utf-8").split("\n", 1)[0]
            self.last_schema = storage.load_json(cwd / "schema.json")
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
            return worker.run(self.path, self.inputs, "codex")

    def approved_session(self):
        state, log = self.pair()
        manifest, _ = worker.bundle(state, self.inputs)
        return worker.approved_session(self.path, state, log, manifest)

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
        before = self.pair()
        budget = storage.load_json(storage.companion(self.path, "execution"))
        with patch.object(worker, "invoke") as invoke, patch.object(worker, "preflight") as preflight:
            self.assertEqual(worker.run(self.path, self.inputs, "codex"), "VALID")
        invoke.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual(self.pair(), before)
        self.assertEqual(storage.load_json(storage.companion(self.path, "execution")), budget)
        self.assertNotIn("--last", self.last_args)

    def sstd_operations(self):
        fixture = storage.load_json(ROOT / "tests/fixtures/impact/sstd-not-required.json")
        self.path = self.path.with_name(fixture["task"]["task_id"] + ".json")
        self.script("create_task.py", "--source-type", "SSTD_CHANGE",
                    "--source-reference", fixture["task"]["source_reference"],
                    "--risk-level", "MEDIUM", "--task-id", self.path.stem,
                    "--task-directory", str(self.path.parent))
        self.move("ANALYZING")
        self.manifest, self.result = fixture["manifest"], fixture["result"]
        bodies = {
            "source": "Synthetic SSTD CI adds a build dependency and a Release deploy restart. "
                      "The packet fields, units, ranges and behavior are unchanged.",
            "client": "Synthetic SSTC decoder and UI consume the unchanged packet.",
            "instructions": "Synthetic SSTC dependency and Android permission requirements are unchanged.",
        }
        for item in self.manifest["evidence"]:
            raw = bodies[item["evidence_id"]].encode()
            item["content_sha256"] = hashlib.sha256(raw).hexdigest()
            (self.inputs / "evidence" / (item["evidence_id"] + ".txt")).write_bytes(raw)
        self.write_manifest()

    def test_prompt_defines_scope_and_result_consistency(self):
        self.assertEqual(self.execute(), "VALID")
        for rule in (
            "change_required means whether SSTC needs modification",
            "SSTD CI, Release, deployment, service restart",
            "sstd_change_required means an additional SSTD change",
            "protocol, parser, model, numeric values, units, ranges and UI display",
            "Each evidence_id may appear only once in result.evidence",
            "unique within each impact.evidence_ids",
            "The same ID may support multiple impacts",
            "UNKNOWN impact or any unresolved question requires change_required=UNDETERMINED",
            "UNDETERMINED requires at least one unresolved question",
            "NOT_REQUIRED requires risk_level=NONE",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.last_prompt)

    def test_sstd_operations_no_change_preserves_task_authority(self):
        self.sstd_operations()
        before, _ = self.pair()
        self.assertEqual(self.execute(), "VALID")
        state, log = self.pair()
        self.assertEqual(state["status"], "ANALYZING")
        self.assertEqual(state["risk_level"], before["risk_level"])
        record = log["codex_analysis"]
        result = storage.load_json(self.path.parent / record["result_file"])
        self.assertEqual(result["change_required"], "NOT_REQUIRED")
        self.assertEqual(result["approval_reasons"], [])
        self.assertEqual(result["input_context"], self.manifest["input_context"])
        self.assertIn("SSTD CI, Release, deployment, service restart", self.last_prompt)

    def assert_invalid_preserved(self, diagnostic, *, model_invalid=False):
        original = copy.deepcopy(self.result)
        self.assertEqual(self.execute(), "INVALID_RESULT")
        state, log = self.pair()
        self.assertEqual(state["status"], "ANALYSIS_FAILED")
        self.assertEqual(log["stop_reason"], "INVALID_RESULT")
        self.assertIn(diagnostic, log["codex_analysis"]["validation_errors"])
        self.assertIsNone(log["codex_analysis"]["result_file"])
        artifact = self.path.with_name(self.path.stem + ".analysis-1.json")
        if model_invalid:
            self.assertFalse(artifact.exists())
        else:
            self.assertEqual(storage.load_json(artifact), original)
        model_file = self.path.parent / log["codex_analysis"]["model_result_file"]
        expected_model = {key: value for key, value in original.items()
                          if key not in ("task_id", "source_type", "input_context")}
        self.assertEqual(storage.load_json(model_file), expected_model)
        checkpoint = storage.load_json(storage.companion(self.path, "codex-checkpoint"))
        self.assertEqual(checkpoint["state"], state)
        self.assertEqual(checkpoint["log"], log)

    def test_duplicate_evidence_result_preserves_failure(self):
        self.sstd_operations()
        self.result["evidence"].append(dict(self.result["evidence"][0], reason="Another reason."))
        self.assert_invalid_preserved("DUPLICATE_EVIDENCE: result.evidence[3]")

    def test_duplicate_impact_ids_preserves_failure(self):
        self.sstd_operations()
        self.result["impacts"]["dependency"]["evidence_ids"].append("source")
        self.assert_invalid_preserved("DUPLICATE_ITEM: result.impacts.dependency.evidence_ids", model_invalid=True)

    def test_unknown_with_required_preserves_failure(self):
        self.result["impacts"]["dependency"]["status"] = "UNKNOWN"
        self.assert_invalid_preserved("UNDETERMINED_REQUIRED: result.change_required")

    def test_questions_with_not_required_preserves_failure(self):
        self.sstd_operations()
        self.result["unresolved_questions"] = ["Does the supplied packet remain compatible?"]
        self.assert_invalid_preserved("UNDETERMINED_REQUIRED: result.change_required")

    def test_undetermined_without_question_preserves_failure(self):
        self.sstd_operations()
        self.result["change_required"] = "UNDETERMINED"
        self.assert_invalid_preserved("QUESTION_REQUIRED: result.unresolved_questions")

    def test_unknown_with_question_accepts_undetermined(self):
        self.sstd_operations()
        self.result["change_required"] = "UNDETERMINED"
        self.result["risk_level"] = "LOW"
        self.result["impacts"]["protocol_contract"]["status"] = "UNKNOWN"
        self.result["unresolved_questions"] = ["Is the packet behavior compatible?"]
        self.assertEqual(self.execute(), "VALID")
        self.assertEqual(self.pair()[0]["status"], "ANALYZING")

    def test_model_schema_excludes_controller_identity(self):
        self.assertEqual(self.execute(), "VALID")
        for key in ("task_id", "source_type", "input_context"):
            self.assertNotIn(key, self.last_schema["properties"])
            self.assertNotIn(key, self.last_schema["required"])
        self.assertFalse(self.last_schema["additionalProperties"])
        self.assertIn("input_context", storage.load_json(worker.SCHEMA_PATH)["properties"])

    def test_controller_binds_git_revision_and_preserves_model_response(self):
        self.sstd_operations()
        revision = "08f917cf853785cf026c6c32f7af244097ba3a15"
        self.manifest["input_context"]["sstd_revision"] = revision
        self.manifest["evidence"][0]["revision"] = revision
        self.result["input_context"] = copy.deepcopy(self.manifest["input_context"])
        self.write_manifest()
        self.assertEqual(self.execute(), "VALID")
        state, log = self.pair()
        record = log["codex_analysis"]
        model = storage.load_json(self.path.parent / record["model_result_file"])
        result = storage.load_json(self.path.parent / record["result_file"])
        self.assertEqual(worker.digest(model), record["model_result_sha256"])
        for key in ("task_id", "source_type", "input_context"):
            self.assertNotIn(key, model)
            self.assertEqual(result[key], self.manifest[key])
        self.assertEqual(result["input_context"]["sstd_revision"], revision)
        self.assertEqual({key: result[key] for key in model}, model)
        self.assertEqual(worker.digest(result), record["result_sha256"])
        self.assertEqual(state["status"], "ANALYZING")

    def test_ai_supplied_identity_is_rejected_without_overwriting_it(self):
        self.sstd_operations()
        response = copy.deepcopy(self.result)
        response["input_context"]["sstd_revision"] = "08f917cf853785cf026c6c32d893e2961cce0f2"
        # Supply the forbidden field directly, bypassing the normal fake response helper.
        def fake(args, prompt, cwd, on_session, **kwargs):
            on_session(SESSION)
            events = [{"type": "thread.started", "thread_id": SESSION},
                      {"type": "item.completed", "item": {"type": "agent_message",
                       "text": json.dumps(response)}}, {"type": "turn.completed"}]
            return 0, b"\n".join(worker.canonical(event) for event in events), None
        with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=fake):
            self.assertEqual(worker.run(self.path, self.inputs, "codex"), "INVALID_RESULT")
        state, log = self.pair()
        record = log["codex_analysis"]
        self.assertEqual(record["validation_errors"], ["EXTRA_PROPERTY: result"])
        self.assertEqual(storage.load_json(self.path.parent / record["model_result_file"]), response)
        self.assertIsNone(record["result_file"])
        self.assertFalse(self.path.with_name(self.path.stem + ".analysis-1.json").exists())
        self.assertEqual(state["status"], "ANALYSIS_FAILED")

    def test_controller_preserves_request_hash_and_null_revisions(self):
        self.assertEqual(self.execute(), "VALID")
        record = self.pair()[1]["codex_analysis"]
        result = storage.load_json(self.path.parent / record["result_file"])
        context = result["input_context"]
        self.assertEqual(context, self.manifest["input_context"])
        self.assertIsNone(context["sstd_revision"])
        self.assertIsNone(context["sstd_base_revision"])
        self.assertEqual(context["request_sha256"], self.manifest["evidence"][0]["content_sha256"])

    def test_controller_metadata_does_not_exceed_result_budget(self):
        response = {key: value for key, value in self.result.items()
                    if key not in ("task_id", "source_type", "input_context")}
        response["summary"] = ""
        response["summary"] = "X" * (worker.MAX_TOTAL_BYTES - len(worker.canonical(response)))
        candidate = worker.canonical(response)
        self.assertEqual(len(candidate), worker.MAX_TOTAL_BYTES)
        def fake(args, prompt, cwd, on_session, **kwargs):
            on_session(SESSION)
            return 0, b"", None
        with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=fake), \
                patch.object(worker, "parse_events", return_value=(candidate.decode(), None)):
            self.assertEqual(worker.run(self.path, self.inputs, "codex"), "RESULT_SIZE")
        state, log = self.pair()
        record = log["codex_analysis"]
        self.assertEqual(storage.load_json(self.path.parent / record["model_result_file"]), response)
        self.assertIsNone(record["result_file"])
        self.assertFalse(self.path.with_name(self.path.stem + ".analysis-1.json").exists())
        self.assertEqual(state["status"], "ANALYSIS_FAILED")

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
        self.assertEqual(self.execute(result=result), "SECRET_CONTENT")
        for file in self.path.parent.glob("*.json"):
            self.assertNotIn(result["summary"], file.read_text(encoding="utf-8"))

    def test_slack_app_token_result_never_persisted(self):
        result = copy.deepcopy(self.result)
        result["summary"] = "xapp-1-" + "A" * 30
        self.assertEqual(self.execute(result=result), "SECRET_CONTENT")
        self.assertIsNone(self.pair()[1]["codex_analysis"]["result_file"])
        for file in self.path.parent.glob("*.json"):
            self.assertNotIn(result["summary"], file.read_text(encoding="utf-8"))

    def test_approved_session_uses_exact_validated_read_only_analysis(self):
        self.execute()
        self.assertEqual(self.approve(), "IMPLEMENTING")
        before = self.pair()
        self.assertEqual(self.approved_session(), SESSION)
        self.assertEqual(self.pair(), before)
        self.assertEqual(self.pair()[0]["status"], "IMPLEMENTING")
        with self.assertRaises(ValueError):
            worker.run(self.path, self.inputs, "codex")

    def test_rejected_or_unapproved_task_cannot_resume(self):
        self.execute()
        with self.assertRaises(ValueError):
            self.approved_session()
        self.approve(reject=True)
        with self.assertRaises(ValueError):
            self.approved_session()

    def test_changed_result_or_session_invalidates_approval(self):
        self.execute()
        self.approve()
        state, log = self.pair()
        log["codex_analysis"]["session_id"] = OTHER
        storage.save_pair(self.path, state, log)
        with self.assertRaises(ValueError):
            self.approved_session()

    def test_result_hash_checked_again_after_approval(self):
        self.execute()
        self.approve()
        _, log = self.pair()
        result_path = self.path.parent / log["codex_analysis"]["result_file"]
        changed = copy.deepcopy(self.result)
        changed["summary"] = "Changed after approval"
        storage.atomic_json(result_path, changed)
        with self.assertRaises(ValueError):
            self.approved_session()

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
            self.approved_session()

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
        self.assertEqual(self.pair()[0]["status"], "ANALYSIS_FAILED")
        self.assertEqual(self.pair()[1]["stop_reason"], "ATTEMPT_LIMIT")

    def test_next_analysis_uses_remaining_shared_time_and_cannot_claim_success_after_exhaustion(self):
        import execution_policy as policy
        self.assertEqual(self.execute(outcome="TIMEOUT"), "TIMEOUT")
        output = storage.companion(self.path, "execution")
        budget = storage.load_json(output)
        budget["active_seconds"] = 1700
        storage.atomic_json(output, budget)
        with patch.object(policy.time, "monotonic", return_value=10) as clock:
            def final(args, prompt, cwd, on_session, **kwargs):
                self.assertEqual(kwargs["timeout"], 100)
                on_session(SESSION)
                clock.return_value = 111
                return 0, self.events(), None
            with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=final):
                self.assertEqual(worker.run(self.path, self.inputs, "codex"), "EXECUTION_BUDGET_EXHAUSTED")
        state, log = self.pair()
        self.assertEqual(state["status"], "ANALYSIS_FAILED")
        self.assertEqual(log["codex_analysis"]["outcome"], "EXECUTION_BUDGET_EXHAUSTED")
        self.assertEqual(storage.load_json(output)["active_seconds"], 1801)

    def test_exhausted_budget_stops_before_another_analysis_transport(self):
        storage.atomic_json(storage.companion(self.path, "execution"), {
            "active_seconds": 1800, "attempts": 0, "write_steps": 0})
        with patch.object(worker, "invoke") as invoke, self.assertRaisesRegex(ValueError, "EXECUTION_BUDGET_EXHAUSTED"):
            worker.run(self.path, self.inputs, "codex")
        invoke.assert_not_called()
        self.assertEqual(self.pair()[0]["status"], "ANALYSIS_FAILED")
        storage.load_checkpoint(self.path, *self.pair())

    def test_preflight_help_calls_share_remaining_deadline_and_stop_without_analysis(self):
        import execution_policy as policy
        storage.atomic_json(storage.companion(self.path, "execution"), {
            "active_seconds": 1799, "attempts": 0, "write_steps": 0})
        timeouts = []
        with patch.object(policy.time, "monotonic", return_value=10) as clock:
            def help_result(command, **kwargs):
                timeouts.append(kwargs["timeout"])
                if len(timeouts) == 1:
                    clock.return_value = 10.75
                    return subprocess.CompletedProcess(command, 0,
                        b"--ignore-user-config --ignore-rules --output-schema --json")
                clock.return_value = 11.1
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            with patch.object(worker.subprocess, "run", side_effect=help_result), \
                    patch.object(worker, "invoke") as invoke:
                with self.assertRaises(subprocess.TimeoutExpired):
                    worker.run(self.path, self.inputs, "codex")
        self.assertEqual(timeouts, [1, 0.25])
        invoke.assert_not_called()
        state, log = self.pair()
        self.assertEqual(state["status"], "ANALYSIS_FAILED")
        self.assertEqual(log["stop_reason"], "EXECUTION_BUDGET_EXHAUSTED")
        storage.load_checkpoint(self.path, state, log)

    def test_deprecated_approved_cli_preserves_approval_and_never_launches_transport(self):
        import run_codex_impact as cli
        self.execute()
        self.approve()
        before = self.pair()
        checkpoint = storage.checkpoint_path(self.path).read_bytes()
        arguments = ["run_codex_impact.py", "--task-file", str(self.path),
                     "--input-directory", str(self.inputs), "--resume-approved"]
        with patch.object(sys, "argv", arguments), patch.object(sys, "stderr", io.StringIO()), \
             patch.object(worker, "invoke") as invoke:
            self.assertEqual(cli.main(), 2)
        invoke.assert_not_called()
        self.assertEqual(self.pair(), before)
        self.assertEqual(storage.checkpoint_path(self.path).read_bytes(), checkpoint)

    def test_session_mismatch_is_failure_without_new_session_fallback(self):
        self.execute(outcome="TIMEOUT")
        self.assertEqual(self.execute(session=OTHER), "SESSION_MISMATCH")
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
        self.assertEqual(fault, "EVENT_LIMIT")

    def test_reader_start_failure_terminates_the_real_child_before_waiting(self):
        import time
        processes = []
        started = time.monotonic()
        with patch.object(worker.threading.Thread, "start", side_effect=RuntimeError("thread unavailable")):
            _, _, fault = worker.invoke([sys.executable, "-c", "import time;time.sleep(5)"],
                b"", self.root, lambda _: None, timeout=0.05, on_start=processes.append)
        self.assertEqual(fault, "EVENT_STREAM_START_FAILED")
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(storage.process_stopped(processes[0]))

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

    def test_tool_start_and_nonobject_events_are_refused(self):
        for raw, reason in ((b'{"type":"item.started","item":{"type":"command_execution"}}', "UNEXPECTED_TOOL"),
                            (b'[]', "EVENT_FORMAT"),
                            (b'{"type":"item.completed","item":[]}', "EVENT_FORMAT")):
            with self.assertRaisesRegex(ValueError, reason):
                worker.parse_events(raw)

    def test_nonfatal_error_item_can_complete_but_stream_error_cannot(self):
        # Codex rust-v0.153.2 exec_events.rs differentiates these event types.
        warning = worker.canonical({"type": "item.completed", "item": {
            "type": "error", "message": "A nonfatal notice"}})
        final, outcome = worker.parse_events(warning + b"\n" + self.events())
        self.assertIsNone(outcome)
        expected = {key: value for key, value in self.result.items()
                    if key not in ("task_id", "source_type", "input_context")}
        self.assertEqual(json.loads(final), expected)
        error = worker.canonical({"type": "error", "message": "An unrecoverable stream error"})
        self.assertEqual(worker.parse_events(error + b"\n" + self.events())[1], "CODEX_FAILED")

    def test_stream_error_and_result_failures_keep_fixed_safe_reasons(self):
        for reason in ("UNEXPECTED_TOOL", "SESSION_MISMATCH", "INPUT_CHANGED", "RESULT_SIZE"):
            with self.subTest(reason=reason):
                self.assertEqual(worker.safe_reason(ValueError(reason)), reason)
        self.assertEqual(worker.safe_reason(ValueError("password=untrusted-private-value")),
                         "EXECUTION_OR_RESULT_ERROR")

    def test_launch_failure_does_not_spend_analysis_attempt(self):
        with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=FileNotFoundError):
            self.assertEqual(worker.run(self.path, self.inputs, "missing-codex"), "EXECUTABLE_NOT_FOUND")
        self.assertEqual(self.pair()[0]["attempt"], 0)
        self.assertEqual(self.pair()[0]["status"], "ANALYZING")
        self.assertEqual(self.execute(), "VALID")

    def test_missing_executable_and_preflight_timeout_keep_retryable_state(self):
        for error, outcome in ((FileNotFoundError("sensitive path"), "EXECUTABLE_NOT_FOUND"),
                               (subprocess.TimeoutExpired("private command", 1), "PREFLIGHT_TIMEOUT")):
            with patch.object(worker, "preflight", side_effect=error), patch.object(worker, "invoke") as invoke:
                self.assertEqual(worker.run(self.path, self.inputs, "codex"), outcome)
            invoke.assert_not_called()
            self.assertEqual(self.pair()[0]["attempt"], 0)
            self.assertEqual(self.pair()[1]["stop_reason"], outcome)

    def test_launch_permission_failure_does_not_consume_attempt_or_fail_task(self):
        with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=PermissionError):
            self.assertEqual(worker.run(self.path, self.inputs, "codex"), "EXECUTABLE_UNAVAILABLE")
        self.assertEqual(self.pair()[0]["attempt"], 0)
        self.assertEqual(self.pair()[0]["status"], "ANALYZING")
        self.assertEqual(self.execute(), "VALID")

    def test_interrupt_before_launch_closes_active_budget_without_spending_attempt(self):
        real_write = storage.atomic_json
        def interrupt(path, value):
            if path.name == "schema.json":
                raise KeyboardInterrupt
            real_write(path, value)
        with patch.object(worker, "preflight"), patch.object(worker, "atomic_json", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker.run(self.path, self.inputs, "codex")
        self.assertFalse(storage.load_json(storage.companion(self.path, "execution"))["active"])
        self.assertEqual(self.pair()[0]["attempt"], 0)
        self.assertNotIn("codex_analysis", self.pair()[1])

    def test_codex_failure_retries_same_session_and_eventually_terminates(self):
        self.assertEqual(self.execute(outcome="CODEX_FAILED"), "CODEX_FAILED")
        self.assertEqual(self.pair()[0]["status"], "ANALYZING")
        self.assertEqual(self.execute(), "VALID")
        self.assertIn(SESSION, self.last_args)

    def test_keyboard_interrupt_is_saved_and_propagated(self):
        with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker.run(self.path, self.inputs, "codex")
        self.assertEqual(self.pair()[1]["codex_analysis"]["outcome"], "INTERRUPTED")
        self.assertEqual(self.pair()[0]["status"], "ANALYZING")
        self.assertEqual(self.execute(), "VALID")

    def test_missing_approval_is_a_fixed_refusal(self):
        self.execute()
        self.approve()
        state, log = self.pair()
        log.pop("approvals")
        with self.assertRaisesRegex(ValueError, "APPROVAL_REQUIRED"):
            worker.approved_session(self.path, state, log, self.manifest)

    def test_schema_preserves_nullable_types_and_refuses_unknown_semantics(self):
        schema = worker.output_schema(storage.load_json(worker.SCHEMA_PATH))
        context = schema["$defs"]["input_context"]
        self.assertEqual(context["properties"]["sstc_revision"]["type"], ["string", "null"])
        self.assertEqual(schema["properties"]["input_context"], {"$ref": "#/$defs/input_context"})
        for keyword in ("anyOf", "oneOf", "allOf"):
            with self.assertRaisesRegex(ValueError, "SCHEMA_UNSUPPORTED_KEYWORD"):
                worker.output_schema({keyword: [{"type": "string"}, {"type": "null"}]})

    def test_proxy_and_ca_settings_pass_without_controller_secrets(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "https://proxy.invalid:443",
                                    "SSL_CERT_FILE": "/trusted/company-ca.pem",
                                    "SLACK_BOT_TOKEN": "private"}):
            environment = worker.worker_environment()
        self.assertEqual(environment["HTTPS_PROXY"], "https://proxy.invalid:443")
        self.assertEqual(environment["SSL_CERT_FILE"], "/trusted/company-ca.pem")
        self.assertNotIn("SLACK_BOT_TOKEN", environment)

    def test_bundle_defends_evidence_id_even_if_schema_walker_regresses(self):
        for name in ("../escape", "..\\escape", "/escape", "C:escape", ".", "..", "bad\nname"):
            self.manifest["evidence"][0]["evidence_id"] = name
            self.write_manifest()
            with patch.object(worker, "validate_shape", return_value=[]):
                with self.assertRaisesRegex(ValueError, "EVIDENCE_ID"):
                    worker.bundle(self.pair()[0], self.inputs)

    def crash_analysis(self):
        def crash(args, prompt, cwd, on_session, **kwargs):
            kwargs["on_start"](os.getpid())
            on_session(SESSION)
            raise SystemExit(99)
        with patch.object(worker, "preflight"), patch.object(worker, "invoke", side_effect=crash):
            with self.assertRaises(SystemExit):
                worker.run(self.path, self.inputs, "codex")

    def test_running_process_cannot_be_reconciled_or_restarted(self):
        self.crash_analysis()
        before = self.pair()
        with self.assertRaisesRegex(ValueError, "PROCESS_STILL_RUNNING"):
            worker.reconcile_interrupted(self.path, self.inputs, active_seconds=30, write_steps=0)
        with self.assertRaisesRegex(ValueError, "PROCESS_STILL_RUNNING"):
            worker.reconcile_interrupted(self.path, self.inputs, abort=True)
        self.assertEqual(self.pair(), before)
        with self.assertRaisesRegex(ValueError, "INTERRUPTED_RUN_REQUIRES_RECONCILIATION"):
            self.execute()

    def test_stopped_process_can_be_reconciled_without_codex_or_budget_reset(self):
        self.crash_analysis()
        with patch.object(storage, "process_stopped", return_value=True), patch.object(worker, "invoke") as invoke:
            self.assertEqual(worker.reconcile_interrupted(self.path, self.inputs,
                             active_seconds=30, write_steps=0), "INTERRUPTED")
        invoke.assert_not_called()
        self.assertEqual(storage.load_json(storage.companion(self.path, "execution"))["active_seconds"], 30)
        self.assertEqual(self.pair()[0]["attempt"], 1)
        self.assertEqual(self.execute(), "VALID")
        self.assertIn(SESSION, self.last_args)

    def test_uncertain_legacy_running_analysis_can_only_be_aborted(self):
        self.crash_analysis()
        state, log = self.pair()
        log["codex_analysis"].pop("process_id")
        storage.save_pair(self.path, state, log, codex_checkpoint=True)
        with self.assertRaisesRegex(ValueError, "PROCESS_CONFIRMATION_REQUIRED"):
            worker.reconcile_interrupted(self.path, self.inputs, active_seconds=30, write_steps=0)
        self.assertEqual(worker.reconcile_interrupted(self.path, self.inputs, abort=True), "ANALYSIS_FAILED")
        with self.assertRaises(ValueError):
            self.execute()

    def test_cli_errors_do_not_echo_arguments(self):
        result = self.script("run_codex_impact.py", "--secret=do-not-echo", success=False)
        self.assertNotIn("do-not-echo", result.stderr)


if __name__ == "__main__":
    unittest.main()
