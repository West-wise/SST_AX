"""Decision replies are bound to real committed approval audits, never payloads."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import slack_decision_feedback as feedback
import task_storage as storage
import test_slack_approval as fixtures


class SlackFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SlackApprovalTest(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.path
        self.config = {"SLACK_TEAM_ID": "T1", "SLACK_CHANNEL_ID": "C1",
                       "SLACK_APP_ID": "A1", "SLACK_APPROVER_IDS": "U1"}
        self.web = Mock()
        self.web.chat_postMessage.return_value = {"ok": True, "channel": "C1", "ts": "789.123"}

    def commit(self, decision="IMPLEMENTING"):
        if decision == "REJECTED":
            self.fixture.payload["actions"][0]["action_id"] = "ax_reject"
        self.fixture.payload["response_url"] = "RAW_SECRET_NEVER_SAVED"
        self.assertEqual(self.fixture.decide(), decision)
        return feedback.prepare_feedback(self.path, decision, self.config)

    def ledger(self):
        return storage.load_json(storage.companion(self.path, "slack-feedback"))

    def test_approval_and_rejection_use_committed_recipient_and_fixed_korean_reply(self):
        key = self.commit()
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "SENT")
        message = self.web.chat_postMessage.call_args.kwargs
        self.assertEqual(message["channel"], "C1")
        self.assertEqual(message["thread_ts"], "123.456")
        self.assertEqual(message["text"], "SST-AX 승인 접수 완료: " + self.path.stem + "\n승인 결정이 저장되었습니다.")
        self.assertEqual(self.ledger()["deliveries"][key]["status"], "SENT")
        self.assertNotIn("RAW_SECRET", json.dumps(self.ledger()))
        self.assertNotIn(self.fixture.request["nonce"], json.dumps(self.ledger()))

    def test_rejection_reply_does_not_change_saved_rejection(self):
        key = self.commit("REJECTED")
        before = self.fixture.evidence()
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "SENT")
        self.assertIn("거절 접수 완료", self.web.chat_postMessage.call_args.kwargs["text"])
        self.assertEqual(self.fixture.evidence(), before)

    def test_duplicate_envelopes_and_restart_do_not_duplicate_sent_reply(self):
        key = self.commit()
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "SENT")
        self.assertEqual(feedback.prepare_feedback(self.path, "IMPLEMENTING", self.config), key)
        self.assertEqual(feedback.recover_feedback(self.path, self.config), key)
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "SENT")
        self.web.chat_postMessage.assert_called_once()

    def test_missing_verified_audit_cannot_create_a_success_reply(self):
        with self.assertRaises(ValueError):
            feedback.prepare_feedback(self.path, "IMPLEMENTING", self.config)
        self.assertFalse(storage.companion(self.path, "slack-feedback").exists())
        self.web.chat_postMessage.assert_not_called()

    def test_failed_send_preserves_decision_and_never_blind_retries(self):
        key = self.commit()
        before = self.fixture.evidence()
        self.web.chat_postMessage.side_effect = RuntimeError("RAW_SECRET_NEVER_PRINTED")
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "UNCERTAIN")
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "UNCERTAIN")
        self.assertEqual(feedback.recover_feedback(self.path, self.config), key)
        self.assertEqual(self.fixture.evidence(), before)
        self.web.chat_postMessage.assert_called_once()
        self.assertNotIn("RAW_SECRET", json.dumps(self.ledger()))

    def test_crashed_sending_is_uncertain_but_pending_send_can_recover(self):
        key = self.commit()
        self.assertEqual(feedback.recover_feedback(self.path, self.config), key)
        value = self.ledger()
        value["deliveries"][key]["status"] = "SENDING"
        storage.atomic_json(storage.companion(self.path, "slack-feedback"), value)
        self.assertEqual(feedback.recover_feedback(self.path, self.config), key)
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "UNCERTAIN")
        self.web.chat_postMessage.assert_not_called()

    def test_legacy_decision_without_outbox_is_not_sent_on_startup(self):
        self.assertEqual(self.fixture.decide(), "IMPLEMENTING")
        self.assertIsNone(feedback.recover_feedback(self.path, self.config))
        self.web.chat_postMessage.assert_not_called()

    def test_advanced_task_can_deliver_pending_reply_without_new_authority(self):
        key = self.commit()
        self.fixture.move("VALIDATING")
        before = self.fixture.evidence()
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "SENT")
        self.assertEqual(self.fixture.evidence(), before)

    def test_changed_request_snapshot_blocks_send(self):
        key = self.commit()
        request = copy.deepcopy(self.fixture.request)
        request["snapshot_hash"] = "a" * 64
        storage.atomic_json(self.fixture.request_path, request)
        with self.assertRaises(ValueError):
            feedback.send_feedback(self.path, key, self.config, self.web)
        self.web.chat_postMessage.assert_not_called()

    def test_invalid_response_is_uncertain_without_raw_response_storage(self):
        key = self.commit()
        self.web.chat_postMessage.return_value = {"ok": True, "channel": "OTHER", "ts": "789.123",
                                                  "raw": "RAW_SECRET_NEVER_SAVED"}
        self.assertEqual(feedback.send_feedback(self.path, key, self.config, self.web), "UNCERTAIN")
        self.assertNotIn("RAW_SECRET", json.dumps(self.ledger()))


if __name__ == "__main__":
    unittest.main()
