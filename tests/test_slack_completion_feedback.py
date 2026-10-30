"""Verified pipeline completion replies, replay safety, and transport integration."""
import argparse
import contextlib
import io
import copy
import json
import os
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_completion_feedback as completion
import slack_decision_feedback as decision
import slack_runner as runner
import run_slack_gateway as gateway
import task_storage as storage
import sst_ax_controller as controller
import test_sstc_pipeline as pipeline_fixture
import test_slack_runner as runner_fixture

class SlackCompletionTest(unittest.TestCase):
    def setUp(self):
        original = pipeline_fixture.fixture.SstcWorkerTest.analysis
        def retained_inputs(owner):
            original(owner)
            state = storage.load_json(owner.task)
            log = storage.load_json(storage.companion(owner.task, "log"))
            log["codex_analysis"]["inputs_directory"] = str(owner.inputs.absolute())
            storage.save_pair(owner.task, state, log, checkpoint_status="ANALYZING")
        self.fixture = pipeline_fixture.SstcPipelineTest(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        with patch.object(pipeline_fixture.fixture.SstcWorkerTest, "analysis", retained_inputs):
            self.fixture.setUp()
        self.path = self.fixture.path
        self.config = {"SLACK_TEAM_ID": "T1", "SLACK_CHANNEL_ID": "C1", "SLACK_APP_ID": "A1",
                       "SLACK_APPROVER_IDS": "U1", "SLACK_BOT_TOKEN": "xoxb-fixture", "SLACK_APP_TOKEN": "xapp-fixture"}
        self.web = Mock()
        self.web.auth_test.return_value = {"team_id": "T1"}
        self.web.chat_postMessage.return_value = {"ok": True, "channel": "C1", "ts": "789.123"}

    def arm(self):
        return decision.prepare_feedback(self.path, "IMPLEMENTING", self.config)

    def ready(self):
        self.fixture.publish()
        self.fixture.client.run_status = "completed"
        result = pipeline_fixture.pipeline.check(self.path, self.fixture.client)
        self.assertEqual(result["outcome"], "READY_FOR_REVIEW")

    def evidence(self):
        return {str(p): p.read_bytes() for p in (self.path, storage.companion(self.path, "log"),
            storage.checkpoint_path(self.path), storage.companion(self.path, "pipeline"),
            storage.companion(self.path, "github-validation")) if p.exists()}

    def test_verified_completion_replies_to_approval_thread_with_pr_and_preserves_task(self):
        self.arm(); self.ready(); before = self.evidence()
        self.assertEqual(completion.deliver_completions([self.path], self.config, self.web), 0)
        message = self.web.chat_postMessage.call_args.kwargs
        self.assertEqual((message["channel"], message["thread_ts"]), ("C1", "123.456"))
        self.assertIn("작업 완료", message["text"])
        self.assertIn("https://github.com/West-wise/Server_State_Telemetry_Client/pull/9", message["text"])
        self.assertIn("PR을 검토", message["text"])
        self.assertFalse(message["unfurl_links"])
        self.assertEqual(completion.load_delivery(self.path)["status"], "SENT")
        self.assertEqual(self.evidence(), before)

    def test_no_notification_or_authentication_before_required_validation(self):
        self.arm()
        self.assertEqual(completion.deliver_completions([self.path], self.config, self.web), 0)
        self.web.auth_test.assert_not_called(); self.web.chat_postMessage.assert_not_called()
        self.assertEqual(completion.load_delivery(self.path)["status"], "WAITING")

    def test_old_completion_without_subscription_is_not_backfilled(self):
        self.ready()
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "NOT_ARMED")
        self.web.chat_postMessage.assert_not_called()

    def test_sent_reply_is_not_repeated_on_scan_restart_or_duplicate_approval(self):
        self.arm(); self.ready()
        self.assertEqual(completion.deliver_completions([self.path], self.config, self.web), 0)
        self.arm()
        self.assertEqual(completion.deliver_completions([self.path], self.config, self.web), 0)
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "SENT")
        self.web.chat_postMessage.assert_called_once(); self.web.auth_test.assert_called_once()

    def test_post_response_loss_is_uncertain_and_does_not_repeat_or_change_task(self):
        self.arm(); self.ready(); before = self.evidence()
        self.web.chat_postMessage.side_effect = TimeoutError("RAW_TRANSPORT_MUST_NOT_PERSIST")
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "UNCERTAIN")
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "UNCERTAIN")
        self.web.chat_postMessage.assert_called_once()
        self.assertEqual(self.evidence(), before)
        self.assertNotIn("RAW_TRANSPORT", json.dumps(completion.load_delivery(self.path)))

    def test_mismatched_slack_response_never_reports_sent(self):
        self.arm(); self.ready()
        self.web.chat_postMessage.return_value = {"ok": True, "channel": "COTHER", "ts": "789.123"}
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "UNCERTAIN")
        self.assertIsNone(completion.load_delivery(self.path)["message_ts"])

    def test_tampered_sensor_cannot_produce_success_reply(self):
        self.arm(); self.ready()
        p = storage.companion(self.path, "github-validation")
        sensor = storage.load_json(p); sensor["candidate_sha"] = "b" * 40; storage.atomic_json(p, sensor)
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "REFUSED")
        self.web.chat_postMessage.assert_not_called()

    def test_forged_ready_state_without_producer_receipts_is_refused(self):
        self.arm()
        state = storage.load_json(self.path); log = storage.load_json(storage.companion(self.path, "log"))
        state["status"] = log["status"] = "READY_FOR_REVIEW"
        state["draft_pr_url"] = "https://github.com/West-wise/Server_State_Telemetry_Client/pull/9"
        log["state_transitions"].append({"from_status": "IMPLEMENTING", "to_status": "READY_FOR_REVIEW",
            "occurred_at": state["updated_at"], "reason": "Synthetic forgery"})
        storage.save_pair(self.path, state, log)
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "REFUSED")
        self.web.chat_postMessage.assert_not_called()

    def test_changed_approval_recipient_is_refused_before_send(self):
        self.arm(); self.ready()
        config = {**self.config, "SLACK_CHANNEL_ID": "COTHER"}
        with self.assertRaises(ValueError):
            completion.send_completion(self.path, config, self.web)
        self.web.chat_postMessage.assert_not_called()

    def sending(self):
        self.arm(); self.ready()
        with storage.task_lock(self.path):
            value, status = completion.advance(self.path, self.config)
            self.assertEqual(status, "PENDING")
            value.update(status="SENDING", sender_pid=os.getpid())
            storage.atomic_json(completion.ledger_path(self.path), value)

    def test_interrupted_post_is_not_retried_after_sender_process_stops(self):
        self.sending()
        with patch.object(completion, "process_stopped", return_value=True):
            self.assertEqual(completion.send_completion(self.path, self.config, self.web), "UNCERTAIN")
        self.web.chat_postMessage.assert_not_called()

    def test_concurrent_scan_does_not_reclassify_an_active_sender(self):
        self.sending()
        self.assertEqual(completion.send_completion(self.path, self.config, self.web), "SENDING")
        self.assertEqual(completion.load_delivery(self.path)["status"], "SENDING")
        self.web.chat_postMessage.assert_not_called()

    def test_prepost_authentication_failures_stop_after_three_and_preserve_completion(self):
        self.arm(); self.ready(); before = self.evidence()
        self.web.auth_test.return_value = {"team_id": "TOTHER"}
        for _ in range(4):
            self.assertEqual(completion.deliver_completions([self.path], self.config, self.web), 2)
        self.assertEqual(self.web.auth_test.call_count, 3)
        self.web.chat_postMessage.assert_not_called()
        self.assertEqual(completion.load_delivery(self.path)["status"], "REFUSED")
        self.assertEqual(self.evidence(), before)

    def test_persistent_receiver_delivers_completion_once_after_restart(self):
        self.arm(); self.ready()
        socket = Mock(); socket.socket_mode_request_listeners = []
        stop = Event(); stop.set()
        helper = runner_fixture.SlackRunnerTest(methodName="runTest")
        with helper.transport(self.web, socket), patch.object(runner, "recover_feedback", return_value=None):
            args = argparse.Namespace(command="serve", task_file=None, tasks_directory=self.path.parent, stop_event=stop)
            self.assertEqual(runner.run(args, self.config), 0)
        self.web.chat_postMessage.assert_called_once()
        self.assertEqual(completion.load_delivery(self.path)["status"], "SENT")

    def test_gateway_tick_uses_same_verified_completion_transport_without_socket_mode(self):
        self.arm(); self.ready()
        socket = Mock()
        helper = runner_fixture.SlackRunnerTest(methodName="runTest")
        with helper.transport(self.web, socket):
            self.assertEqual(gateway.completion_tick({"tasks_directory": self.path.parent}, self.config), 0)
        self.web.chat_postMessage.assert_called_once()
        socket.connect.assert_not_called()

    def test_external_pr_url_in_delivery_is_refused_before_network(self):
        self.arm(); self.ready()
        with storage.task_lock(self.path):
            value, _ = completion.advance(self.path, self.config)
        value["pr_url"] = "https://evil.invalid/pull/9"
        storage.atomic_json(completion.ledger_path(self.path), value)
        with self.assertRaises(ValueError):
            completion.send_completion(self.path, self.config, self.web)
        self.web.chat_postMessage.assert_not_called()

    def test_controller_notification_failure_does_not_change_ready_task_or_failure_count(self):
        self.arm(); self.ready(); before = self.evidence()
        root = self.path.parent.parent
        journal_path = root / "controller.json"
        event = {"task_id": self.path.stem, "failures": 0, "active_seconds": 0}
        storage.atomic_json(journal_path, {"schema_version": "1.0", "events": {"fixture": event}, "sstd_cursor": "d" * 40})
        config = {"state_directory": root, "worktree_directory": root / "controller-worktrees"}
        remote = Mock(); remote.api.return_value = {"sha": "a" * 40}
        with patch.object(controller, "materialize"), patch.object(controller, "discover", return_value=[]), patch.object(runner, "configuration", return_value=self.config), patch.object(completion, "deliver_completions", return_value=2) as deliver, contextlib.redirect_stderr(io.StringIO()):
            result = controller.run_once(config, remote)
        self.assertEqual(result["outcomes"][self.path.stem], "READY_FOR_REVIEW")
        deliver.assert_called_once_with([self.path], self.config)
        journal = storage.load_json(journal_path)
        self.assertEqual(journal["events"]["fixture"]["failures"], 0)
        self.assertEqual(journal["sstd_cursor"], "d" * 40)
        self.assertEqual(self.evidence(), before)
