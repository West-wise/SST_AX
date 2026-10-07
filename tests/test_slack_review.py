"""Approval displays and polling are bound to retained analysis evidence."""
import copy
import shutil
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import test_codex_impact as codex_tests
import test_slack_runner as slack_tests
import slack_approval as approval
import slack_runner as runner
import task_storage as storage


class ApprovalReviewTest(unittest.TestCase):
    def setUp(self):
        self.fixture = codex_tests.CodexImpactTest("test_analysis_validates_and_preserves_authority")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        inputs = fixture.path.parent / (fixture.path.stem + ".inputs")
        shutil.move(fixture.inputs, inputs)
        fixture.inputs = inputs
        fixture.execute()
        fixture.move("WAITING_APPROVAL", "--reason", "UI_CHANGE")
        self.request = approval.prepare_request(fixture.path, "T1", "C1", "A1")

    def test_display_contains_verified_summary_source_and_decision_scope(self):
        review = approval.approval_review(self.fixture.path, self.request)
        self.assertEqual(review["summary"], self.fixture.result["summary"])
        self.assertEqual(review["input_context"], self.fixture.manifest["input_context"])
        message = runner.approval_message(self.request, review)
        sections = "\n".join(block["text"]["text"] for block in message["blocks"]
                             if block["type"] == "section")
        self.assertIn("UI_CHANGE", sections)
        self.assertIn(review["input_context"]["sstc_revision"], sections)
        self.assertIn("구현 단계 진입만", sections)
        self.assertIn("미해결 질문", sections)
        self.assertTrue(all(block["text"]["type"] == "plain_text"
                            for block in message["blocks"] if block["type"] == "section"))

    def test_modified_result_or_evidence_cannot_be_sent_for_approval(self):
        fixture = self.fixture
        _, log = fixture.pair()
        artifact = fixture.path.parent / log["codex_analysis"]["result_file"]
        original = artifact.read_bytes()
        changed = copy.deepcopy(fixture.result)
        changed["summary"] = "Different scope"
        storage.atomic_json(artifact, changed)
        with self.assertRaises(ValueError):
            approval.approval_review(fixture.path, self.request)
        artifact.write_bytes(original)
        evidence = next((fixture.inputs / "evidence").glob("*.txt"))
        evidence.write_text("modified evidence", encoding="utf-8")
        with self.assertRaises(ValueError):
            approval.approval_review(fixture.path, self.request)

    def test_changed_snapshot_cannot_render_an_old_request(self):
        state, log = self.fixture.pair()
        log["commands"].append({"name": "scope changed"})
        storage.save_pair(self.fixture.path, state, log)
        with self.assertRaises(ValueError):
            approval.approval_review(self.fixture.path, self.request)

    def test_long_untrusted_text_is_bounded_and_does_not_enter_notification(self):
        review = approval.approval_review(self.fixture.path, self.request)
        review["summary"] = "<@U123> " * 1000
        message = runner.approval_message(self.request, review)
        self.assertNotIn("<@U123>", message["text"])
        self.assertTrue(all(len(block["text"]["text"]) <= 3000
                            for block in message["blocks"] if block["type"] == "section"))


class ApprovalPollingTest(unittest.TestCase):
    def test_single_connection_routes_second_task_nonce_before_ack(self):
        harness = slack_tests.SlackRunnerTest()
        harness.setUp()
        web, socket = Mock(), Mock()
        web.auth_test.return_value = {"team_id": "T123"}
        socket.socket_mode_request_listeners = []
        paths = [Path("first.json"), Path("second.json")]
        records = [{"message_ts": "1.1", "nonce": "first"},
                   {"message_ts": "1.2", "nonce": "second"}]
        events = []
        socket.send_socket_mode_response.side_effect = lambda value: events.append("ack")
        event = SimpleNamespace(type="interactive", envelope_id="event", payload={
            "actions": [{"value": "second"}]})
        socket.connect.side_effect = lambda: socket.socket_mode_request_listeners[0](socket, event)

        def decide(path, *args, **kwargs):
            events.append(path.name)
            return "IMPLEMENTING"

        with harness.transport(web, socket), \
                patch.object(approval, "prepare_request", side_effect=records), \
                patch.object(approval, "apply_decision", side_effect=decide), \
                patch.object(runner, "monotonic", side_effect=[0, 2]):
            self.assertEqual(runner.listen_many(paths, harness.config, 1), 0)
        self.assertEqual(events, ["second.json", "ack"])
        socket.connect.assert_called_once()
        socket.close.assert_called_once()
        web.chat_postMessage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
