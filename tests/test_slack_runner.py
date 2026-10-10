"""Transport boundary checks without a Slack connection or credentials."""
import argparse
import contextlib
import io
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from threading import Event

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

    def test_reply_journal_ack_and_background_send_follow_committed_decision(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        socket.socket_mode_request_listeners = []
        events = []
        socket.send_socket_mode_response.side_effect = lambda value: events.append("ack")
        event = types.SimpleNamespace(type="interactive", payload={}, envelope_id="e1")
        socket.connect.side_effect = lambda: socket.socket_mode_request_listeners[0](socket, event)
        def commit(*args, **kw):
            events.append("commit")
            return "REJECTED"
        def journal(*args):
            events.append("journal")
            return "key"
        def send(*args):
            events.append("send")
            return "SENT"
        with self.transport(web, socket), patch("slack_approval.prepare_request", return_value={"message_ts": "1.2"}), patch("slack_approval.apply_decision", side_effect=commit), patch.object(runner, "prepare_feedback", side_effect=journal), patch.object(runner, "send_feedback", side_effect=send):
            self.assertEqual(runner.run(argparse.Namespace(command="listen", task_file=Path("task.json")), self.config), 0)
        self.assertEqual(events, ["commit", "journal", "ack", "send"])

    def test_persistent_connection_accepts_late_click_and_discovers_new_task(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        socket.socket_mode_request_listeners = []
        paths = [Path("first.json"), Path("late.json")]
        records = [{"nonce": "first", "message_ts": "1.2"}, {"nonce": "late", "message_ts": "3.4"}]
        events, scans = [], []
        class Stop:
            count = 0
            def wait(self, _):
                self.count += 1
                # 700 rescan cycles exceed the former 10-minute bounded loop.
                if self.count == 701:
                    event = types.SimpleNamespace(type="interactive", payload={"actions": [{"value": "late"}]}, envelope_id="late")
                    socket.socket_mode_request_listeners[0](socket, event)
                return self.count > 702
        def discover(_):
            scans.append(1)
            return paths if len(scans) > 600 else paths[:1]
        def prepare(path, *args):
            return records[paths.index(path)]
        def decide(path, *args, **kwargs):
            events.append(path)
            return "IMPLEMENTING"
        with tempfile.TemporaryDirectory() as directory, self.transport(web, socket), patch.object(runner, "trusted_tasks", side_effect=discover), patch.object(runner, "task_lock", return_value=contextlib.nullcontext()), patch.object(runner, "load_json", return_value={"status": "WAITING_APPROVAL"}), patch("slack_approval.prepare_request", side_effect=prepare), patch("slack_approval.apply_decision", side_effect=decide), patch.object(runner, "prepare_feedback", return_value="key"), patch.object(runner, "send_feedback", return_value="SENT"):
            args = argparse.Namespace(command="serve", task_file=None, tasks_directory=Path(directory), stop_event=Stop())
            self.assertEqual(runner.run(args, self.config), 0)
        socket.connect.assert_called_once()
        socket.close.assert_called_once()
        self.assertGreater(len(scans), 700)
        self.assertEqual(events, [paths[1]])
        socket.send_socket_mode_response.assert_called_once()

    def test_unknown_nonce_and_malformed_payload_ack_without_success_reply(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        socket.socket_mode_request_listeners = []
        stop = Event()
        def connect():
            for payload in ({"actions": [{"value": "unknown"}]}, {"actions": None}, [], {"actions": [None]}):
                socket.socket_mode_request_listeners[0](socket, types.SimpleNamespace(type="interactive", payload=payload, envelope_id="e"))
            stop.set()
        socket.connect.side_effect = connect
        with tempfile.TemporaryDirectory() as directory, self.transport(web, socket), patch.object(runner, "trusted_tasks", return_value=[]), patch("slack_approval.apply_decision") as decide, patch.object(runner, "prepare_feedback") as journal, patch.object(runner, "send_feedback") as send:
            args = argparse.Namespace(command="serve", task_file=None, tasks_directory=Path(directory), stop_event=stop)
            self.assertEqual(runner.run(args, self.config), 0)
        self.assertEqual(socket.send_socket_mode_response.call_count, 4)
        decide.assert_not_called()
        journal.assert_not_called()
        send.assert_not_called()

    def test_feedback_failure_still_acks_committed_decision(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        socket.socket_mode_request_listeners = []
        event = types.SimpleNamespace(type="interactive", payload={}, envelope_id="e1")
        socket.connect.side_effect = lambda: socket.socket_mode_request_listeners[0](socket, event)
        output = io.StringIO()
        with self.transport(web, socket), patch("slack_approval.prepare_request", return_value={"message_ts": "1.2"}), patch("slack_approval.apply_decision", return_value="IMPLEMENTING"), patch.object(runner, "prepare_feedback", side_effect=RuntimeError("RAW_SECRET")), contextlib.redirect_stderr(output):
            self.assertEqual(runner.run(argparse.Namespace(command="listen", task_file=Path("task.json")), self.config), 0)
        socket.send_socket_mode_response.assert_called_once()
        self.assertIn("SLACK_FEEDBACK_REVIEW_REQUIRED", output.getvalue())
        self.assertNotIn("RAW_SECRET", output.getvalue())

    def test_ignored_decision_does_not_send_feedback(self):
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        socket.socket_mode_request_listeners = []
        stop = Event()
        event = types.SimpleNamespace(type="interactive", payload={}, envelope_id="e1")
        def connect():
            socket.socket_mode_request_listeners[0](socket, event)
            stop.set()
        socket.connect.side_effect = connect
        with self.transport(web, socket), patch("slack_approval.prepare_request", return_value={"message_ts": "1.2"}), patch("slack_approval.apply_decision", return_value="IGNORED"), patch.object(runner, "prepare_feedback") as journal:
            args = argparse.Namespace(command="listen", task_file=Path("task.json"), stop_event=stop)
            self.assertEqual(runner.run(args, self.config), 0)
        journal.assert_not_called()
        web.chat_postMessage.assert_not_called()

    def test_trusted_scan_rejects_task_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sstc-feature-20261010-0001.json").symlink_to(root / "outside.json")
            with self.assertRaisesRegex(ValueError, "SLACK_TASK_SYMLINK_REFUSED"):
                runner.trusted_tasks(root)


if __name__ == "__main__":
    unittest.main()
