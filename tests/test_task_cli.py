"""Focused integration tests for the local SST-AX task intake CLIs."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"


class TaskCliTest(unittest.TestCase):
    """Verify task creation, validation, and read-only feature evidence flow."""

    def run_script(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run an SST-AX CLI from the repository root."""
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIRECTORY / script), *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_creates_valid_feature_task_and_impact_report(self) -> None:
        """A feature request produces valid state, log, and evidence report."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            task_directory = Path(temporary_directory)
            task_id = "sstc-feature-20260905-0001"
            created = self.run_script(
                "create_task.py",
                "--source-type",
                "SSTC_FEATURE",
                "--source-reference",
                "https://github.com/West-wise/Server_State_Telemetry_Client/issues/1",
                "--risk-level",
                "LOW",
                "--task-id",
                task_id,
                "--task-directory",
                str(task_directory),
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            task_path = task_directory / f"{task_id}.json"
            rejected = self.run_script(
                "update_task_state.py", "--task-file", str(task_path), "--status", "COMPLETED"
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("Transition is not allowed", rejected.stderr)

            analyzing = self.run_script(
                "update_task_state.py", "--task-file", str(task_path), "--status", "ANALYZING"
            )
            self.assertEqual(analyzing.returncode, 0, analyzing.stderr)
            validated = self.run_script("validate_task_state.py", str(task_path))
            self.assertEqual(validated.returncode, 0, validated.stderr)

            analyzed = self.run_script("analyze_impact.py", "--task-file", str(task_path))
            self.assertEqual(analyzed.returncode, 0, analyzed.stderr)
            self.assertTrue((task_directory / f"{task_id}.impact.md").exists())

            completed = self.run_script(
                "update_task_state.py",
                "--task-file",
                str(task_path),
                "--status",
                "COMPLETED",
                "--reason",
                "No SSTD protocol or SSTC implementation impact found.",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            task_state = json.loads(task_path.read_text(encoding="utf-8"))
            task_log_path = task_directory / f"{task_id}.log.json"
            task_log = json.loads(task_log_path.read_text(encoding="utf-8"))
            self.assertEqual(task_state["status"], "COMPLETED")
            self.assertEqual(
                task_log["stop_reason"],
                "No SSTD protocol or SSTC implementation impact found.",
            )
            self.assertEqual(len(task_log["state_transitions"]), 3)

    def test_rejects_invalid_branch(self) -> None:
        """A branch outside the reserved prefix cannot create task state."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            created = self.run_script(
                "create_task.py",
                "--source-type",
                "SSTC_FEATURE",
                "--source-reference",
                "issue-2",
                "--task-id",
                "sstc-feature-20260905-0002",
                "--branch",
                "invalid-branch",
                "--task-directory",
                temporary_directory,
            )
            self.assertEqual(created.returncode, 2)
            self.assertIn("does not match required pattern", created.stderr)

    def test_creation_rejects_recognizable_secrets_without_state_or_output(self) -> None:
        token = "xapp-1-A123456-T123456-abcdefghijklmnopqrstuvwxyz012345"
        for field, value in (("--source-reference", token), ("--approval-reason", token),
                             ("--branch", "ax/sstc-sync/" + token),
                             ("--checkpoint-path", "state/checkpoints/" + token + ".json")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary_directory:
                arguments = ["--source-type", "SSTC_FEATURE", "--source-reference", "manual:test",
                             "--task-id", "sstc-feature-20261007-0001", "--task-directory", temporary_directory]
                arguments += [field, value]
                created = self.run_script("create_task.py", *arguments)
                self.assertEqual(created.returncode, 2)
                self.assertEqual(created.stdout, "")
                self.assertNotIn(token, created.stderr)
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    def test_creation_usage_errors_do_not_echo_secrets(self) -> None:
        token = "xapp-1-A123456-T123456-abcdefghijklmnopqrstuvwxyz012345"
        result = self.run_script("create_task.py", "--risk-level", token)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_task_validation_errors_do_not_echo_untrusted_keys_or_uri_hosts(self) -> None:
        token = "xapp-1-A123456-T123456-abcdefghijklmnopqrstuvwxyz012345"
        fixture = json.loads((REPOSITORY_ROOT / "tests/fixtures/impact/sstc-required.json").read_text(encoding="utf-8"))
        for value in ({**fixture["task"], token: "synthetic"},
                      {**fixture["task"], "draft_pr_url": "https://[" + token + "]/"}):
            with tempfile.TemporaryDirectory() as temporary_directory:
                path = Path(temporary_directory) / "task.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                result = self.run_script("validate_task_state.py", str(path))
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(token, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_collects_sstd_commit_evidence_without_source_write(self) -> None:
        """An SSTD task records Git evidence from a local source repository."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            source_repository = temporary_path / "sstd"
            source_repository.mkdir()
            for arguments in (
                ("init",),
                ("config", "user.email", "ax@example.invalid"),
                ("config", "user.name", "SST-AX test"),
            ):
                result = subprocess.run(
                    ["git", *arguments],
                    cwd=source_repository,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            (source_repository / "protocol.md").write_text("v1\n", encoding="utf-8")
            committed = subprocess.run(
                ["git", "add", "protocol.md"],
                cwd=source_repository,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            committed = subprocess.run(
                ["git", "commit", "-m", "add protocol evidence"],
                cwd=source_repository,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)

            task_directory = temporary_path / "tasks"
            task_id = "sstd-sync-20260905-0001"
            created = self.run_script(
                "create_task.py",
                "--source-type",
                "SSTD_CHANGE",
                "--source-reference",
                "HEAD",
                "--task-id",
                task_id,
                "--task-directory",
                str(task_directory),
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            analyzed = self.run_script(
                "analyze_impact.py",
                "--task-file",
                str(task_directory / f"{task_id}.json"),
                "--source-repository",
                str(source_repository),
            )
            self.assertEqual(analyzed.returncode, 0, analyzed.stderr)
            report = (task_directory / f"{task_id}.impact.md").read_text(encoding="utf-8")
            self.assertIn("protocol.md", report)


if __name__ == "__main__":
    unittest.main()
