"""Offline approval tests using real CLI task/checkpoint creation."""

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import slack_approval as approval
import task_storage as storage


class SlackApprovalTest(unittest.TestCase):
    def run_script(self, script, *arguments):
        result = subprocess.run(
            [sys.executable, "-B", str(Path(storage.__file__).parent / script), *arguments],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks" / "sstc-feature-20260914-0001.json"
        self.config = dict(team_id="T1", channel_id="C1", app_id="A1", approver_ids={"U1"})
        self.run_script(
            "create_task.py", "--source-type", "SSTC_FEATURE", "--source-reference", "issue-1",
            "--risk-level", "HIGH", "--task-id", self.path.stem,
            "--task-directory", str(self.path.parent),
        )
        self.move("ANALYZING")
        self.move("WAITING_APPROVAL", "--reason", "human review")
        self.request_path = storage.companion(self.path, "slack-request")
        self.request = self.prepare()
        approval.bind_message(self.path, self.request["nonce"], "123.456")
        self.request = storage.load_json(self.request_path)
        self.payload = {
            "type": "block_actions", "team": {"id": "T1"}, "api_app_id": "A1",
            "user": {"id": "U1"}, "channel": {"id": "C1"},
            "container": {"message_ts": "123.456"},
            "actions": [{"action_id": "ax_approve", "value": self.request["nonce"]}],
        }

    def move(self, status, *extra):
        self.run_script("update_task_state.py", "--task-file", str(self.path),
                        "--status", status, *extra)

    def prepare(self):
        return approval.prepare_request(self.path, "T1", "C1", "A1")

    def decide(self, payload=None, **config):
        return approval.apply_decision(
            self.path, self.payload if payload is None else payload,
            **dict(self.config, **config),
        )

    def evidence(self):
        return tuple(path.read_bytes() for path in (
            self.path, storage.companion(self.path, "log"),
            storage.checkpoint_path(self.path), self.request_path,
        ))

    def test_approve_and_minimal_audit(self):
        self.payload["raw_secret"] = "must never persist"
        self.assertEqual(self.decide(), "IMPLEMENTING")
        state = storage.load_json(self.path)
        log = storage.load_json(storage.companion(self.path, "log"))
        storage.validate_pair(self.path, state, log)
        self.assertEqual(len(log["state_transitions"]), 4)
        self.assertEqual(state["updated_at"], log["state_transitions"][-1]["occurred_at"])
        self.assertEqual(log["approvals"], [{
            "task_id": self.path.stem, "team_id": "T1", "channel_id": "C1",
            "app_id": "A1", "user_id": "U1", "message_ts": "123.456",
            "nonce": self.request["nonce"], "decision": "IMPLEMENTING",
            "occurred_at": state["updated_at"], "snapshot_hash": self.request["snapshot_hash"],
        }])
        self.assertNotIn("must never persist", json.dumps(log))

    def test_reject_and_duplicate(self):
        self.payload["actions"][0]["action_id"] = "ax_reject"
        self.assertEqual(self.decide(), "REJECTED")
        log = storage.load_json(storage.companion(self.path, "log"))
        self.assertEqual(log["finished_at"], log["approvals"][0]["occurred_at"])
        self.assertTrue(log["stop_reason"])
        before = self.evidence()
        self.assertEqual(self.decide(), "REJECTED")
        self.assertEqual(self.evidence(), before)

    def test_wrong_payload_identity_is_ignored(self):
        for key, nested in (("team", "id"), ("user", "id"), ("channel", "id"),
                            ("container", "message_ts"), ("api_app_id", None),
                            ("type", None)):
            with self.subTest(key=key):
                payload = copy.deepcopy(self.payload)
                if nested:
                    payload[key][nested] = "WRONG"
                else:
                    payload[key] = "WRONG"
                before = self.evidence()
                self.assertEqual(self.decide(payload), "IGNORED")
                self.assertEqual(self.evidence(), before)

    def test_wrong_nonce_and_action_are_ignored(self):
        for key in ("value", "action_id"):
            with self.subTest(key=key):
                payload = copy.deepcopy(self.payload)
                payload["actions"][0][key] = "WRONG"
                self.assertEqual(self.decide(payload), "IGNORED")
        self.assertEqual(storage.load_json(self.path)["status"], "WAITING_APPROVAL")

    def test_malformed_payloads_are_ignored(self):
        malformed = [None, [], {}, {"actions": None}, {"actions": []},
                     dict(self.payload, actions=[None]), dict(self.payload, actions={}),
                     dict(self.payload, actions=self.payload["actions"] * 2),
                     dict(self.payload, user={"id": []}),
                     dict(self.payload, team=None)]
        before = self.evidence()
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertEqual(approval.apply_decision(self.path, payload, **self.config),
                                 "IGNORED")
                self.assertEqual(self.evidence(), before)

    def test_config_fails_closed(self):
        before = self.evidence()
        for key in ("team_id", "channel_id", "app_id"):
            for value in ("", " ", None):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        self.decide(**{key: value})
                    args = dict(team_id="T1", channel_id="C1", app_id="A1")
                    args[key] = value
                    with self.assertRaises(ValueError):
                        approval.prepare_request(self.path, **args)
        for allowlist in (set(), {""}, {"U1", " "}, None, ["U1"]):
            with self.assertRaises(ValueError):
                self.decide(approver_ids=allowlist)
        self.assertEqual(self.evidence(), before)

    def test_changed_config_cannot_consume_old_request(self):
        for key, field in (("team_id", "team"), ("channel_id", "channel"),
                           ("app_id", "api_app_id")):
            payload = copy.deepcopy(self.payload)
            payload[field] = "NEW" if field == "api_app_id" else {"id": "NEW"}
            self.assertEqual(self.decide(payload, **{key: "NEW"}), "IGNORED")

    def test_request_snapshot_and_24_hour_expiry(self):
        bundle = {
            "state": storage.load_json(self.path),
            "log": storage.load_json(storage.companion(self.path, "log")),
            "checkpoint": storage.load_json(storage.checkpoint_path(self.path)),
        }
        canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
        self.assertEqual(self.request["snapshot_hash"],
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        remaining = datetime.fromisoformat(self.request["expires_at"]) - datetime.now(timezone.utc)
        self.assertTrue(timedelta(hours=23, minutes=59) < remaining <= timedelta(hours=24))

    def test_bound_request_is_reused_without_write(self):
        before = self.evidence()
        self.assertEqual(self.prepare(), self.request)
        self.assertEqual(self.evidence(), before)

    def test_unbound_send_can_be_retried_with_fresh_nonce(self):
        self.request["message_ts"] = None
        storage.atomic_json(self.request_path, self.request)
        self.assertEqual(self.decide(), "IGNORED")
        fresh = self.prepare()
        self.assertNotEqual(fresh["nonce"], self.request["nonce"])
        self.assertIsNone(fresh["message_ts"])
        with self.assertRaises(ValueError):
            approval.bind_message(self.path, self.request["nonce"], "123.456")
        approval.bind_message(self.path, fresh["nonce"], "222.333")
        self.assertEqual(self.decide(), "IGNORED")

    def test_bind_is_idempotent_but_cannot_rebind(self):
        approval.bind_message(self.path, self.request["nonce"], "123.456")
        before = self.evidence()
        for nonce, message in ((self.request["nonce"], "999.999"), ("bad", "123.456"),
                               ("", "123.456"), (self.request["nonce"], "")):
            with self.assertRaises(ValueError):
                approval.bind_message(self.path, nonce, message)
        self.assertEqual(self.evidence(), before)

    def test_expired_request_is_ignored_and_replaced(self):
        self.request["expires_at"] = "2000-01-01T00:00:00Z"
        storage.atomic_json(self.request_path, self.request)
        before = self.evidence()
        self.assertEqual(self.decide(), "IGNORED")
        with self.assertRaises(ValueError):
            approval.bind_message(self.path, self.request["nonce"], "123.456")
        self.assertEqual(self.evidence(), before)
        self.assertNotEqual(self.prepare()["nonce"], self.request["nonce"])

    def test_invalid_expiry_fails_closed(self):
        for expires in ("bad", "2999-01-01T00:00:00", None):
            self.request["expires_at"] = expires
            storage.atomic_json(self.request_path, self.request)
            self.assertEqual(self.decide(), "IGNORED")

    def test_stale_state_log_or_checkpoint(self):
        paths = (self.path, storage.companion(self.path, "log"),
                 storage.checkpoint_path(self.path))
        for path, key, value in ((paths[0], "attempt", 1),
                                 (paths[1], "unresolved_issues", ["changed"]),
                                 (paths[2], "resume_status", "IMPLEMENTING")):
            with self.subTest(path=path):
                original = storage.load_json(path)
                storage.atomic_json(path, dict(original, **{key: value}))
                before = self.evidence()
                self.assertEqual(self.decide(), "IGNORED")
                with self.assertRaises(ValueError):
                    approval.bind_message(self.path, self.request["nonce"], "123.456")
                if path != paths[2]:
                    with self.assertRaises(ValueError):
                        self.prepare()
                self.assertEqual(self.evidence(), before)
                storage.atomic_json(path, original)

    def test_refreshed_checkpoint_does_not_authorize_old_snapshot(self):
        state = storage.load_json(self.path)
        state["attempt"] += 1
        storage.atomic_json(self.path, state)
        checkpoint = storage.load_json(storage.checkpoint_path(self.path))
        checkpoint["state"] = state
        storage.atomic_json(storage.checkpoint_path(self.path), checkpoint)
        self.assertEqual(self.decide(), "IGNORED")
        self.assertNotEqual(self.prepare()["nonce"], self.request["nonce"])

    def test_critical_approval_forbidden_but_rejection_allowed(self):
        state = storage.load_json(self.path)
        state["risk_level"] = "CRITICAL"
        storage.atomic_json(self.path, state)
        checkpoint = storage.load_json(storage.checkpoint_path(self.path))
        checkpoint["state"] = state
        storage.atomic_json(storage.checkpoint_path(self.path), checkpoint)
        request = self.prepare()
        approval.bind_message(self.path, request["nonce"], "123.456")
        self.payload["actions"][0]["value"] = request["nonce"]
        self.assertEqual(self.decide(), "IGNORED")
        self.payload["actions"][0]["action_id"] = "ax_reject"
        self.assertEqual(self.decide(), "REJECTED")

    def test_duplicate_approval_and_opposite_decision(self):
        self.assertEqual(self.decide(), "IMPLEMENTING")
        before = self.evidence()
        self.assertEqual(self.decide(), "IMPLEMENTING")
        self.payload["actions"][0]["action_id"] = "ax_reject"
        self.assertEqual(self.decide(), "IGNORED")
        self.assertEqual(self.evidence(), before)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_replay_cannot_approve_new_cycle(self):
        self.assertEqual(self.decide(), "IMPLEMENTING")
        self.move("WAITING_APPROVAL", "--reason", "new scope")
        self.assertEqual(self.decide(), "IGNORED")
        fresh = self.prepare()
        approval.bind_message(self.path, fresh["nonce"], "234.567")
        before = self.evidence()
        self.assertEqual(self.decide(), "IGNORED")
        self.assertEqual(self.evidence(), before)
        self.payload["actions"][0]["value"] = fresh["nonce"]
        self.payload["container"]["message_ts"] = "234.567"
        self.assertEqual(self.decide(), "IMPLEMENTING")
        self.assertEqual(len(storage.load_json(storage.companion(self.path, "log"))
                             ["approvals"]), 2)

    def test_missing_checkpoint_is_ignored(self):
        storage.checkpoint_path(self.path).unlink()
        self.assertEqual(self.decide(), "IGNORED")
        with self.assertRaises(OSError):
            self.prepare()

    def test_pending_journal_refused_and_recovered_without_duplicate(self):
        real_write = storage.atomic_json
        for failed_path in (self.path, storage.companion(self.path, "log")):
            with self.subTest(failed_path=failed_path):
                original_state = storage.load_json(self.path)
                original_log = storage.load_json(storage.companion(self.path, "log"))

                def interrupt(path, value):
                    if path == failed_path:
                        raise OSError("injected interruption")
                    real_write(path, value)

                with patch.object(storage, "atomic_json", side_effect=interrupt):
                    with self.assertRaises(OSError):
                        self.decide()
                self.assertTrue(storage.companion(self.path, "pending").exists())
                for operation in (
                    self.prepare, self.decide,
                    lambda: approval.bind_message(self.path, self.request["nonce"], "123.456"),
                ):
                    with self.assertRaisesRegex(ValueError, "recover"):
                        operation()
                for _ in range(2):
                    self.run_script("update_task_state.py", "--task-file", str(self.path),
                                    "--recover")
                before = self.evidence()
                self.assertEqual(self.decide(), "IMPLEMENTING")
                self.assertEqual(self.evidence(), before)
                log = storage.load_json(storage.companion(self.path, "log"))
                self.assertEqual(len(log["approvals"]), 1)
                self.assertEqual(len(log["state_transitions"]), 4)
                storage.atomic_json(self.path, original_state)
                storage.atomic_json(storage.companion(self.path, "log"), original_log)


if __name__ == "__main__":
    unittest.main()
