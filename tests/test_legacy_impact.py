"""Legacy Markdown evidence stays read-only and uses the bounded Git boundary."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_impact as legacy
from impact_collection import CollectionError

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
SYNTHETIC_TOKEN = "ghp_" + "A" * 32


class LegacyImpactTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture = json.loads((ROOT / "tests/fixtures/impact/sstd-required.json").read_text(encoding="utf-8"))
        self.path = self.root / (fixture["task"]["task_id"] + ".json")
        fixture["task"]["source_reference"] = "HEAD"
        self.path.write_text(json.dumps(fixture["task"]), encoding="utf-8")
        self.before = self.path.read_bytes()
        self.report = self.path.with_suffix(".impact.md")
        self.args = argparse.Namespace(task_file=self.path, source_repository=self.root / "source")

    def invoke(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(legacy, "parse_args", return_value=self.args), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = legacy.main()
        self.assertEqual(self.path.read_bytes(), self.before)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_resolves_once_and_uses_pinned_sha_with_hardened_git(self):
        with patch.object(legacy, "Git") as git:
            git.return_value.resolve.return_value = REVISION
            git.return_value.run.side_effect = [b"protocol.md\n", b"safe stat\n"]
            code, stdout, stderr = self.invoke()
        self.assertEqual((code, stderr), (0, ""))
        git.assert_called_once_with(self.args.source_repository)
        git.return_value.resolve.assert_called_once_with("HEAD")
        self.assertEqual(len(git.return_value.run.call_args_list), 2)
        for call in git.return_value.run.call_args_list:
            self.assertIn(REVISION, call.args[0])
            self.assertNotIn("HEAD", call.args[0])
        text = self.report.read_text(encoding="utf-8")
        self.assertIn(REVISION, text)
        self.assertIn("does not decide risk", text)
        self.assertIn("IMPACT_REPORT=", stdout)

    def test_secret_in_git_summary_is_rejected_before_truncation_or_persistence(self):
        with patch.object(legacy, "Git") as git:
            git.return_value.resolve.return_value = REVISION
            git.return_value.run.side_effect = [b"protocol.md\n",
                ("x" * (legacy.MAX_EVIDENCE_LENGTH + 1) + "\n" + SYNTHETIC_TOKEN).encode()]
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1)
        self.assertFalse(self.report.exists())
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "IMPACT_REPORT_ERROR=GIT_EVIDENCE\n")
        self.assertNotIn(SYNTHETIC_TOKEN, stdout + stderr)

    def test_git_timeout_or_environment_failure_does_not_echo_exception(self):
        for error in (CollectionError("GIT_TIMEOUT"), FileNotFoundError(SYNTHETIC_TOKEN)):
            with self.subTest(error=type(error).__name__), patch.object(legacy, "Git", side_effect=error):
                code, stdout, stderr = self.invoke()
            self.assertEqual((code, stdout, stderr), (1, "", "IMPACT_REPORT_ERROR=GIT_EVIDENCE\n"))
            self.assertFalse(self.report.exists())

    def test_real_git_commit_secret_is_not_written_to_report(self):
        source = self.args.source_repository
        source.mkdir()
        for arguments in (["init"], ["config", "user.name", "Synthetic"],
                ["config", "user.email", "synthetic@example.invalid"]):
            result = subprocess.run(["git", *arguments], cwd=source, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0)
        (source / "protocol.md").write_text("v1\n", encoding="utf-8")
        for arguments in (["add", "protocol.md"], ["commit", "-m", SYNTHETIC_TOKEN]):
            result = subprocess.run(["git", *arguments], cwd=source, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0)
        code, stdout, stderr = self.invoke()
        self.assertEqual((code, stdout, stderr), (1, "", "IMPACT_REPORT_ERROR=GIT_EVIDENCE\n"))
        self.assertFalse(self.report.exists())

    def test_invalid_cli_argument_is_not_echoed(self):
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["analyze_impact.py", "--task-file", str(self.path),
                "--unexpected", SYNTHETIC_TOKEN]), contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as stopped:
            legacy.parse_args()
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(stderr.getvalue(), "IMPACT_REPORT_ERROR=INVALID_ARGUMENTS; see --help\n")
