"""Offline approval-bound publication, remote validation and Draft PR production."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_validation as sensor
import run_sstc_pipeline as cli
import sstc_candidate as candidate
import sstc_pipeline as pipeline
import sstc_worker as worker
import task_storage as storage
import test_sstc_worker as fixture

CANDIDATE = "c" * 40
WORKFLOW_SHA = "a" * 40
RUN_ID = 117
PREFIX = "repos/" + sensor.REPOSITORY


class FakeGitHub:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []
        self.overrides = {}
        self.run_status = "in_progress"
        self.conclusion = "success"
        self.pull = None
        self.receipt_change = None
        self.branch_sha = CANDIDATE

    def receipt(self):
        result = {"schema_version": "1.0", "task_id": self.owner.path.stem,
                  "repository": sensor.REPOSITORY, "candidate_sha": CANDIDATE,
                  "workflow_ref": sensor.REPOSITORY + "/" + sensor.WORKFLOW + "@refs/heads/main",
                  "workflow_sha": WORKFLOW_SHA, "run_id": RUN_ID, "run_attempt": 1,
                  "build_result": "success", "validation": {"jdk": "17", "commands": sensor.COMMANDS},
                  "authorization": "NONE"}
        if self.receipt_change:
            self.receipt_change(result)
        return result

    def archive(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            entry = zipfile.ZipInfo("validation.json", (2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, json.dumps(self.receipt()))
        return output.getvalue()

    def api(self, endpoint, payload=None, *, binary=False):
        self.calls.append((endpoint, copy.deepcopy(payload), binary))
        if endpoint in self.overrides:
            result = self.overrides[endpoint]
            if callable(result):
                return result(payload)
            if isinstance(result, BaseException):
                raise result
            return copy.deepcopy(result)
        if endpoint == PREFIX + "/git/commits/" + self.owner.revision:
            return {"sha": self.owner.revision, "tree": {"sha": self.owner.base_tree}}
        if endpoint.startswith(PREFIX + "/git/matching-refs/heads/"):
            return []
        if endpoint == PREFIX + "/git/trees":
            return {"sha": self.owner.tree}
        if endpoint == PREFIX + "/git/commits":
            return {"sha": CANDIDATE}
        if endpoint == PREFIX + "/git/commits/" + CANDIDATE:
            return {"sha": CANDIDATE, "tree": {"sha": self.owner.tree},
                    "parents": [{"sha": self.owner.revision}]}
        if endpoint == PREFIX + "/git/refs":
            return {"ref": payload["ref"], "object": {"type": "commit", "sha": CANDIDATE}}
        if endpoint.startswith(PREFIX + "/git/ref/heads/"):
            return {"ref": "refs/heads/" + self.owner.branch,
                    "object": {"type": "commit", "sha": self.branch_sha}}
        if endpoint == PREFIX + "/commits/main":
            return {"sha": WORKFLOW_SHA}
        if endpoint == PREFIX + "/commits/" + CANDIDATE:
            return {"sha": CANDIDATE}
        if endpoint == PREFIX + "/actions/workflows/sstc-validation.yml":
            return {"id": 77, "path": sensor.WORKFLOW, "state": "active"}
        if endpoint == PREFIX + "/actions/workflows/sstc-validation.yml/dispatches":
            return {"workflow_run_id": RUN_ID}
        if endpoint == PREFIX + "/actions/runs/117":
            return {"id": RUN_ID, "workflow_id": 77, "repository": {"full_name": sensor.REPOSITORY},
                    "event": "workflow_dispatch", "head_branch": "main", "head_sha": WORKFLOW_SHA,
                    "path": sensor.WORKFLOW, "run_attempt": 1, "status": self.run_status,
                    "conclusion": self.conclusion}
        if endpoint == PREFIX + "/actions/runs/117/attempts/1/jobs?per_page=100":
            return {"total_count": 3, "jobs": [{"name": name, "status": "completed",
                    "conclusion": "success", "run_id": RUN_ID} for name in ("prepare", "build", "receipt")]}
        if endpoint == PREFIX + "/actions/runs/117/artifacts?per_page=100&name=sstc-validation-117-1":
            archive = self.archive()
            return {"total_count": 1, "artifacts": [{"id": 19, "name": "sstc-validation-117-1",
                    "expired": False, "size_in_bytes": len(archive),
                    "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                    "workflow_run": {"id": RUN_ID, "head_sha": WORKFLOW_SHA}}]}
        if endpoint == PREFIX + "/actions/artifacts/19/zip":
            return self.archive()
        if endpoint == PREFIX + "/pulls" and payload is not None:
            self.pull = {"number": 9, "state": "open", "draft": True, "merged_at": None,
                         "html_url": "https://github.com/" + sensor.REPOSITORY + "/pull/9",
                         "head": {"sha": CANDIDATE, "ref": self.owner.branch,
                                  "repo": {"full_name": sensor.REPOSITORY}},
                         "base": {"ref": "main", "repo": {"full_name": sensor.REPOSITORY}},
                         "title": payload["title"], "body": payload["body"]}
            return copy.deepcopy(self.pull)
        if endpoint.startswith(PREFIX + "/pulls?state=all&base=main&head="):
            return [{"number": self.pull["number"]}] if self.pull else []
        if endpoint == PREFIX + "/pulls/9":
            return copy.deepcopy(self.pull)
        raise AssertionError("Unexpected API request: " + endpoint)


class SstcPipelineTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.SstcWorkerTest()
        self.addCleanup(self.fixture.doCleanups)
        original = fixture.SstcWorkerTest.analysis
        def pinned_analysis(owner):
            owner.manifest["input_context"]["sstc_revision"] = owner.revision
            for evidence in owner.manifest["evidence"]:
                if evidence["source"] == "SSTC":
                    evidence["revision"] = owner.revision
            owner.result["input_context"] = copy.deepcopy(owner.manifest["input_context"])
            storage.atomic_json(owner.inputs / "manifest.json", owner.manifest)
            original(owner)
        with patch.object(fixture.SstcWorkerTest, "analysis", pinned_analysis):
            self.fixture.setUp()
        self.path, self.inputs = self.fixture.task, self.fixture.inputs
        self.revision, self.worktree = self.fixture.revision, self.fixture.worktree
        self.branch = "ax/sstc-sync/pipeline-test"
        self.checkpoint = storage.checkpoint_path(self.path)
        self.checkpoint_bytes = self.checkpoint.read_bytes()
        def implement(executable, session, prompt, cwd, **kwargs):
            self.assertEqual(session, fixture.SESSION)
            self.assertIn(b"Do not push or create a PR", prompt)
            path = cwd / "app/src/main/java/example/Screen.kt"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"package example\n")
            raw = (json.dumps({"type": "thread.started", "thread_id": session}) +
                   '\n{"type":"turn.completed"}\n').encode()
            return 0, raw, None
        with patch.object(worker, "invoke", side_effect=implement), patch.object(worker, "validation") as local:
            self.assertEqual(worker.run(self.path, self.fixture.source, self.worktree,
                                         self.branch, self.inputs, validation_mode="github"), "IMPLEMENTED")
            local.assert_not_called()
        files = candidate.candidate_snapshot(self.worktree, self.revision, self.branch)
        self.tree, _ = candidate.tree_payload(self.worktree, self.revision, self.branch, files)
        self.base_tree = candidate.git(self.worktree, ["rev-parse", self.revision + "^{tree}"]).decode().strip()
        self.client = FakeGitHub(self)

    def pair(self):
        return storage.load_json(self.path), storage.load_json(storage.companion(self.path, "log"))

    def publish(self):
        return pipeline.publish(self.path, self.inputs, self.client)

    def test_real_slack_gate_implementation_publication_pending_and_ready(self):
        record = self.publish()
        self.assertEqual(record["outcome"], "PENDING")
        self.assertEqual(self.pair()[0]["status"], "VALIDATING")
        files = [entry for endpoint, payload, _ in self.client.calls
                 if endpoint == PREFIX + "/git/trees" for entry in payload["tree"]]
        self.assertEqual(files[0]["path"], "app/src/main/java/example/Screen.kt")
        self.assertEqual(files[0]["content"], "package example\n")
        before = {path: path.read_bytes() for path in (self.path, storage.companion(self.path, "log"), self.checkpoint)}
        self.assertEqual(pipeline.check(self.path, self.client)["outcome"], "PENDING")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.client.run_status = "completed"
        ready = pipeline.check(self.path, self.client)
        state, log = self.pair()
        self.assertEqual(ready["outcome"], "READY_FOR_REVIEW")
        self.assertEqual(state["status"], "READY_FOR_REVIEW")
        self.assertEqual(state["draft_pr_url"], self.client.pull["html_url"])
        self.assertEqual(log["draft_pr"]["candidate_sha"], CANDIDATE)
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)
        self.assertEqual(len(log["approvals"]), 1)
        self.assertIn("검증 대상 커밋", self.client.pull["body"])
        self.assertIn(CANDIDATE, self.client.pull["body"])
        self.assertTrue(self.client.pull["draft"])
        calls = copy.deepcopy(self.client.calls)
        self.assertEqual(pipeline.check(self.path, self.client), ready)
        self.assertEqual(self.client.calls, calls)

    def test_missing_or_changed_slack_approval_rejects_before_network(self):
        state, original = self.pair()
        for mutate in (lambda log: log["approvals"].clear(),
                       lambda log: log["approvals"][-1].update(nonce="forged"),
                       lambda log: log["codex_analysis"].update(session_id="00000000-0000-0000-0000-000000000000")):
            with self.subTest(mutation=mutate):
                log = copy.deepcopy(original)
                mutate(log)
                storage.save_pair(self.path, state, log)
                with self.assertRaises((ValueError, IndexError)):
                    self.publish()
                self.assertEqual(self.client.calls, [])
        storage.save_pair(self.path, state, original)

    def test_changed_evidence_or_worker_files_reject_before_network(self):
        evidence = self.inputs / "evidence/client.txt"
        original = evidence.read_bytes()
        evidence.write_bytes(b"altered evidence")
        with self.assertRaises(ValueError):
            self.publish()
        evidence.write_bytes(original)
        (self.worktree / "app/src/main/java/example/Screen.kt").write_bytes(b"later change\n")
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.client.calls, [])

    def test_unsuccessful_or_unverified_local_worker_is_not_publishable(self):
        state, original = self.pair()
        for field, value in (("outcome", "WORKER_FAILED"), ("validation_mode", "local")):
            with self.subTest(field=field):
                log = copy.deepcopy(original)
                log["codex_worker"][field] = value
                storage.save_pair(self.path, state, log)
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.client.calls, [])

    def test_verified_local_worker_still_requires_candidate_actions(self):
        state, log = self.pair()
        log["codex_worker"].update(validation_mode="local",
            validation={"command": "./gradlew testDebugUnitTest assembleDebug", "exit_code": 0})
        storage.save_pair(self.path, state, log)
        result = self.publish()
        self.assertEqual(result["outcome"], "PENDING")
        self.assertEqual(self.pair()[0]["status"], "VALIDATING")
        self.assertFalse(any(endpoint == PREFIX + "/pulls" for endpoint, _, _ in self.client.calls))
        self.assertEqual(sum(endpoint.endswith("/dispatches") and payload is not None
            for endpoint, payload, _ in self.client.calls), 1)

    def test_worker_exit_false_nonzero_or_command_change_rejects(self):
        state, original = self.pair()
        mutations = (lambda log: log["commands"][-1].update(exit_code=False),
                     lambda log: log["commands"][-1].update(exit_code=1),
                     lambda log: log["commands"][-1]["command"].append("--dangerously-bypass-approvals-and-sandbox"))
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                log = copy.deepcopy(original)
                mutate(log)
                storage.save_pair(self.path, state, log)
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertEqual(self.client.calls, [])

    def test_existing_remote_branch_is_never_updated_or_force_pushed(self):
        endpoint = PREFIX + "/git/matching-refs/heads/" + self.branch
        self.client.overrides[endpoint] = [{"ref": "refs/heads/" + self.branch}]
        with self.assertRaisesRegex(ValueError, "REMOTE_BRANCH_ALREADY_EXISTS"):
            self.publish()
        self.assertTrue(all(payload is None for _, payload, _ in self.client.calls))
        self.assertFalse(pipeline.record_path(self.path).exists())

    def test_wrong_remote_tree_blocks_commit_ref_and_repeat_publication(self):
        self.client.overrides[PREFIX + "/git/trees"] = {"sha": "f" * 40}
        with self.assertRaisesRegex(ValueError, "PUBLISHED_TREE_MISMATCH"):
            self.publish()
        self.assertEqual(self.pair()[0]["status"], "IMPLEMENTING")
        self.assertEqual(storage.load_json(pipeline.record_path(self.path))["outcome"], "PUBLISH_UNCERTAIN")
        calls = copy.deepcopy(self.client.calls)
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(self.client.calls, calls)
        self.assertNotIn(PREFIX + "/git/commits", [call[0] for call in calls])

    def test_lost_dispatch_response_is_persisted_and_not_repeated(self):
        dispatch = PREFIX + "/actions/workflows/sstc-validation.yml/dispatches"
        self.client.overrides[dispatch] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertEqual(self.pair()[0]["status"], "VALIDATING")
        self.assertEqual(storage.load_json(sensor.record_path(self.path))["outcome"], "DISPATCH_UNCERTAIN")
        with self.assertRaises(ValueError):
            self.publish()
        with self.assertRaises(ValueError):
            pipeline.check(self.path, self.client)
        self.assertEqual(sum(endpoint == dispatch for endpoint, _, _ in self.client.calls), 1)

    def test_lost_ref_response_reconciles_without_another_publication_post(self):
        self.client.overrides[PREFIX + "/git/refs"] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        before = len(self.client.calls)
        record = pipeline.reconcile(self.path, self.client)
        self.assertEqual(record["outcome"], "DISPATCH_UNCERTAIN")
        self.assertEqual(self.pair()[0]["status"], "VALIDATING")
        self.assertTrue(all(payload is None for _, payload, _ in self.client.calls[before:]))
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)
        sensor.request_validation(self.path, CANDIDATE, self.client)
        self.client.run_status = "completed"
        self.assertEqual(pipeline.check(self.path, self.client)["outcome"], "READY_FOR_REVIEW")

    def test_publication_pair_survives_crash_before_journal_phase_change(self):
        original = pipeline.atomic_json
        def interrupt(path, value):
            if path == pipeline.record_path(self.path) and value.get("outcome") == "DISPATCH_UNCERTAIN":
                raise OSError("interrupted journal update")
            original(path, value)
        with patch.object(pipeline, "atomic_json", side_effect=interrupt), self.assertRaises(OSError):
            self.publish()
        self.assertEqual(self.pair()[0]["status"], "VALIDATING")
        self.assertEqual(storage.load_json(pipeline.record_path(self.path))["outcome"], "PUBLISH_UNCERTAIN")
        before = len(self.client.calls)
        result = pipeline.reconcile(self.path, self.client)
        self.assertEqual(result["outcome"], "DISPATCH_UNCERTAIN")
        self.assertTrue(all(payload is None for _, payload, _ in self.client.calls[before:]))

    def test_publication_journal_replays_exact_transition_after_pair_write_failure(self):
        before = self.pair()
        with patch.object(pipeline, "save_pair", side_effect=OSError("pair write interrupted")), \
                self.assertRaises(OSError):
            self.publish()
        self.assertEqual(self.pair(), before)
        intended = storage.load_json(pipeline.record_path(self.path))["publication_pair"]
        result = pipeline.reconcile(self.path, self.client)
        self.assertEqual(result["outcome"], "DISPATCH_UNCERTAIN")
        self.assertEqual(self.pair(), (intended["state"], intended["log"]))

    def test_uncertain_dispatch_abort_is_terminal_and_preserves_remote_receipts(self):
        endpoint = PREFIX + "/actions/workflows/sstc-validation.yml/dispatches"
        self.client.overrides[endpoint] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        receipt = sensor.record_path(self.path).read_bytes()
        calls = copy.deepcopy(self.client.calls)
        result = pipeline.abort(self.path)
        self.assertEqual(result["outcome"], "BUILD_FAILED")
        self.assertEqual(self.pair()[0]["status"], "BUILD_FAILED")
        self.assertEqual(sensor.record_path(self.path).read_bytes(), receipt)
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)
        self.assertEqual(pipeline.check(self.path, self.client), result)

    def test_abort_journal_replays_before_pending_pair_exists_without_sensor_request(self):
        self.client.overrides[PREFIX + "/git/trees"] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        before = self.pair()
        calls = copy.deepcopy(self.client.calls)
        with patch.object(pipeline, "save_pair", side_effect=OSError("before pending journal")), \
                self.assertRaises(OSError):
            pipeline.abort(self.path)
        self.assertEqual(self.pair(), before)
        self.assertFalse(storage.companion(self.path, "pending").exists())
        record = storage.load_json(pipeline.record_path(self.path))
        self.assertEqual(record["outcome"], "FINALIZING")
        self.assertFalse(sensor.record_path(self.path).exists())
        result = pipeline.check(self.path, self.client)
        self.assertEqual(result["outcome"], "IMPLEMENTATION_FAILED")
        self.assertEqual(self.pair(), (record["final_pair"]["state"], record["final_pair"]["log"]))
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)

    def test_abort_replay_refuses_changes_to_the_recorded_pair(self):
        self.client.overrides[PREFIX + "/git/trees"] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        with patch.object(pipeline, "save_pair", side_effect=OSError("before pending journal")), \
                self.assertRaises(OSError):
            pipeline.abort(self.path)
        before = self.pair()
        record = storage.load_json(pipeline.record_path(self.path))
        record["final_pair"]["log"]["approvals"].clear()
        record["final_snapshot"]["log"] = sensor.digest(record["final_pair"]["log"])
        storage.atomic_json(pipeline.record_path(self.path), record)
        with self.assertRaisesRegex(ValueError, "ABORT_TRANSITION_REFUSED"):
            pipeline.abort(self.path)
        self.assertEqual(self.pair(), before)

    def test_reconciliation_ref_mismatch_preserves_uncertain_pair(self):
        self.client.overrides[PREFIX + "/git/refs"] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        before = self.pair(), pipeline.record_path(self.path).read_bytes()
        self.client.branch_sha = "f" * 40
        with self.assertRaisesRegex(ValueError, "REMOTE_BRANCH_CHANGED"):
            pipeline.reconcile(self.path, self.client)
        self.assertEqual((self.pair(), pipeline.record_path(self.path).read_bytes()), before)

    def test_unknown_candidate_can_be_aborted_without_touching_remote_or_approval(self):
        self.client.overrides[PREFIX + "/git/trees"] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        calls = copy.deepcopy(self.client.calls)
        with self.assertRaisesRegex(ValueError, "CANDIDATE_SHA_REQUIRED"):
            pipeline.reconcile(self.path, self.client)
        result = pipeline.abort(self.path)
        self.assertEqual(result["outcome"], "IMPLEMENTATION_FAILED")
        self.assertEqual(self.pair()[0]["status"], "IMPLEMENTATION_FAILED")
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)
        self.assertEqual(pipeline.check(self.path, self.client), result)

    def test_lost_dispatch_reconciles_only_completed_matching_task_receipt(self):
        endpoint = PREFIX + "/actions/workflows/sstc-validation.yml/dispatches"
        self.client.overrides[endpoint] = TimeoutError("response lost")
        with self.assertRaises(TimeoutError):
            self.publish()
        before = sensor.record_path(self.path).read_bytes()
        with self.assertRaisesRegex(ValueError, "COMPLETED_RECEIPT_REQUIRED_FOR_RECONCILIATION"):
            sensor.check_validation(self.path, self.client, reconcile_run_id=RUN_ID)
        self.assertEqual(sensor.record_path(self.path).read_bytes(), before)
        self.client.run_status = "completed"
        self.client.receipt_change = lambda receipt: receipt.update(task_id="sstc-feature-20260912-9999")
        with self.assertRaisesRegex(ValueError, "RECEIPT_MISMATCH"):
            sensor.check_validation(self.path, self.client, reconcile_run_id=RUN_ID)
        self.assertEqual(sensor.record_path(self.path).read_bytes(), before)
        self.client.receipt_change = None
        self.assertEqual(sensor.check_validation(self.path, self.client, reconcile_run_id=RUN_ID)["outcome"], "VALIDATED")
        self.assertEqual(pipeline.check(self.path, self.client)["outcome"], "READY_FOR_REVIEW")
        self.assertEqual(sum(ep == endpoint and payload is not None for ep, payload, _ in self.client.calls), 1)

    def test_failed_build_transitions_failure_and_never_creates_pr(self):
        self.publish()
        self.client.run_status, self.client.conclusion = "completed", "failure"
        result = pipeline.check(self.path, self.client)
        self.assertEqual(result["outcome"], "BUILD_FAILED")
        state, log = self.pair()
        self.assertEqual(state["status"], "BUILD_FAILED")
        self.assertEqual(log["stop_reason"], "BUILD_FAILED")
        self.assertFalse(any(endpoint == PREFIX + "/pulls" for endpoint, _, _ in self.client.calls))
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)

    def test_lost_producer_dispatch_acknowledgement_reuses_sensor_run(self):
        self.publish()
        record = storage.load_json(pipeline.record_path(self.path))
        record.pop("run_id")
        record["outcome"] = "DISPATCH_UNCERTAIN"
        storage.atomic_json(pipeline.record_path(self.path), record)
        self.assertEqual(storage.load_json(sensor.record_path(self.path))["run_id"], RUN_ID)
        self.client.run_status = "completed"
        result = pipeline.check(self.path, self.client)
        self.assertEqual(result["outcome"], "READY_FOR_REVIEW")
        self.assertEqual(result["run_id"], RUN_ID)
        self.assertEqual(storage.load_json(pipeline.record_path(self.path))["run_id"], RUN_ID)
        dispatch = PREFIX + "/actions/workflows/sstc-validation.yml/dispatches"
        self.assertEqual(sum(endpoint == dispatch and payload is not None
                             for endpoint, payload, _ in self.client.calls), 1)
        self.assertEqual(sum(endpoint == PREFIX + "/pulls" and payload is not None
                             for endpoint, payload, _ in self.client.calls), 1)
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)

    def test_receipt_candidate_task_or_attempt_mismatch_never_creates_pr(self):
        self.publish()
        self.client.run_status = "completed"
        for field, value in (("candidate_sha", "f" * 40), ("task_id", "sstc-feature-20260912-0099"),
                             ("run_attempt", 2), ("workflow_sha", CANDIDATE)):
            with self.subTest(field=field):
                self.client.receipt_change = lambda receipt: receipt.update({field: value})
                with self.assertRaises(ValueError):
                    pipeline.check(self.path, self.client)
                self.assertEqual(self.pair()[0]["status"], "VALIDATING")
        self.assertFalse(any(endpoint == PREFIX + "/pulls" for endpoint, _, _ in self.client.calls))

    def test_remote_branch_movement_after_validated_run_blocks_pr(self):
        self.publish()
        self.client.run_status = "completed"
        self.client.branch_sha = "f" * 40
        with self.assertRaisesRegex(ValueError, "REMOTE_BRANCH_CHANGED"):
            pipeline.check(self.path, self.client)
        self.assertEqual(self.pair()[0]["status"], "VALIDATING")
        self.assertFalse(any(endpoint == PREFIX + "/pulls" for endpoint, _, _ in self.client.calls))

    def test_pending_task_log_and_checkpoint_changes_are_rejected(self):
        self.publish()
        paths = (self.path, storage.companion(self.path, "log"), self.checkpoint)
        for path in paths:
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                data = json.loads(raw)
                data["updated_at" if path == self.path else "changed"] = "2026-10-06T00:00:00Z"
                storage.atomic_json(path, data)
                calls = copy.deepcopy(self.client.calls)
                with self.assertRaises(ValueError):
                    pipeline.check(self.path, self.client)
                self.assertEqual(self.client.calls, calls)
                path.write_bytes(raw)

    def test_pr_timeout_reconciles_existing_draft_without_duplicate_post(self):
        self.publish()
        self.client.run_status = "completed"
        def lost_response(payload):
            del self.client.overrides[PREFIX + "/pulls"]
            self.client.api(PREFIX + "/pulls", payload)
            raise TimeoutError("response lost")
        self.client.overrides[PREFIX + "/pulls"] = lost_response
        with self.assertRaises(TimeoutError):
            pipeline.check(self.path, self.client)
        self.assertEqual(storage.load_json(pipeline.record_path(self.path))["outcome"], "PR_UNCERTAIN")
        before = sum(endpoint == PREFIX + "/pulls" and payload is not None for endpoint, payload, _ in self.client.calls)
        self.assertEqual(pipeline.check(self.path, self.client)["outcome"], "READY_FOR_REVIEW")
        self.assertEqual(sum(endpoint == PREFIX + "/pulls" and payload is not None
                             for endpoint, payload, _ in self.client.calls), before)

    def test_wrong_pr_draft_head_base_or_repo_prevents_ready(self):
        self.publish()
        self.client.run_status = "completed"
        record = storage.load_json(pipeline.record_path(self.path))
        payload = pipeline.pr_payload(self.path, record)
        response = self.client.api(PREFIX + "/pulls", payload)
        mutations = (lambda pr: pr.update(draft=False),
                     lambda pr: pr["head"].update(sha="f" * 40),
                     lambda pr: pr["base"].update(ref="dev"),
                     lambda pr: pr["head"]["repo"].update(full_name="attacker/repo"),
                     lambda pr: pr.update(number=True))
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                altered = copy.deepcopy(response)
                mutate(altered)
                self.client.overrides[PREFIX + "/pulls"] = altered
                self.client.overrides[PREFIX + "/pulls/9"] = altered
                with self.assertRaises(ValueError):
                    pipeline.check(self.path, self.client)
                self.assertEqual(self.pair()[0]["status"], "VALIDATING")

    def test_concurrent_checker_uncertain_pr_is_reconciled_without_duplicate_post(self):
        self.publish()
        self.client.run_status = "completed"
        record = storage.load_json(pipeline.record_path(self.path))
        self.client.api(PREFIX + "/pulls", pipeline.pr_payload(self.path, record))
        self.client.calls.clear()
        original = pipeline.check_validation
        def concurrent_checker(task_file, client):
            result = original(task_file, client)
            persisted = storage.load_json(pipeline.record_path(task_file))
            persisted["outcome"] = "PR_UNCERTAIN"
            storage.atomic_json(pipeline.record_path(task_file), persisted)
            return result
        with patch.object(pipeline, "check_validation", side_effect=concurrent_checker):
            result = pipeline.check(self.path, self.client)
        self.assertEqual(result["outcome"], "READY_FOR_REVIEW")
        self.assertFalse(any(endpoint == PREFIX + "/pulls" and payload is not None
                             for endpoint, payload, _ in self.client.calls))
        self.assertTrue(any(endpoint.startswith(PREFIX + "/pulls?state=all&base=main&head=")
                            for endpoint, _, _ in self.client.calls))
        self.assertEqual(self.checkpoint.read_bytes(), self.checkpoint_bytes)

    def test_cli_exception_never_prints_credentials_or_raw_api_output(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(pipeline, "check", side_effect=ValueError("xoxb-private-secret")), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["check", "--task-file", str(self.path)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("private-secret", stderr.getvalue())
        self.assertIn("SSTC_PIPELINE_ERROR=", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
