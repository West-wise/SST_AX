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
        subprocess.run(["git", "-C", str(self.source), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-qm", "base"], check=True)
        self.revision = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
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
        def fake_invoke(executable, session, prompt, cwd):
            self.assertIn(SESSION, worker.codex_command(executable, session))
            self.assertIn(b"Do not push", prompt)
            self.assertEqual(cwd, self.worktree)
            return 0, b'{"type":"turn.completed"}\n', None
        with patch.object(worker, "git", side_effect=fake_git), patch.object(worker, "create_worktree") as add, \
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
             patch.object(worker, "create_worktree"), patch.object(worker, "invoke", return_value=(0, b'{"type":"turn.completed"}\n', None)), \
             patch.object(worker, "validation", return_value=(0, "./gradlew testDebugUnitTest assembleDebug")):
            self.assertEqual(worker.run(self.task, self.source, self.worktree, "ax/sstc-sync/demo",
                                         self.inputs), "VALIDATED")
        self.assertEqual((self.source / "local-notes").read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
