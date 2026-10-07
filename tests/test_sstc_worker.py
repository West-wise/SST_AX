"""Offline tests for approval-bound SSTC worktree orchestration."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import slack_approval as approval
import sstc_worker as worker
import task_storage as storage

ROOT = Path(__file__).resolve().parents[1]
SESSION = "0199a213-81c0-7800-8aa1-bbab2a035a53"
PLAN = {"jdk": "17", "commands": ["chmod +x gradlew", "./gradlew testDebugUnitTest assembleDebug"],
        "documents": {"AGENTS.md": {"sha256": "a" * 64, "text": "Use the CI validation."}}}


class SstcWorkerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture = storage.load_json(ROOT / "tests/fixtures/impact/sstc-required.json")
        self.task = self.root / "tasks" / (fixture["task"]["task_id"] + ".json")
        self.run_script("create_task.py", "--source-type", "SSTC_FEATURE",
                        "--source-reference", fixture["task"]["source_reference"],
                        "--risk-level", "HIGH", "--task-id", self.task.stem,
                        "--task-directory", str(self.task.parent))
        self.move("ANALYZING")
        self.inputs = self.root / "inputs"
        (self.inputs / "evidence").mkdir(parents=True)
        self.manifest, self.result = fixture["manifest"], fixture["result"]
        for item in self.manifest["evidence"]:
            raw = ("Synthetic " + item["evidence_id"]).encode()
            item["content_sha256"] = __import__("hashlib").sha256(raw).hexdigest()
            (self.inputs / "evidence" / (item["evidence_id"] + ".txt")).write_bytes(raw)
            if item["source"] == "REQUEST":
                item["request_sha256"] = item["content_sha256"]
                self.manifest["input_context"]["request_sha256"] = item["content_sha256"]
        self.result["input_context"] = copy.deepcopy(self.manifest["input_context"])
        storage.atomic_json(self.inputs / "manifest.json", self.manifest)
        self.source = self.root / "SSTC"
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        (self.source / "README").write_text("base\n", encoding="utf-8")
        (self.source / "AGENTS.md").write_text("Use the CI validation.\n", encoding="utf-8")
        (self.source / ".github/workflows").mkdir(parents=True)
        (self.source / ".github/workflows/android-ci.yml").write_text(
            "jobs:\n  build:\n    steps:\n      - uses: actions/setup-java@v4\n        with:\n          java-version: '17'\n      - run: chmod +x gradlew\n      - run: ./gradlew testDebugUnitTest assembleDebug\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
        self.revision = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        self.manifest["input_context"]["sstc_revision"] = self.revision
        for item in self.manifest["evidence"]:
            if item["source"] == "SSTC":
                item["revision"] = self.revision
        self.result["input_context"] = copy.deepcopy(self.manifest["input_context"])
        storage.atomic_json(self.inputs / "manifest.json", self.manifest)
        self.analysis()
        log = storage.load_json(storage.companion(self.task, "log"))
        log["codex_analysis"]["sstc_revision"] = self.revision
        storage.save_pair(self.task, storage.load_json(self.task), log)
        self.approve()
        self.worktree = self.root / "worktree"

    def run_script(self, name, *args):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / name), *args],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def move(self, status, *args):
        return self.run_script("update_task_state.py", "--task-file", str(self.task),
                               "--status", status, *args)

    def analysis(self):
        manifest = storage.load_json(self.inputs / "manifest.json")
        log = storage.load_json(storage.companion(self.task, "log"))
        log["codex_analysis"] = {"session_id": SESSION, "outcome": "VALID",
                                  "manifest_sha256": __import__("hashlib").sha256(worker.canonical(manifest)).hexdigest(),
                                  "result_file": "result.json", "result_sha256": __import__("hashlib").sha256(worker.canonical(self.result)).hexdigest()}
        storage.atomic_json(self.task.parent / "result.json", self.result)
        state = storage.load_json(self.task)
        # Build the exact analysis checkpoint expected by the shared approval validator.
        storage.atomic_json(storage.checkpoint_path(self.task), {"state": state, "log": log, "resume_status": "ANALYZING"})
        storage.save_pair(self.task, state, log)

    def approve(self):
        self.move("WAITING_APPROVAL", "--reason", "human review")
        request = approval.prepare_request(self.task, "T1", "C1", "A1")
        approval.bind_message(self.task, request["nonce"], "123.456")
        payload = {"type": "block_actions", "team": {"id": "T1"}, "api_app_id": "A1",
                   "user": {"id": "U1"}, "channel": {"id": "C1"},
                   "container": {"message_ts": "123.456"},
                   "actions": [{"action_id": "ax_approve", "value": request["nonce"]}]}
        self.assertEqual(approval.apply_decision(self.task, payload, team_id="T1", channel_id="C1",
                                                 app_id="A1", approver_ids={"U1"}), "IMPLEMENTING")

    def test_approved_worker_creates_isolated_branch_and_runs_validation(self):
        calls = []
        def fake_git(source, arguments, **kwargs):
            calls.append(arguments)
            if arguments[:2] == ["rev-parse", "HEAD"]:
                return self.revision
            return ""
        def fake_invoke(executable, session, prompt, cwd, **kwargs):
            self.assertIn(SESSION, worker.codex_command(executable, session))
            self.assertIn(b"Do not push", prompt)
            self.assertEqual(cwd, self.worktree)
            return 0, self.github_events(), None
        with patch.object(worker, "git", side_effect=fake_git), patch.object(worker, "create_worktree") as add, \
             patch.object(worker, "validation_plan", return_value=PLAN), \
             patch("sstc_candidate.candidate_snapshot", return_value=[]), \
             patch.object(worker, "invoke", side_effect=fake_invoke), patch.object(worker, "validation", return_value=(0, "./gradlew testDebugUnitTest assembleDebug")):
            self.assertEqual(worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                         self.inputs, "codex"), "VALIDATED")
        add.assert_called_once_with(self.source, self.worktree, "ax/sstc-sync/demo", self.revision)
        state, log = storage.load_json(self.task), storage.load_json(storage.companion(self.task, "log"))
        self.assertEqual(state["status"], "IMPLEMENTING")
        self.assertEqual(log["codex_worker"]["validation"]["exit_code"], 0)

    def test_approval_and_task_status_are_required(self):
        self.move("VALIDATING")
        with self.assertRaises(ValueError):
            worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo", self.inputs)

    def pair(self):
        return storage.load_json(self.task), storage.load_json(storage.companion(self.task, "log"))

    def test_main_branch_and_existing_worktree_are_rejected(self):
        with self.assertRaises(ValueError):
            worker.create_worktree(self.source, self.worktree, "main", self.revision)
        self.worktree.mkdir()
        with self.assertRaises(ValueError):
            worker.create_worktree(self.source, self.worktree, "ax/sstc-sync/demo", self.revision)

    def test_dirty_source_is_preserved(self):
        (self.source / "local-notes").write_text("keep", encoding="utf-8")
        with patch.object(worker, "git", side_effect=lambda source, args, **kwargs: self.revision if args[:2] == ["rev-parse", "HEAD"] else ""), \
             patch.object(worker, "validation_plan", return_value=PLAN), \
             patch("sstc_candidate.candidate_snapshot", return_value=[]), \
             patch.object(worker, "create_worktree"), patch.object(worker, "invoke", return_value=(0, self.github_events(), None)), \
             patch.object(worker, "validation", return_value=(0, "./gradlew testDebugUnitTest assembleDebug")):
            self.assertEqual(worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                         self.inputs), "VALIDATED")
        self.assertEqual((self.source / "local-notes").read_text(encoding="utf-8"), "keep")

    def pin_inputs_and_approve(self):
        self.manifest["input_context"]["sstc_revision"] = self.revision
        for item in self.manifest["evidence"]:
            if item["source"] == "SSTC":
                item["revision"] = self.revision
        self.result["input_context"] = copy.deepcopy(self.manifest["input_context"])
        storage.atomic_json(self.inputs / "manifest.json", self.manifest)
        self.analysis()
        self.approve()

    def test_github_worker_records_candidate_without_local_gradle(self):
        self.pin_inputs_and_approve()
        files = [{"path": "app/src/main/Test.kt", "mode": "100644", "sha256": "a" * 64}]
        def fake_invoke(executable, session, prompt, cwd, **kwargs):
            self.assertEqual(session, SESSION)
            self.assertEqual(cwd, self.worktree)
            self.assertIn(b"do not run local Android build, test, or lint", prompt)
            self.assertIn(b"GitHub Actions", prompt)
            self.assertIn(b"Do not push or create a PR", prompt)
            self.assertIn(b"target_guidance", prompt)
            return 0, self.github_events(), None
        with patch.object(worker, "git", return_value=""), patch.object(worker, "create_worktree"), \
             patch.object(worker, "validation_plan", return_value=PLAN), \
             patch.object(worker, "invoke", side_effect=fake_invoke), patch.object(worker, "validation") as build, \
             patch("sstc_candidate.candidate_snapshot", return_value=files) as snapshot:
            self.assertEqual(worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                         self.inputs, validation_mode="github"), "IMPLEMENTED")
        build.assert_not_called()
        snapshot.assert_called_once_with(self.worktree, self.revision, "ax/sstc-sync/demo")
        state, log = self.pair()
        record = log["codex_worker"]
        self.assertEqual(state["status"], "IMPLEMENTING")
        self.assertEqual(record["validation_mode"], "github")
        self.assertIsNone(record["validation"])
        self.assertEqual(record["candidate_files"], files)
        self.assertEqual(log["commands"][-1]["exit_code"], 0)
        self.assertIsNotNone(log["commands"][-1]["finished_at"])

    def test_github_changed_approval_and_session_are_rejected_before_execution(self):
        self.pin_inputs_and_approve()
        state, original = self.pair()
        for field, value in (("decision", "REJECTED"), ("nonce", "unapproved")):
            with self.subTest(field=field):
                log = copy.deepcopy(original)
                log["approvals"][-1][field] = value
                storage.save_pair(self.task, state, log)
                with patch.object(worker, "create_worktree") as add, patch.object(worker, "invoke") as invoke:
                    with self.assertRaises(ValueError):
                        worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                   self.inputs, validation_mode="github")
                add.assert_not_called()
                invoke.assert_not_called()
        log = copy.deepcopy(original)
        log["codex_analysis"]["session_id"] = "0199a213-81c0-7800-8aa1-bbab2a035a54"
        storage.save_pair(self.task, state, log)
        with patch.object(worker, "create_worktree") as add:
            with self.assertRaises(ValueError):
                worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                           self.inputs, validation_mode="github")
        add.assert_not_called()

    def test_github_conflicting_analysis_revision_is_rejected(self):
        log = storage.load_json(storage.companion(self.task, "log"))
        log["codex_analysis"]["sstc_revision"] = "b" * 40
        storage.save_pair(self.task, storage.load_json(self.task), log)
        self.approve()
        with patch.object(worker, "create_worktree") as add, patch.object(worker, "git") as git:
            with self.assertRaisesRegex(ValueError, "PINNED_REVISION_REQUIRED"):
                worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                           self.inputs, validation_mode="github")
        add.assert_not_called()
        git.assert_not_called()

    def test_github_changed_manifest_is_rejected(self):
        self.pin_inputs_and_approve()
        self.manifest["input_context"]["sstc_revision"] = "b" * 40
        storage.atomic_json(self.inputs / "manifest.json", self.manifest)
        with patch.object(worker, "create_worktree") as add:
            with self.assertRaises(ValueError):
                worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                           self.inputs, validation_mode="github")
        add.assert_not_called()

    def test_github_rejected_candidate_is_not_implemented(self):
        self.pin_inputs_and_approve()
        with patch.object(worker, "git", return_value=""), patch.object(worker, "create_worktree"), \
             patch.object(worker, "validation_plan", return_value=PLAN), \
             patch.object(worker, "invoke", return_value=(0, self.github_events(), None)), \
             patch.object(worker, "validation") as build, \
             patch("sstc_candidate.candidate_snapshot", side_effect=ValueError("HEAD_OR_BRANCH_CHANGED")):
            with self.assertRaisesRegex(ValueError, "HEAD_OR_BRANCH_CHANGED"):
                worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                           self.inputs, validation_mode="github")
        build.assert_not_called()
        state, log = self.pair()
        self.assertEqual(state["status"], "IMPLEMENTING")
        self.assertEqual(log["codex_worker"]["outcome"], "WORKER_FAILED")
        self.assertNotIn("candidate_files", log["codex_worker"])

    def test_github_failed_codex_skips_snapshot_and_build(self):
        self.pin_inputs_and_approve()
        with patch.object(worker, "git", return_value=""), patch.object(worker, "create_worktree"), \
             patch.object(worker, "validation_plan", return_value=PLAN), \
             patch.object(worker, "invoke", return_value=(1, b'{"type":"turn.failed"}\n', None)), \
             patch.object(worker, "validation") as build, patch("sstc_candidate.candidate_snapshot", return_value=[]) as snapshot:
            self.assertEqual(worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                         self.inputs, validation_mode="github"), "CODEX_FAILED")
        build.assert_not_called()
        snapshot.assert_called_once_with(self.worktree, self.revision, "ax/sstc-sync/demo")
        self.assertNotIn("candidate_files", self.pair()[1]["codex_worker"])

    def test_github_real_worktree_records_untracked_source_and_refuses_replay(self):
        self.pin_inputs_and_approve()
        content = "package example\nclass Test\n"
        def fake_invoke(executable, session, prompt, cwd, **kwargs):
            added = cwd / "app/src/main/java/Test.kt"
            added.parent.mkdir(parents=True)
            added.write_text(content, encoding="utf-8")
            return 0, self.github_events(), None
        with patch.object(worker, "invoke", side_effect=fake_invoke), patch.object(worker, "validation") as build:
            self.assertEqual(worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                         self.inputs, validation_mode="github"), "IMPLEMENTED")
        build.assert_not_called()
        state, log = self.pair()
        self.assertEqual(log["codex_worker"]["candidate_files"], [{
            "path": "app/src/main/java/Test.kt", "mode": "100644",
            "sha256": __import__("hashlib").sha256(
                (self.worktree / "app/src/main/java/Test.kt").read_bytes()).hexdigest()}])
        self.assertEqual(worker.git(self.source, ["rev-parse", "HEAD"]), self.revision)
        with patch.object(worker, "create_worktree") as add:
            with self.assertRaisesRegex(ValueError, "EXPLICIT_RESUME_REQUIRED"):
                worker.run(self.task, self.source, self.root / "another-worktree", "ax/sstc-sync/another",
                           self.inputs, validation_mode="github")
        add.assert_not_called()

    def github_events(self):
        return (json.dumps({"type": "thread.started", "thread_id": SESSION}).encode() +
                b'\n{"type":"turn.completed"}\n')

    def test_github_session_event_requires_exactly_one_approved_thread(self):
        completed = b'{"type":"turn.completed"}\n'
        wrong = json.dumps({"type": "thread.started", "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a54"}).encode()
        valid = json.dumps({"type": "thread.started", "thread_id": SESSION}).encode()
        for events in (completed, wrong + b"\n" + completed, valid + b"\n" + valid + b"\n" + completed):
            with self.subTest(events=events):
                self.assertEqual(worker.validate_events(events, SESSION), "SESSION_MISMATCH")
        self.assertIsNone(worker.validate_events(self.github_events(), SESSION))
        self.assertEqual(worker.validate_events(valid + b'\n{"type":"turn.failed"}\n', SESSION), "CODEX_FAILED")

    def test_github_missing_session_event_skips_candidate_and_validation(self):
        self.pin_inputs_and_approve()
        with patch.object(worker, "git", return_value=""), patch.object(worker, "create_worktree"), \
             patch.object(worker, "validation_plan", return_value=PLAN), \
             patch.object(worker, "invoke", return_value=(0, b'{"type":"turn.completed"}\n', None)), \
             patch.object(worker, "validation") as build, patch("sstc_candidate.candidate_snapshot") as snapshot:
            self.assertEqual(worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                         self.inputs, validation_mode="github"), "SESSION_MISMATCH")
        snapshot.assert_not_called()
        build.assert_not_called()
        self.assertEqual(self.pair()[1]["codex_worker"]["outcome"], "SESSION_MISMATCH")

    def test_unknown_validation_mode_is_rejected(self):
        with patch.object(worker, "create_worktree") as add:
            with self.assertRaisesRegex(ValueError, "VALIDATION_MODE_REFUSED"):
                worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                           self.inputs, validation_mode="remote")
        add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
