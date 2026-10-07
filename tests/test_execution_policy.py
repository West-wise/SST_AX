"""Policy decisions, preserved authority and actual same-worktree recovery."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import execution_policy as policy
import sstc_worker as worker
import task_storage as storage
import test_sstc_worker as fixture
from impact_validation import APPROVALS


class ExecutionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SstcWorkerTest()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.path, self.inputs = self.f.task, self.f.inputs

    def analysis(self, required="REQUIRED", risk="LOW", flags=()):
        state, log = policy.pair(self.path)
        state.update(status="ANALYZING", risk_level="LOW", approval_reason=None, checkpoint_path=None)
        log.update(status="ANALYZING", approvals=[])
        log["state_transitions"].append({"from_status": "IMPLEMENTING", "to_status": "ANALYZING",
                                         "occurred_at": state["updated_at"], "reason": "Synthetic setup"})
        storage.save_pair(self.path, state, log)
        result = self.f.result
        result.update(change_required=required, risk_level=risk, approval_reasons=list(flags))
        for impact in result["impacts"].values():
            impact["status"] = "ABSENT"
        if "UI_CHANGE" in flags:
            result["impacts"]["ui_ux"]["status"] = "PRESENT"
        if required == "UNDETERMINED":
            result["unresolved_questions"] = ["Missing semantic evidence"]
            result["impacts"]["ui_ux"]["status"] = "UNKNOWN"
        self.f.analysis()

    def run_worker(self, invoke, *, resume=False, **kwargs):
        with patch.object(worker, "invoke", side_effect=invoke):
            return worker.run(self.path, self.f.source, self.f.worktree, "ax/sstc-sync/recovery",
                              self.inputs, validation_mode="github", resume=resume, **kwargs)

    def implement(self, executable, session, prompt, cwd, **kwargs):
        self.assertEqual(session, fixture.SESSION)
        path = cwd / "app/src/main/java/example/Client.kt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("package example\n", encoding="utf-8")
        kwargs["on_write"](1)
        return 0, self.f.github_events(), None

    def test_low_and_medium_implement_without_slack_and_bind_all_evidence(self):
        for risk in ("LOW", "MEDIUM"):
            with self.subTest(risk=risk):
                self.analysis(risk=risk)
                policy.authority_path(self.path).unlink(missing_ok=True)
                result = policy.decide(self.path, self.inputs)
                self.assertEqual(result["decision"], "IMPLEMENTING")
                ctx = policy.execution_context(self.path, self.inputs)
                self.assertEqual(ctx["proof"]["authorization"], "POLICY")
                self.assertEqual(ctx["proof"]["manifest_sha256"], policy.digest(ctx["manifest"]))
                self.assertEqual(ctx["proof"]["result_sha256"], policy.digest(ctx["result"]))
                self.assertEqual(ctx["proof"]["session_id"], fixture.SESSION)

    def test_valid_no_change_completes_and_unknown_stops(self):
        self.analysis(required="NOT_REQUIRED", risk="NONE")
        self.assertEqual(policy.decide(self.path, self.inputs)["decision"], "COMPLETED")
        self.assertFalse(policy.authority_path(self.path).exists())
        self.analysis(required="UNDETERMINED")
        self.assertEqual(policy.decide(self.path, self.inputs)["decision"], "ANALYSIS_FAILED")
        self.assertFalse(policy.authority_path(self.path).exists())

    def test_high_and_ui_low_require_actual_slack_and_cannot_mint_auto_authority(self):
        for risk, flags in (("HIGH", ("HIGH_RISK",)), ("LOW", ("UI_CHANGE",))):
            with self.subTest(risk=risk):
                self.analysis(risk=risk, flags=flags)
                self.assertEqual(policy.decide(self.path, self.inputs)["decision"], "WAITING_APPROVAL")
                state, log = policy.pair(self.path)
                checkpoint = storage.load_json(storage.checkpoint_path(self.path))
                self.assertEqual(checkpoint["state"], state)
                self.assertEqual(checkpoint["log"], log)
                self.assertFalse(policy.authority_path(self.path).exists())
                with self.assertRaises(ValueError):
                    policy.execution_context(self.path, self.inputs)

    def test_generic_implementing_state_does_not_grant_automatic_permission(self):
        self.analysis()
        self.f.move("IMPLEMENTING")
        with self.assertRaises((ValueError, IndexError)):
            policy.execution_context(self.path, self.inputs)
        self.assertFalse(policy.authority_path(self.path).exists())

    def test_auto_worker_consumes_same_session_pinned_target_guidance_and_no_local_gradle(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        before = policy.authority_path(self.path).read_bytes()
        self.assertEqual(self.run_worker(self.implement), "IMPLEMENTED")
        state, log = policy.pair(self.path)
        self.assertEqual(state["status"], "IMPLEMENTING")
        self.assertEqual(log.get("approvals"), [])
        self.assertEqual(policy.authority_path(self.path).read_bytes(), before)
        plan = log["codex_worker"]["validation_plan"]
        self.assertEqual(plan["revision"], self.f.revision)
        self.assertIn("AGENTS.md", plan["documents"])
        self.assertIn(".github/workflows/android-ci.yml", plan["documents"])

    def test_read_only_analysis_budget_seals_once_to_exact_automatic_authority(self):
        import codex_impact as analyzer
        self.analysis()
        state, log = policy.pair(self.path)
        log.pop("codex_analysis")
        storage.save_pair(self.path, state, log)
        def analyze(args, prompt, cwd, on_session, **kwargs):
            on_session(fixture.SESSION)
            events = [{"type": "thread.started", "thread_id": fixture.SESSION},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(self.f.result)}},
                      {"type": "turn.completed"}]
            return 0, b"\n".join(analyzer.canonical(value) for value in events), None
        with patch.object(analyzer, "preflight"), patch.object(analyzer, "invoke", side_effect=analyze):
            self.assertEqual(analyzer.run(self.path, self.inputs, "codex"), "VALID")
        before = storage.load_json(storage.companion(self.path, "execution"))
        self.assertIsNone(before["authority_sha256"])
        policy.decide(self.path, self.inputs)
        self.assertEqual(self.run_worker(self.implement), "IMPLEMENTED")
        after = storage.load_json(storage.companion(self.path, "execution"))
        self.assertEqual(after["authority_sha256"], policy.digest(storage.load_json(policy.authority_path(self.path))))
        self.assertGreaterEqual(after["active_seconds"], before["active_seconds"])
        self.assertEqual(after["attempts"], 1)

    def test_exhausted_worker_budget_stops_without_creating_worktree(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        storage.atomic_json(storage.companion(self.path, "execution"), {
            "active_seconds": 1800, "attempts": 0, "write_steps": 0})
        with patch.object(worker, "invoke") as invoke, patch.object(worker, "create_worktree") as create:
            with self.assertRaisesRegex(ValueError, "EXECUTION_BUDGET_EXHAUSTED"):
                worker.run(self.path, self.f.source, self.f.worktree, "ax/sstc-sync/recovery", self.inputs,
                           validation_mode="github")
        invoke.assert_not_called()
        create.assert_not_called()
        self.assertEqual(policy.pair(self.path)[0]["status"], "IMPLEMENTATION_FAILED")

    def test_slack_usage_limit_preserves_authority_and_resumes_dirty_same_worktree(self):
        def limited(executable, session, prompt, cwd, **kwargs):
            self.implement(executable, session, prompt, cwd, **kwargs)
            return 1, (json.dumps({"type": "thread.started", "thread_id": session}) +
                       '\n{"type":"error","message":"usage_limit_reached"}\n').encode(), None
        self.assertEqual(self.run_worker(limited), "RATE_LIMIT")
        proof = policy.authority_path(self.path).read_bytes()
        self.assertEqual(policy.pair(self.path)[0]["status"], "DEFERRED_RATE_LIMIT")
        with self.assertRaisesRegex(ValueError, "OBSERVED_RESET_REQUIRED"):
            self.run_worker(self.implement, resume=True)
        self.f.run_script("update_task_state.py", "--task-file", str(self.path),
                          "--record-reset-at", "2000-01-01T00:00:00Z")
        self.assertEqual(self.run_worker(self.implement, resume=True), "IMPLEMENTED")
        self.assertEqual(policy.authority_path(self.path).read_bytes(), proof)
        self.assertEqual(len(policy.pair(self.path)[1]["approvals"]), 1)
        self.assertEqual(storage.load_json(storage.companion(self.path, "execution"))["attempts"], 2)

    def test_legacy_failed_worker_can_resume_without_erasing_logs_or_new_approval(self):
        def failed(executable, session, prompt, cwd, **kwargs):
            self.implement(executable, session, prompt, cwd, **kwargs)
            return 1, b'{"type":"turn.failed"}\n', None
        self.assertEqual(self.run_worker(failed), "CODEX_FAILED")
        policy.authority_path(self.path).unlink()
        storage.companion(self.path, "execution").unlink()
        state, log = policy.pair(self.path)
        log["codex_worker"]["outcome"] = "WORKER_FAILED"
        log["codex_worker"].pop("resume_files")
        storage.save_pair(self.path, state, log)
        commands_before = copy.deepcopy(log["commands"])
        with self.assertRaisesRegex(ValueError, "LEGACY_BUDGET_OBSERVATION_REQUIRED"):
            self.run_worker(self.implement, resume=True)
        self.assertEqual(self.run_worker(self.implement, resume=True, legacy_active_seconds=30,
                                         legacy_write_steps=2), "IMPLEMENTED")
        after = policy.pair(self.path)[1]
        self.assertEqual(after["commands"][:-1], commands_before)
        self.assertEqual(len(after["approvals"]), 1)

    def test_result_input_session_and_policy_change_invalidate_permission(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        result_path = self.path.parent / "result.json"
        original = result_path.read_bytes()
        changed = json.loads(original)
        changed["summary"] = "Different scope"
        storage.atomic_json(result_path, changed)
        with self.assertRaisesRegex(ValueError, "RESULT_CHANGED"):
            policy.execution_context(self.path, self.inputs)
        result_path.write_bytes(original)
        state, log = policy.pair(self.path)
        log["codex_analysis"]["session_id"] = "0199a213-81c0-7800-8aa1-bbab2a035a54"
        storage.save_pair(self.path, state, log)
        with self.assertRaisesRegex(ValueError, "EXECUTION_AUTHORITY_CHANGED"):
            policy.execution_context(self.path, self.inputs)

    def test_retry_time_and_write_budgets_are_enforced_separately_from_file_count(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        output = storage.companion(self.path, "execution")
        storage.atomic_json(output, {"active_seconds": 0, "attempts": 3, "write_steps": 0})
        with self.assertRaisesRegex(ValueError, "EXECUTION_ATTEMPT_LIMIT"):
            policy.Budget(self.path, "IMPLEMENTING", attempt=True)
        storage.atomic_json(output, {"active_seconds": 1800, "attempts": 0, "write_steps": 0})
        with self.assertRaisesRegex(ValueError, "EXECUTION_BUDGET_EXHAUSTED"):
            policy.Budget(self.path, "IMPLEMENTING")
        storage.atomic_json(output, {"active_seconds": 0, "attempts": 0, "write_steps": 20})
        budget = policy.Budget(self.path, "GITHUB")
        with self.assertRaisesRegex(ValueError, "EXECUTION_BUDGET_EXHAUSTED"):
            budget.tick(writes=1)
        budget.finish("EXHAUSTED")
        self.assertEqual(storage.load_json(output)["write_steps"], 21)

    def test_partial_identity_source_and_policy_decision_cannot_forge_permission(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        output = policy.authority_path(self.path)
        original = storage.load_json(output)
        mutations = (lambda proof: proof.update(identity={}),
                     lambda proof: proof.update(source_revision="f" * 40),
                     lambda proof: proof["original_pair"]["log"]["policy_decision"].pop("result_sha256"))
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(original)
                mutate(changed)
                storage.atomic_json(output, changed)
                with self.assertRaises(ValueError):
                    policy.execution_context(self.path, self.inputs)
        storage.atomic_json(output, original)

    def test_interrupted_policy_authority_write_is_recovered_from_exact_decision_journal(self):
        self.analysis()
        original = policy.make_authority
        def failed(*args, **kwargs):
            if kwargs.get("persist", True):
                raise OSError("simulated write failure")
            return original(*args, **kwargs)
        with patch.object(policy, "make_authority", side_effect=failed):
            with self.assertRaises(OSError):
                policy.decide(self.path, self.inputs)
        self.assertEqual(policy.pair(self.path)[0]["status"], "ANALYZING")
        self.assertTrue(storage.companion(self.path, "execution-decision").exists())
        self.assertEqual(policy.decide(self.path, self.inputs)["decision"], "IMPLEMENTING")
        self.assertFalse(storage.companion(self.path, "execution-decision").exists())
        self.assertEqual(policy.execution_context(self.path, self.inputs)["proof"]["authorization"], "POLICY")

    def test_corrupt_budget_counters_and_last_api_time_exhaustion_fail_closed(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        output = storage.companion(self.path, "execution")
        for field, value in (("active_seconds", -1), ("attempts", False), ("write_steps", -1),
                             ("active_seconds", float("inf"))):
            with self.subTest(field=field, value=value):
                record = {"active_seconds": 0, "attempts": 0, "write_steps": 0}
                record[field] = value
                storage.atomic_json(output, record)
                with self.assertRaises(ValueError):
                    policy.Budget(self.path, "GITHUB")
        storage.atomic_json(output, {"active_seconds": 1799, "attempts": 0, "write_steps": 0})
        class Client:
            def api(self, *args, **kwargs):
                return {"success": True}
        with patch.object(policy.time, "monotonic", side_effect=(10, 10, 12)):
            with self.assertRaisesRegex(ValueError, "EXECUTION_BUDGET_EXHAUSTED"):
                policy.BudgetClient(Client(), self.path).api("read-only-final-check")

    def test_waiting_time_does_not_consume_active_budget(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        with patch.object(policy.time, "monotonic", side_effect=(10, 15, 4000, 4005)):
            policy.Budget(self.path, "FIRST").finish("DONE")
            policy.Budget(self.path, "SECOND").finish("DONE")
        self.assertEqual(storage.load_json(storage.companion(self.path, "execution"))["active_seconds"], 10)

    def test_real_github_subprocess_uses_remaining_budget_and_legacy_fake_api_still_works(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        output = storage.companion(self.path, "execution")
        for used, expected_timeout in ((100, 30), (1798.75, 1.25)):
            with self.subTest(used=used):
                storage.atomic_json(output, {"active_seconds": used, "attempts": 0, "write_steps": 0})
                with patch.object(policy.time, "monotonic", return_value=10), \
                        patch("github_validation.subprocess.run") as run:
                    run.return_value.returncode = 0
                    run.return_value.stdout = b'{"success":true}'
                    result = policy.BudgetClient(policy.GitHub(), self.path).api("read-only-check")
                    self.assertTrue(result["success"])
                    self.assertEqual(run.call_args.kwargs["timeout"], expected_timeout)
        class LegacyClient:
            def api(self, endpoint, payload=None, *, binary=False):
                return {"endpoint": endpoint}
        with patch.object(policy.time, "monotonic", return_value=10):
            self.assertEqual(policy.BudgetClient(LegacyClient(), self.path).api("legacy-check"),
                             {"endpoint": "legacy-check"})

    def test_real_github_deadline_exhaustion_persists_terminal_checkpoint(self):
        self.analysis()
        policy.decide(self.path, self.inputs)
        storage.atomic_json(storage.companion(self.path, "execution"),
                            {"active_seconds": 1799, "attempts": 0, "write_steps": 0})
        with patch.object(policy.time, "monotonic", side_effect=(10, 10, 12)), \
                patch("github_validation.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 1)) as run:
            with self.assertRaisesRegex(ValueError, "EXECUTION_BUDGET_EXHAUSTED"):
                policy.BudgetClient(policy.GitHub(), self.path).api("read-only-check")
        self.assertEqual(run.call_args.kwargs["timeout"], 1)
        state, log = policy.pair(self.path)
        self.assertEqual(state["status"], "IMPLEMENTATION_FAILED")
        self.assertEqual(log["stop_reason"], "EXECUTION_BUDGET_EXHAUSTED")
        self.assertEqual(storage.load_json(storage.checkpoint_path(self.path))["state"], state)

    def test_changed_partial_diff_and_running_worker_cannot_resume(self):
        def failed(executable, session, prompt, cwd, **kwargs):
            self.implement(executable, session, prompt, cwd, **kwargs)
            return 1, b'{"type":"turn.failed"}\n', None
        self.assertEqual(self.run_worker(failed), "CODEX_FAILED")
        (self.f.worktree / "app/src/main/java/example/Client.kt").write_text("changed later\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "RESUME_DIFF_CHANGED"):
            self.run_worker(self.implement, resume=True)
        state, log = policy.pair(self.path)
        log["codex_worker"]["outcome"] = "RUNNING"
        storage.save_pair(self.path, state, log)
        with self.assertRaisesRegex(ValueError, "REQUIRES_RECONCILIATION"):
            self.run_worker(self.implement, resume=True)

    def test_changed_jdk_or_extra_ci_lint_cannot_use_fixed_remote_receipt(self):
        ci = worker.git(self.f.source, ["show", self.f.revision + ":.github/workflows/android-ci.yml"])
        original = worker.git
        for changed in (ci.replace("'17'", "'21'"), ci + "\n      - run: ./gradlew lintDebug\n"):
            def read(path, arguments, **kwargs):
                return changed if arguments[-1].endswith(":.github/workflows/android-ci.yml") else original(path, arguments, **kwargs)
            with patch.object(worker, "git", side_effect=read):
                with self.assertRaisesRegex(ValueError, "TARGET_VALIDATION_PLAN_UNSUPPORTED"):
                    worker.validation_plan(self.f.source, self.f.revision)

    def test_shell_write_events_count_without_file_change_events(self):
        raw = (json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "python write.py"}}) +
               '\n{"type":"turn.completed"}\n')
        command = [sys.executable, "-c", "import sys;sys.stdin.buffer.read();sys.stdout.write(" + repr(raw) + ")"]
        counts = []
        with patch.object(worker, "codex_command", return_value=command):
            code, events, fault = worker.invoke("codex", fixture.SESSION, b"prompt", self.f.source,
                                                on_write=counts.append)
        self.assertEqual(code, 0)
        self.assertIsNone(fault)
        self.assertEqual(counts, [1])

    def test_target_validation_guidance_is_required_not_silently_invented(self):
        plan = worker.validation_plan(self.f.source, self.f.revision)
        self.assertEqual(plan["commands"], ["chmod +x gradlew", "./gradlew testDebugUnitTest assembleDebug"])
        with patch.object(worker, "git", return_value="unsupported build"):
            with self.assertRaisesRegex(ValueError, "TARGET_VALIDATION_PLAN_UNSUPPORTED"):
                worker.validation_plan(self.f.source, self.f.revision)

    def test_auto_implementation_through_remote_validation_produces_draft_without_slack(self):
        from types import SimpleNamespace
        import sstc_candidate as candidate
        import sstc_pipeline as pipeline
        from test_sstc_pipeline import FakeGitHub
        self.analysis()
        policy.decide(self.path, self.inputs)
        self.assertEqual(self.run_worker(self.implement), "IMPLEMENTED")
        files = candidate.candidate_snapshot(self.f.worktree, self.f.revision, "ax/sstc-sync/recovery")
        tree, _ = candidate.tree_payload(self.f.worktree, self.f.revision, "ax/sstc-sync/recovery", files)
        owner = SimpleNamespace(path=self.path, revision=self.f.revision, tree=tree,
                                branch="ax/sstc-sync/recovery", base_tree=candidate.git(
                                    self.f.worktree, ["rev-parse", self.f.revision + "^{tree}"]).decode().strip())
        client = FakeGitHub(owner)
        self.assertEqual(pipeline.publish(self.path, self.inputs, client)["outcome"], "PENDING")
        client.run_status = "completed"
        self.assertEqual(pipeline.check(self.path, client)["outcome"], "READY_FOR_REVIEW")
        self.assertTrue(client.pull["draft"])
        self.assertEqual(policy.pair(self.path)[1]["approvals"], [])

    def test_feature_server_contract_and_unsupported_profile_stop_before_worker(self):
        self.analysis()
        self.f.result["impacts"]["protocol_contract"]["status"] = "PRESENT"
        self.f.result["approval_reasons"] = [APPROVALS["protocol_contract"]]
        self.f.analysis()
        self.assertEqual(policy.decide(self.path, self.inputs)["decision"], "PROTOCOL_APPROVAL_REQUIRED")
        self.analysis()
        self.f.result["impacts"]["dependency"]["status"] = "PRESENT"
        self.f.result["approval_reasons"] = [APPROVALS["dependency"]]
        self.f.analysis()
        result = policy.decide(self.path, self.inputs)
        self.assertEqual(result["decision"], "ANALYSIS_FAILED")
        self.assertEqual(result["reason"], "UNSUPPORTED_VALIDATION_SCOPE")
        self.assertFalse(self.f.worktree.exists())

    def test_sstd_published_contract_adaptation_is_explicitly_allowed_after_slack(self):
        data = storage.load_json(fixture.ROOT / "tests/fixtures/impact/sstd-required.json")
        self.path = self.f.task = self.path.parent / (data["task"]["task_id"] + ".json")
        self.f.run_script("create_task.py", "--source-type", "SSTD_CHANGE", "--source-reference",
                          data["task"]["source_reference"], "--risk-level", "HIGH", "--task-id", self.path.stem,
                          "--task-directory", str(self.path.parent))
        self.f.move("ANALYZING")
        self.f.manifest, self.f.result = data["manifest"], data["result"]
        self.f.manifest["input_context"]["sstc_revision"] = self.f.revision
        for evidence in self.f.manifest["evidence"]:
            raw = ("Synthetic " + evidence["evidence_id"]).encode()
            evidence["content_sha256"] = __import__("hashlib").sha256(raw).hexdigest()
            (self.inputs / "evidence" / (evidence["evidence_id"] + ".txt")).write_bytes(raw)
            if evidence["source"] == "SSTC":
                evidence["revision"] = self.f.revision
        self.f.result["input_context"] = copy.deepcopy(self.f.manifest["input_context"])
        for impact in self.f.result["impacts"].values():
            impact["status"] = "ABSENT"
        self.f.result["impacts"]["protocol_contract"]["status"] = "PRESENT"
        self.f.result["approval_reasons"] = [APPROVALS["protocol_contract"]]
        storage.atomic_json(self.inputs / "manifest.json", self.f.manifest)
        self.f.analysis()
        self.f.approve()
        def implement(executable, session, prompt, cwd, **kwargs):
            self.assertIn(b"already-published SSTD contract", prompt)
            self.assertIn(b"DTO, parser, field names, units and numeric interpretation", prompt)
            self.assertIn(b"Do not modify SSTD", prompt)
            return self.implement(executable, session, prompt, cwd, **kwargs)
        self.assertEqual(self.run_worker(implement), "IMPLEMENTED")


if __name__ == "__main__":
    unittest.main()
