"""One-command Slack approval launcher tests."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import request_slack_approval as launcher


class RequestSlackApprovalTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "SLACK_BOT_TOKEN": "xoxb-fixture", "SLACK_APP_TOKEN": "xapp-fixture",
            "SLACK_TEAM_ID": "T123", "SLACK_CHANNEL_ID": "C123",
            "SLACK_APP_ID": "A123", "SLACK_APPROVER_IDS": "U123,U456",
        }
        self.analysis = {"state": {"status": "ANALYZING", "source_type": "SSTC_FEATURE"}, "log": {},
                         "result": {"change_required": "REQUIRED", "risk_level": "HIGH",
                                    "impacts": {"protocol_contract": {"status": "ABSENT"},
                                                "sstd_change_required": {"status": "ABSENT"}}}}

    def test_complete_environment_never_prompts(self):
        with patch("request_slack_approval.getpass.getpass") as secret, \
                patch("builtins.input") as plain:
            self.assertEqual(launcher.configuration(self.config, False), self.config)
        secret.assert_not_called()
        plain.assert_not_called()

    def test_interactive_prompts_only_for_missing_values(self):
        existing = {"SLACK_TEAM_ID": "T123", "SLACK_CHANNEL_ID": "C123"}
        responses = ["A123", "U123,U456"]
        with patch("request_slack_approval.getpass.getpass",
                   side_effect=["xoxb-fixture", "xapp-fixture"]) as secret, \
                patch("builtins.input", side_effect=responses) as plain:
            self.assertEqual(launcher.configuration(existing, True), self.config)
        self.assertEqual(secret.call_count, 2)
        self.assertEqual(plain.call_count, 2)

    def test_noninteractive_missing_values_fail_without_leaking_existing_token(self):
        output = io.StringIO()
        environ = {"SLACK_BOT_TOKEN": "xoxb-private-value"}
        with patch.dict(launcher.os.environ, environ, clear=True), \
                patch.object(launcher.sys.stdin, "isatty", return_value=False), \
                contextlib.redirect_stderr(output):
            self.assertEqual(launcher.main(["--task-file", "task.json",
                                            "--reason", "UI_CHANGE"]), 2)
        self.assertNotIn("xoxb-private-value", output.getvalue())

    def test_invalid_reason_fails_before_prompt_or_network(self):
        output = io.StringIO()
        with patch.dict(launcher.os.environ, self.config, clear=True), \
                patch("request_slack_approval.configuration") as configuration, \
                patch("request_slack_approval.run") as run, \
                contextlib.redirect_stderr(output):
            self.assertEqual(launcher.main(["--task-file", "task.json",
                                            "--reason", "bad\nreason"]), 2)
        configuration.assert_not_called()
        run.assert_not_called()

    def test_analyzing_checks_before_checkpoint_and_listens_after(self):
        events = []

        def gateway(args, config):
            events.append(args.command)
            return 0

        def update(args):
            events.append((args.status, args.reason))
            return 0

        lock = unittest.mock.MagicMock()
        with patch("slack_runner.run", side_effect=gateway), \
                patch("task_storage.load_json", return_value={"status": "ANALYZING"}), \
                patch("task_storage.task_lock", return_value=lock), \
                patch("execution_policy.validated_analysis", return_value=self.analysis), \
                patch("update_task_state.update", side_effect=update):
            self.assertEqual(launcher.run(Path("task.json"), "UI_CHANGE", self.config), 0)
        self.assertEqual(events, ["check", ("WAITING_APPROVAL", "UI_CHANGE"), "listen"])
        self.assertEqual(lock.__enter__.call_count, 2)

    def test_failed_check_never_changes_task_or_listens(self):
        with patch("slack_runner.run", return_value=2) as gateway, \
                patch("task_storage.load_json", return_value={"status": "ANALYZING"}), \
                patch("task_storage.task_lock"), \
                patch("execution_policy.validated_analysis", return_value=self.analysis), \
                patch("update_task_state.update") as update:
            self.assertEqual(launcher.run(Path("task.json"), "UI_CHANGE", self.config), 2)
        gateway.assert_called_once()
        update.assert_not_called()

    def test_waiting_task_reuses_matching_checkpoint_without_transition(self):
        state = {"status": "WAITING_APPROVAL", "approval_reason": "UI_CHANGE"}
        with patch("slack_runner.run", side_effect=[0, 0]) as gateway, \
                patch("task_storage.load_json", return_value=state), \
                patch("task_storage.task_lock"), \
                patch("execution_policy.validated_analysis", return_value=self.analysis), \
                patch("update_task_state.update") as update:
            self.assertEqual(launcher.run(Path("task.json"), "UI_CHANGE", self.config), 0)
        self.assertEqual([item.args[0].command for item in gateway.call_args_list],
                         ["check", "listen"])
        update.assert_not_called()

    def test_wrong_state_or_reason_refuses_before_network(self):
        for state in ({"status": "IMPLEMENTING"},
                      {"status": "WAITING_APPROVAL", "approval_reason": "OTHER"}):
            with self.subTest(state=state), \
                    patch("task_storage.load_json", return_value=state), \
                    patch("task_storage.task_lock"), \
                    patch("slack_runner.run") as gateway:
                with self.assertRaises(ValueError):
                    launcher.run(Path("task.json"), "UI_CHANGE", self.config)
                gateway.assert_not_called()

    def test_real_task_without_analysis_never_connects_or_changes_state(self):
        scripts = Path(launcher.__file__).parent
        for status in ("ANALYZING", "WAITING_APPROVAL"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "tasks" / "sstc-feature-20261007-0100.json"
                commands = [
                    ("create_task.py", "--source-type", "SSTC_FEATURE", "--source-reference", "manual:approval-test",
                     "--task-id", path.stem, "--task-directory", str(path.parent)),
                    ("update_task_state.py", "--task-file", str(path), "--status", "ANALYZING"),
                ]
                if status == "WAITING_APPROVAL":
                    commands.append(("update_task_state.py", "--task-file", str(path),
                                     "--status", status, "--reason", "UI_CHANGE"))
                for script, *arguments in commands:
                    result = subprocess.run([sys.executable, "-B", str(scripts / script), *arguments],
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                before = {file: file.read_bytes() for file in Path(temporary).rglob("*") if file.is_file()}
                with patch("slack_runner.run") as gateway:
                    with self.assertRaisesRegex(ValueError, "VALID_ANALYSIS_REQUIRED"):
                        launcher.run(path, "UI_CHANGE", self.config)
                gateway.assert_not_called()
                after = {file: file.read_bytes() for file in Path(temporary).rglob("*") if file.is_file()}
                self.assertEqual(after, before)

    def test_real_valid_analysis_creates_checkpoint_after_transport_check(self):
        import test_codex_impact as codex_tests
        import task_storage as storage
        fixture = codex_tests.CodexImpactTest("test_analysis_validates_and_preserves_authority")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertEqual(fixture.execute(), "VALID")
        observed = []

        def gateway(args, config):
            observed.append((args.command, storage.load_json(fixture.path)["status"]))
            return 0

        with patch("slack_runner.run", side_effect=gateway):
            self.assertEqual(launcher.run(fixture.path, "UI_CHANGE", self.config), 0)
        self.assertEqual(observed, [("check", "ANALYZING"), ("listen", "WAITING_APPROVAL")])
        self.assertEqual(storage.load_json(storage.checkpoint_path(fixture.path))["state"], fixture.pair()[0])

    def test_no_change_or_undetermined_analysis_cannot_request_approval(self):
        for outcome in ("NOT_REQUIRED", "UNDETERMINED"):
            analysis = {**self.analysis, "result": {"change_required": outcome}}
            with self.subTest(outcome=outcome), patch("task_storage.task_lock"), \
                    patch("task_storage.load_json", return_value=self.analysis["state"]), \
                    patch("execution_policy.validated_analysis", return_value=analysis), \
                    patch("slack_runner.run") as gateway, patch("update_task_state.update") as update:
                with self.assertRaisesRegex(ValueError, "REQUIRED_ANALYSIS_NEEDED_FOR_APPROVAL"):
                    launcher.run(Path("task.json"), "UI_CHANGE", self.config)
                gateway.assert_not_called()
                update.assert_not_called()

    def test_analysis_changed_during_transport_check_cannot_create_checkpoint(self):
        changed = {**self.analysis, "state": {"status": "IMPLEMENTING"}}
        with patch("task_storage.task_lock"), \
                patch("task_storage.load_json", return_value=self.analysis["state"]), \
                patch("execution_policy.validated_analysis", side_effect=[self.analysis, changed]), \
                patch("slack_runner.run", return_value=0) as gateway, patch("update_task_state.update") as update:
            with self.assertRaisesRegex(ValueError, "APPROVAL_ANALYSIS_CHANGED"):
                launcher.run(Path("task.json"), "UI_CHANGE", self.config)
            self.assertEqual([call.args[0].command for call in gateway.call_args_list], ["check"])
            update.assert_not_called()

    def test_critical_or_feature_contract_analysis_cannot_request_implementation(self):
        for field in ("risk_level", "protocol_contract", "sstd_change_required"):
            result = {**self.analysis["result"]}
            if field == "risk_level":
                result[field] = "CRITICAL"
            else:
                result["impacts"] = {**result["impacts"], field: {"status": "PRESENT"}}
            with self.subTest(field=field), patch("task_storage.task_lock"), \
                    patch("task_storage.load_json", return_value=self.analysis["state"]), \
                    patch("execution_policy.validated_analysis", return_value={**self.analysis, "result": result}), \
                    patch("slack_runner.run") as gateway, patch("update_task_state.update") as update:
                with self.assertRaises(ValueError):
                    launcher.run(Path("task.json"), "UI_CHANGE", self.config)
                gateway.assert_not_called()
                update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
