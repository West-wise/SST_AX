"""Transport boundary checks without a Slack connection or credentials."""
import argparse
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import slack_runner as runner


class SlackRunnerTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "SLACK_BOT_TOKEN": "xoxb-fixture", "SLACK_APP_TOKEN": "xapp-fixture",
            "SLACK_TEAM_ID": "T123", "SLACK_APP_ID": "A123",
            "SLACK_CHANNEL_ID": "C123", "SLACK_APPROVER_IDS": "U123,U456",
        }

    def test_missing_invalid_configuration_never_echoes_credentials(self):
        for name in self.config:
            config = dict(self.config)
            config[name] = "secret-invalid-value"
            output = io.StringIO()
            with patch.dict(runner.os.environ, config, clear=True), contextlib.redirect_stderr(output):
                self.assertEqual(runner.main(["check"]), 2)
            self.assertIn(name, output.getvalue())
            self.assertNotIn("secret-invalid-value", output.getvalue())

    def transport(self, web, socket):
        modules = {
            "slack_sdk": types.ModuleType("slack_sdk"),
            "slack_sdk.web": types.ModuleType("slack_sdk.web"),
            "slack_sdk.socket_mode": types.ModuleType("slack_sdk.socket_mode"),
            "slack_sdk.socket_mode.response": types.ModuleType("slack_sdk.socket_mode.response"),
        }
        modules["slack_sdk.web"].WebClient = Mock(return_value=web)
        modules["slack_sdk.socket_mode"].SocketModeClient = Mock(return_value=socket)
        modules["slack_sdk.socket_mode.response"].SocketModeResponse = lambda **kw: kw
        return patch.dict(sys.modules, modules)

    def test_check_connects_closes_and_never_posts_or_changes_task(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        with self.transport(web, socket), patch("slack_approval.prepare_request") as prepare:
            self.assertEqual(runner.run(argparse.Namespace(command="check", task_file=None), self.config), 0)
        socket.connect.assert_called_once()
        socket.close.assert_called_once()
        web.chat_postMessage.assert_not_called()
        prepare.assert_not_called()

    def test_workspace_mismatch_blocks_connect_and_send(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "TOTHER"}
        with self.transport(web, socket), self.assertRaises(ValueError):
            runner.run(argparse.Namespace(command="check", task_file=None), self.config)
        socket.connect.assert_not_called()
        web.chat_postMessage.assert_not_called()

    def test_listener_commits_decision_before_ack_and_reuses_message(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        socket.socket_mode_request_listeners = []
        events = []
        socket.send_socket_mode_response.side_effect = lambda value: events.append("ack")
        payload = {"type": "block_actions"}
        event = types.SimpleNamespace(type="interactive", payload=payload, envelope_id="e1")
        socket.connect.side_effect = lambda: socket.socket_mode_request_listeners[0](socket, event)
        def decide(*args, **kwargs):
            events.append("persist")
            self.assertEqual(kwargs["approver_ids"], {"U123", "U456"})
            return "IMPLEMENTING"
        with self.transport(web, socket), patch("slack_approval.prepare_request", return_value={"message_ts": "1.2"}), patch("slack_approval.apply_decision", side_effect=decide):
            self.assertEqual(runner.run(argparse.Namespace(command="listen", task_file=Path("task.json")), self.config), 0)
        self.assertEqual(events, ["persist", "ack"])
        web.chat_postMessage.assert_not_called()

    def test_errors_do_not_echo_raw_transport_response(self):
        output = io.StringIO()
        with patch.dict(runner.os.environ, self.config, clear=True), patch.object(runner, "run", side_effect=RuntimeError("RAW_SECRET")), contextlib.redirect_stderr(output):
            self.assertEqual(runner.main(["check"]), 2)
        self.assertNotIn("RAW_SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
