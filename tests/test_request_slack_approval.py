"""One-command Slack approval launcher tests."""
import contextlib
import io
import sys
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
                patch("update_task_state.update", side_effect=update):
            self.assertEqual(launcher.run(Path("task.json"), "UI_CHANGE", self.config), 0)
        self.assertEqual(events, ["check", ("WAITING_APPROVAL", "UI_CHANGE"), "listen"])
        lock.__enter__.assert_called_once()

    def test_failed_check_never_changes_task_or_listens(self):
        with patch("slack_runner.run", return_value=2) as gateway, \
                patch("task_storage.load_json", return_value={"status": "ANALYZING"}), \
                patch("update_task_state.update") as update:
            self.assertEqual(launcher.run(Path("task.json"), "UI_CHANGE", self.config), 2)
        gateway.assert_called_once()
        update.assert_not_called()

    def test_waiting_task_reuses_matching_checkpoint_without_transition(self):
        state = {"status": "WAITING_APPROVAL", "approval_reason": "UI_CHANGE"}
        with patch("slack_runner.run", side_effect=[0, 0]) as gateway, \
                patch("task_storage.load_json", return_value=state), \
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
                    patch("slack_runner.run") as gateway:
                with self.assertRaises(ValueError):
                    launcher.run(Path("task.json"), "UI_CHANGE", self.config)
                gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
