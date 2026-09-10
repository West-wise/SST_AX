"""Policy refusal and interrupted-save recovery regression tests."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import task_storage as storage


class RecoveryTest(unittest.TestCase):
    def run_script(self, script, *arguments):
        return subprocess.run(
            [sys.executable, str(Path(storage.__file__).parent / script), *arguments],
            capture_output=True, text=True, check=False,
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tasks" / "sstc-feature-20260910-0001.json"
        result = self.run_script(
            "create_task.py", "--source-type", "SSTC_FEATURE", "--source-reference", "issue-1",
            "--task-id", self.path.stem, "--task-directory", str(self.path.parent),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.move("ANALYZING")

    def move(self, status, *extra, success=True):
        result = self.run_script("update_task_state.py", "--task-file", str(self.path),
                                 "--status", status, *extra)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_risk_and_approval_gates_do_not_write(self):
        original = storage.load_json(self.path)
        for risk, reason in (("HIGH", None), ("CRITICAL", None), ("LOW", "UI_CHANGE")):
            state = dict(original, risk_level=risk, approval_reason=reason)
            storage.atomic_json(self.path, state)
            before = self.path.read_bytes()
            self.move("IMPLEMENTING", success=False)
            self.assertEqual(self.path.read_bytes(), before)

    def test_wait_checkpoint_and_approval_bypass_rejected(self):
        self.move("WAITING_APPROVAL", "--reason", "UI_CHANGE")
        checkpoint = storage.load_json(storage.checkpoint_path(self.path))
        self.assertEqual(checkpoint["resume_status"], "ANALYZING")
        self.move("IMPLEMENTING", success=False)
        self.move("DEFERRED_RATE_LIMIT", "--reason", "limit", "--deferred-until", "2000-01-01T00:00:00Z")
        self.move("IMPLEMENTING", success=False)
        self.move("WAITING_APPROVAL", "--reason", "UI_CHANGE")
        self.move("IMPLEMENTING", success=False)

    def test_resume_requires_checkpoint_stage_and_elapsed_reset(self):
        self.move("DEFERRED_RATE_LIMIT", "--reason", "limit", "--deferred-until", "2999-01-01T00:00:00Z")
        self.move("ANALYZING", success=False)
        self.move("IMPLEMENTING", success=False)

    def test_elapsed_reset_resumes_and_missing_checkpoint_blocks(self):
        self.move("DEFERRED_RATE_LIMIT", "--reason", "limit", "--deferred-until", "2000-01-01T00:00:00Z")
        checkpoint = storage.checkpoint_path(self.path)
        contents = checkpoint.read_bytes()
        checkpoint.unlink()
        self.move("ANALYZING", success=False)
        checkpoint.write_bytes(contents)
        self.move("ANALYZING")

    def test_ready_requires_trusted_sensor_results(self):
        self.move("IMPLEMENTING")
        self.move("VALIDATING")
        self.move("READY_FOR_REVIEW", "--reason", "tests passed", success=False)
        self.move("TEST_FAILED", "--reason", "test failure")

    def test_interrupted_pair_save_recovers_once(self):
        state = storage.load_json(self.path)
        log = storage.load_json(storage.companion(self.path, "log"))
        state["status"] = log["status"] = "IMPLEMENTING"
        log["state_transitions"].append({"from_status": "ANALYZING", "to_status": "IMPLEMENTING"})
        real_write = storage.atomic_json

        def interrupt(path, value):
            if path == storage.companion(self.path, "log"):
                raise OSError("injected interruption")
            real_write(path, value)

        with patch.object(storage, "atomic_json", side_effect=interrupt):
            with self.assertRaises(OSError):
                storage.save_pair(self.path, state, log)
        self.move("VALIDATING", success=False)
        for _ in range(2):
            result = self.run_script("update_task_state.py", "--task-file", str(self.path), "--recover")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(storage.load_json(storage.companion(self.path, "log")), log)
        self.move("VALIDATING")

    def test_os_lock_refuses_second_writer(self):
        with storage.task_lock(self.path):
            self.move("IMPLEMENTING", success=False)
        self.move("IMPLEMENTING")

    def test_foreign_log_rejected(self):
        log_path = storage.companion(self.path, "log")
        log = storage.load_json(log_path)
        log["task_id"] = "different-task"
        storage.atomic_json(log_path, log)
        self.move("IMPLEMENTING", success=False)

    def test_stale_checkpoint_cannot_resume(self):
        self.move("DEFERRED_RATE_LIMIT", "--reason", "limit", "--deferred-until", "2000-01-01T00:00:00Z")
        state = storage.load_json(self.path)
        state["source_reference"] = "changed-request"
        storage.atomic_json(self.path, state)
        log_path = storage.companion(self.path, "log")
        log = storage.load_json(log_path)
        log["source_reference"] = "changed-request"
        storage.atomic_json(log_path, log)
        self.move("ANALYZING", success=False)

    def test_corrupt_journal_does_not_overwrite_task(self):
        before = self.path.read_bytes()
        storage.atomic_json(storage.companion(self.path, "pending"), {"state": {}, "log": {}})
        result = self.run_script("update_task_state.py", "--task-file", str(self.path), "--recover")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)
