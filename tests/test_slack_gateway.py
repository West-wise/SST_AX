"""Credential, Unix peer, operation, shared-state and Controller isolation boundaries."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_slack_gateway as service
import slack_credentials as credentials
import slack_gateway_client as client
import sst_ax_controller as controller
import task_storage as storage


class CredentialTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / ".env"
        self.config = {"SLACK_BOT_TOKEN": "xoxb-fixture", "SLACK_APP_TOKEN": "xapp-fixture",
                       "SLACK_TEAM_ID": "T123", "SLACK_CHANNEL_ID": "C123",
                       "SLACK_DAILY_REPORT_CHANNEL_ID": "C456", "SLACK_APP_ID": "A123",
                       "SLACK_APPROVER_IDS": "U123,U456"}
        self.write()

    def write(self, extra="", values=None):
        values = self.config if values is None else values
        self.path.write_text("# managed by the operator\n" + "\n".join(
            name + "=" + value for name, value in values.items()) + "\n" + extra)
        self.path.chmod(0o600)

    def test_private_file_loads_seven_fields_without_env_export(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(credentials.read_credentials(self.path), self.config)
            self.assertFalse(any(key.startswith("SLACK_") for key in os.environ))

    def test_quotes_are_literal_and_no_shell_expansion_runs(self):
        values = {**self.config, "SLACK_BOT_TOKEN": "'xoxb-fixture'"}
        self.write(values=values)
        self.assertEqual(credentials.read_credentials(self.path), self.config)
        self.write(values={**self.config, "SLACK_BOT_TOKEN": "xoxb-$(touch${IFS}" + str(self.root / "pwned") + ")"})
        credentials.read_credentials(self.path)
        self.assertFalse((self.root / "pwned").exists())

    def test_duplicate_typo_missing_empty_and_invalid_values_are_fixed_errors(self):
        changes = [({**self.config, "SLACCK_APP_ID": "A456"}, ""),
                   (self.config, "SLACK_TEAM_ID=T456\n"),
                   ({key: value for key, value in self.config.items() if key != "SLACK_APP_ID"}, ""),
                   ({**self.config, "SLACK_BOT_TOKEN": ""}, ""),
                   ({**self.config, "SLACK_DAILY_REPORT_CHANNEL_ID": "@channel"}, ""),
                   ({**self.config, "SLACK_APP_ID": "secret-invalid-value"}, "")]
        for values, extra in changes:
            with self.subTest(values=list(values)):
                self.write(extra, values)
                with self.assertRaisesRegex(ValueError, "^SLACK_CREDENTIAL_FILE_REFUSED$"):
                    credentials.read_credentials(self.path)

    def test_group_readable_symlink_hardlink_oversize_and_fifo_refused(self):
        self.path.chmod(0o640)
        with self.assertRaises(ValueError):
            credentials.read_credentials(self.path)
        self.path.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            credentials.read_credentials(link)
        os.link(self.path, self.root / "hardlink")
        with self.assertRaises(ValueError):
            credentials.read_credentials(self.path)
        (self.root / "hardlink").unlink()
        self.path.write_bytes(b"x" * 16_385)
        with self.assertRaises(ValueError):
            credentials.read_credentials(self.path)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(ValueError):
            credentials.read_credentials(fifo)

    def test_credential_directory_requires_absolute_systemd_path(self):
        for env in ({}, {"CREDENTIALS_DIRECTORY": "relative"}):
            with self.assertRaises(ValueError):
                credentials.credential_path(env)
        self.assertEqual(credentials.credential_path({"CREDENTIALS_DIRECTORY": "/run/credentials/unit"}),
                         Path("/run/credentials/unit/slack.env"))


class GatewayTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.task_id = "sstc-feature-20261009-0001"
        (self.tasks / (self.task_id + ".json")).write_text("{}")
        (self.tasks / (self.task_id + ".log.json")).write_text("{}")
        self.config = {"socket_path": self.root / "gateway.sock", "tasks_directory": self.tasks,
                       "reports_directory": self.root / "reports", "client_uid": os.getuid() + 1}

    def test_approval_dispatch_reuses_core_and_exposes_no_payload_or_token(self):
        with patch.object(service, "listen_many", return_value=0) as listen:
            result = service.dispatch({"operation": "approvals", "task_ids": [self.task_id]}, self.config, {})
        self.assertEqual(result, "OK")
        listen.assert_called_once_with([self.tasks / (self.task_id + ".json")], {}, timeout_seconds=5)

    def test_arbitrary_text_channels_actions_paths_unknown_ops_and_duplicate_ids_refused(self):
        attacks = [{"operation": "post", "text": "@channel"},
                   {"operation": "check", "channel": "COTHER"},
                   {"operation": "approvals", "task_ids": ["../other"]},
                   {"operation": "approvals", "task_ids": [self.task_id], "payload": {"decision": "approve"}},
                   {"operation": "approvals", "task_ids": [self.task_id] * 2},
                   {"operation": "approvals", "task_ids": []},
                   {"operation": "approvals", "task_ids": [None]}]
        with patch.object(service, "listen_many") as listen, patch.object(service, "slack_run") as check:
            for attack in attacks:
                with self.subTest(attack=attack), self.assertRaises(ValueError):
                    service.dispatch(attack, self.config, {})
            listen.assert_not_called()
            check.assert_not_called()

    def test_symlink_and_external_input_scope_refused_before_slack(self):
        task = self.tasks / (self.task_id + ".json")
        task.unlink()
        task.symlink_to(self.root / "private")
        with self.assertRaises(ValueError):
            service.scoped_task(self.tasks, self.task_id)
        task.unlink()
        task.write_text("{}")
        log = self.tasks / (self.task_id + ".log.json")
        log.write_text(json.dumps({"codex_analysis": {"inputs_directory": str(self.root / "private")}}))
        with self.assertRaises(ValueError):
            service.scoped_task(self.tasks, self.task_id)

    def test_same_uid_service_refuses_before_reading_credentials(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({key: str(value) if isinstance(value, Path) else value
                                     for key, value in {**self.config, "client_uid": os.getuid()}.items()}))
        with patch.object(service, "read_credentials") as read, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(service.main(["--config", str(config), "--check"]), 2)
            read.assert_not_called()

    def test_same_uid_client_and_non_socket_owner_refused(self):
        with self.assertRaises(ValueError):
            client.Gateway(self.config["socket_path"], os.getuid())
        gateway = client.Gateway(self.config["socket_path"], os.getuid() + 1)
        self.config["socket_path"].write_text("private-content")
        with self.assertRaisesRegex(ValueError, "SLACK_GATEWAY_UNAVAILABLE_OR_REFUSED"):
            gateway.check()

    def test_real_unix_peer_uid_and_frame_protocol(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        with left, right:
            self.assertEqual(client.peer_uid(left), os.getuid())
            client.write_frame(left, {"operation": "check"})
            self.assertEqual(client.read_frame(right), {"operation": "check"})
        for raw in (b'{"a":1,"a":2}\n', b'[]\n', b'{}\n{}\n', b'x' * (client.MAX_FRAME + 1)):
            connection = Mock()
            connection.recv.side_effect = [raw]
            with self.subTest(raw=raw[:10]), self.assertRaises(ValueError):
                client.read_frame(connection)

    def test_check_discards_transport_output(self):
        def check(*_):
            print("RAW_SECRET")
            return 0
        output = io.StringIO()
        with patch.object(service, "slack_run", side_effect=check), contextlib.redirect_stdout(output):
            self.assertEqual(service.dispatch({"operation": "check"}, self.config, {}), "OK")
        self.assertNotIn("RAW_SECRET", output.getvalue())


class SharedStateTest(unittest.TestCase):
    def test_atomic_shared_group_mode_private_default_and_other_access_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private.json"
            storage.atomic_json(private, {"v": 1})
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
            root.chmod(0o2770)
            shared = root / "shared.json"
            storage.atomic_json(shared, {"v": 2})
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o660)
            self.assertEqual(json.loads(shared.read_text()), {"v": 2})
            root.chmod(0o2775)
            with self.assertRaisesRegex(ValueError, "SHARED_STATE_DIRECTORY_MUST_BE_PRIVATE"):
                storage.atomic_json(shared, {"v": 3})
            self.assertEqual(json.loads(shared.read_text()), {"v": 2})
            self.assertFalse(list(root.glob(".task-*")))


class ControllerConfigurationTest(unittest.TestCase):
    def test_normal_pending_ui_route_uses_gateway_without_reading_tokens(self):
        from test_controller import ControllerTest
        owner = ControllerTest()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        owner.api.issues = [owner.issue()]
        owner.config.update(slack_gateway_socket=Path("/run/gateway.sock"), slack_gateway_uid=os.getuid() + 1)
        with owner.fake_analysis(risk="HIGH", present=("ui_ux",)), \
                patch.object(client.Gateway, "poll", return_value=0) as poll, \
                patch("slack_runner.configuration", side_effect=AssertionError("Controller read a token")) as legacy:
            result = controller.run_once(owner.config, owner.api)
        self.assertGreaterEqual(poll.call_count, 1)
        legacy.assert_not_called()
        self.assertIsNone(result["approval_error"])
        self.assertIn("WAITING_APPROVAL", result["outcomes"].values())

    def test_auth_failures_checkpoint_and_stop_after_three_without_task_writes(self):
        from datetime import date
        with tempfile.TemporaryDirectory() as directory:
            config = {"tasks_directory": Path(directory) / "tasks", "reports_directory": Path(directory) / "reports"}
            with patch("daily_report.due_date", return_value=date(2026, 10, 8)), \
                    patch("daily_report.build_report", return_value={}) as build, \
                    patch("daily_report.send_report", return_value="AUTH_CHECK_FAILED") as send:
                for _ in range(3):
                    self.assertEqual(service.report_tick(config, {}), "AUTH_CHECK_FAILED")
                self.assertEqual(service.report_tick(config, {}), "REVIEW_REQUIRED")
                self.assertEqual(build.call_count, 3)
                self.assertEqual(send.call_count, 3)
            self.assertFalse(config["tasks_directory"].exists())
            record = json.loads((config["reports_directory"] / "gateway-report-checkpoint.json").read_text())
            self.assertEqual(record["failures"], 3)

    def test_bridge_requires_both_keys_and_distinct_uid_preserves_legacy(self):
        config = {"sstd_repository": "/tmp/server", "sstc_repository": "/tmp/client",
                  "state_directory": "/tmp/state", "worktree_directory": "/tmp/worktrees",
                  "poll_seconds": 30, "bootstrap_sstd_base": "ROOT"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.json"
            path.write_text(json.dumps(config))
            self.assertNotIn("slack_gateway_socket", controller.configuration(path))
            for extra in ({"slack_gateway_socket": "/run/gateway.sock"},
                          {"slack_gateway_uid": os.getuid() + 1},
                          {"slack_gateway_socket": "/run/gateway.sock", "slack_gateway_uid": os.getuid()}):
                path.write_text(json.dumps({**config, **extra}))
                with self.assertRaises(ValueError):
                    controller.configuration(path)
            path.write_text(json.dumps({**config, "slack_gateway_socket": "/run/gateway.sock",
                                        "slack_gateway_uid": os.getuid() + 1}))
            loaded = controller.configuration(path)
            self.assertEqual(loaded["slack_gateway_socket"], Path("/run/gateway.sock"))


if __name__ == "__main__":
    unittest.main()
