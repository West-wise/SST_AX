"""Offline Actions identity, receipt integrity and authority preservation tests."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import github_validation as sensor
import run_github_validation as cli
import task_storage as storage

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "c" * 40
WORKFLOW_SHA = "a" * 40
RUN_ID = 117
WORKFLOW_ID = 77
ARTIFACT_ID = 19
PREFIX = "repos/" + sensor.REPOSITORY


def archive_bytes(document=None, entries=None):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries or [("validation.json", json.dumps(document))]:
                archive.writestr(name, content)
    return output.getvalue()


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def api(self, endpoint, payload=None, *, binary=False):
        self.calls.append((endpoint, copy.deepcopy(payload), binary))
        if endpoint not in self.responses:
            raise AssertionError("Unexpected network request: " + endpoint)
        response = self.responses[endpoint]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class GitHubValidationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = storage.load_json(ROOT / "tests/fixtures/impact/sstc-required.json")["task"]
        state.update(checkpoint_path="state/checkpoints/" + state["task_id"] + ".json",
                     deferred_until=None, draft_pr_url=None, unresolved_issues=[])
        self.path = self.root / "tasks" / (state["task_id"] + ".json")
        log = storage.load_json(ROOT / "templates/task-log.json")
        log.update({key: state[key] for key in ("task_id", "source_type", "source_reference", "status")})
        log.update(state_transitions=[{"from_status": "RECEIVED", "to_status": "ANALYZING",
                                      "occurred_at": state["updated_at"], "reason": "synthetic analysis"}],
                   approvals=[{"decision": "IMPLEMENTING", "nonce": "preserve-existing-approval"}],
                   codex_analysis={"session_id": "0199a213-81c0-7800-8aa1-bbab2a035a53"})
        storage.save_pair(self.path, state, log)
        self.checkpoint = storage.checkpoint_path(self.path)
        storage.atomic_json(self.checkpoint, {"state": state, "log": log, "resume_status": "ANALYZING"})
        self.original = {path: path.read_bytes() for path in
                         (self.path, storage.companion(self.path, "log"), self.checkpoint)}
        self.client = FakeGitHub()
        self.client.responses.update({
            PREFIX + "/commits/main": {"sha": WORKFLOW_SHA},
            PREFIX + "/actions/workflows/sstc-validation.yml": {
                "id": WORKFLOW_ID, "path": sensor.WORKFLOW, "state": "active"},
            PREFIX + "/commits/" + CANDIDATE: {"sha": CANDIDATE},
            PREFIX + "/actions/workflows/sstc-validation.yml/dispatches": {"workflow_run_id": RUN_ID},
        })
        self.run_endpoint = PREFIX + "/actions/runs/" + str(RUN_ID)
        self.jobs_endpoint = self.run_endpoint + "/attempts/1/jobs?per_page=100"
        self.artifacts_endpoint = self.run_endpoint + "/artifacts?per_page=100&name=sstc-validation-117-1"
        self.archive_endpoint = PREFIX + "/actions/artifacts/19/zip"

    def request(self):
        return sensor.request_validation(self.path, CANDIDATE, self.client)

    def read_record(self):
        return storage.load_json(sensor.record_path(self.path))

    def assert_authority_preserved(self):
        for path, raw in self.original.items():
            self.assertEqual(path.read_bytes(), raw, str(path))

    def prepare_success(self):
        record = self.request()
        receipt = {"schema_version": "1.0", "task_id": self.path.stem,
                   "repository": sensor.REPOSITORY, "candidate_sha": CANDIDATE,
                   "workflow_ref": sensor.REPOSITORY + "/" + sensor.WORKFLOW + "@refs/heads/main",
                   "workflow_sha": WORKFLOW_SHA, "run_id": RUN_ID, "run_attempt": 1,
                   "build_result": "success", "validation": {"jdk": "17", "commands": sensor.COMMANDS},
                   "authorization": "NONE"}
        raw = archive_bytes(receipt)
        self.client.responses.update({
            self.run_endpoint: {"id": RUN_ID, "workflow_id": WORKFLOW_ID,
                "repository": {"full_name": sensor.REPOSITORY}, "event": "workflow_dispatch",
                "head_branch": "main", "head_sha": WORKFLOW_SHA, "path": sensor.WORKFLOW,
                "run_attempt": 1, "status": "completed", "conclusion": "success"},
            self.jobs_endpoint: {"total_count": 3, "jobs": [
                {"name": name, "status": "completed", "conclusion": "success", "run_id": RUN_ID}
                for name in ("prepare", "build", "receipt")]},
            self.artifacts_endpoint: {"total_count": 1, "artifacts": [{
                "id": ARTIFACT_ID, "name": "sstc-validation-117-1", "expired": False,
                "size_in_bytes": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "workflow_run": {"id": RUN_ID, "head_sha": WORKFLOW_SHA}}]},
            self.archive_endpoint: raw,
        })
        return record, receipt

    def test_request_pending_completed_and_exact_sha_binding(self):
        self.prepare_success()
        dispatch = self.client.calls[-1]
        self.assertEqual(dispatch[1], {"ref": "main", "inputs": {
            "task_id": self.path.stem, "candidate_sha": CANDIDATE}})
        self.client.responses[self.run_endpoint]["status"] = "in_progress"
        self.assertEqual(sensor.check_validation(self.path, self.client)["outcome"], "PENDING")
        self.assertNotIn(self.jobs_endpoint, [call[0] for call in self.client.calls])
        self.client.responses[self.run_endpoint]["status"] = "completed"
        result = sensor.check_validation(self.path, self.client)
        self.assertEqual(result["outcome"], "VALIDATED")
        self.assertEqual(result["receipt"]["candidate_sha"], CANDIDATE)
        self.assertEqual(result["workflow_sha"], WORKFLOW_SHA)
        self.assertNotEqual(result["workflow_sha"], result["candidate_sha"])
        self.assertEqual(self.read_record(), result)
        self.assert_authority_preserved()

    def test_completed_failure_does_not_download_or_authorize(self):
        self.prepare_success()
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", None):
            with self.subTest(conclusion=conclusion):
                self.client.responses[self.run_endpoint]["conclusion"] = conclusion
                self.client.calls.clear()
                result = sensor.check_validation(self.path, self.client)
                self.assertEqual(result["outcome"], "FAILED")
                self.assertEqual(len(self.client.calls), 1)
                self.assertEqual(result["authorization"], "NONE")
        self.assert_authority_preserved()

    def test_task_log_and_checkpoint_changes_reject_before_network(self):
        self.request()
        self.client.calls.clear()
        for path, original in self.original.items():
            with self.subTest(path=path.name):
                document = json.loads(original)
                document["updated_at" if path == self.path else "changed"] = "2026-09-30T00:00:00Z"
                storage.atomic_json(path, document)
                with self.assertRaises(sensor.ValidationError):
                    sensor.check_validation(self.path, self.client)
                self.assertEqual(self.client.calls, [])
                path.write_bytes(original)

    def test_invalid_candidate_values_reject_before_network(self):
        for candidate in ("main", "c" * 39, "C" * 40, "c" * 41,
                          CANDIDATE + "\n", "--help", "$(secret)", "a; echo secret", None, True):
            with self.subTest(candidate=candidate), self.assertRaises(sensor.ValidationError):
                sensor.request_validation(self.path, candidate, self.client)
        self.assertEqual(self.client.calls, [])
        self.assertFalse(sensor.record_path(self.path).exists())

    def test_unavailable_or_untrusted_workflow_refuses_dispatch(self):
        endpoint = PREFIX + "/actions/workflows/sstc-validation.yml"
        for key, value in (("path", ".github/workflows/other.yml"), ("state", "disabled_manually"),
                           ("id", True), ("id", 0)):
            with self.subTest(key=key, value=value):
                original = self.client.responses[endpoint][key]
                self.client.responses[endpoint][key] = value
                with self.assertRaises(sensor.ValidationError):
                    self.request()
                self.assertFalse(sensor.record_path(self.path).exists())
                self.client.responses[endpoint][key] = original
        self.assertFalse(any(call[1] is not None for call in self.client.calls))

    def test_candidate_commit_mismatch_refuses_dispatch(self):
        self.client.responses[PREFIX + "/commits/" + CANDIDATE]["sha"] = WORKFLOW_SHA
        with self.assertRaises(sensor.ValidationError):
            self.request()
        self.assertFalse(sensor.record_path(self.path).exists())

    def test_timeout_keeps_uncertain_record_and_never_redispatches(self):
        endpoint = PREFIX + "/actions/workflows/sstc-validation.yml/dispatches"
        self.client.responses[endpoint] = TimeoutError("sensitive transport detail")
        with self.assertRaises(TimeoutError):
            self.request()
        self.assertEqual(self.read_record()["outcome"], "DISPATCH_UNCERTAIN")
        self.assertIsNone(self.read_record()["run_id"])
        calls = len(self.client.calls)
        with self.assertRaises(sensor.ValidationError):
            self.request()
        with self.assertRaises(sensor.ValidationError):
            sensor.check_validation(self.path, self.client)
        self.assertEqual(len(self.client.calls), calls)
        self.assert_authority_preserved()

    def test_missing_dispatch_run_id_keeps_uncertain_record(self):
        self.client.responses[PREFIX + "/actions/workflows/sstc-validation.yml/dispatches"] = {}
        with self.assertRaises(sensor.ValidationError):
            self.request()
        self.assertEqual(self.read_record()["outcome"], "DISPATCH_UNCERTAIN")

    def test_existing_pending_request_cannot_dispatch_again(self):
        self.request()
        calls = len(self.client.calls)
        with self.assertRaises(sensor.ValidationError):
            self.request()
        self.assertEqual(len(self.client.calls), calls)

    def test_tampered_request_fields_refuse_before_network(self):
        self.request()
        original = self.read_record()
        cases = [("repository", "other/repository"), ("authorization", "IMPLEMENTING"),
                 ("schema_version", "2.0"), ("task_id", "sstc-feature-20260912-9999"),
                 ("run_id", True), ("run_attempt", True), ("run_attempt", 2),
                 ("workflow_id", False), ("candidate_sha", "main"), ("workflow_sha", "main")]
        self.client.calls.clear()
        for key, value in cases:
            with self.subTest(key=key, value=value):
                record = dict(original, **{key: value})
                storage.atomic_json(sensor.record_path(self.path), record)
                with self.assertRaises(sensor.ValidationError):
                    sensor.check_validation(self.path, self.client)
                self.assertEqual(self.client.calls, [])
        storage.atomic_json(sensor.record_path(self.path), original)

    def test_wrong_run_workflow_repository_or_attempt_refused(self):
        self.prepare_success()
        original = copy.deepcopy(self.client.responses[self.run_endpoint])
        for key, value in (("id", RUN_ID + 1), ("workflow_id", WORKFLOW_ID + 1),
                           ("repository", {"full_name": "other/repository"}), ("event", "push"),
                           ("head_branch", "feature"), ("head_sha", CANDIDATE),
                           ("path", ".github/workflows/other.yml"), ("run_attempt", 2),
                           ("run_attempt", True)):
            with self.subTest(key=key, value=value):
                self.client.responses[self.run_endpoint] = dict(original, **{key: value})
                with self.assertRaises(sensor.ValidationError):
                    sensor.check_validation(self.path, self.client)
        self.assertEqual(self.read_record()["outcome"], "PENDING")

    def test_unknown_run_status_refused(self):
        self.prepare_success()
        self.client.responses[self.run_endpoint]["status"] = "unknown"
        with self.assertRaises(sensor.ValidationError):
            sensor.check_validation(self.path, self.client)

    def test_jobs_must_cover_all_trusted_jobs_for_this_run(self):
        self.prepare_success()
        original = copy.deepcopy(self.client.responses[self.jobs_endpoint])
        changed = copy.deepcopy(original)
        changed["jobs"][0]["name"] = "untrusted-job"
        cases = [dict(original, total_count=4), dict(original, jobs=original["jobs"][:2]), changed]
        for key, value in (("conclusion", "failure"), ("status", "in_progress"), ("run_id", RUN_ID + 1)):
            changed = copy.deepcopy(original)
            changed["jobs"][1][key] = value
            cases.append(changed)
        for jobs in cases:
            with self.subTest(jobs=jobs):
                self.client.responses[self.jobs_endpoint] = jobs
                with self.assertRaises(sensor.ValidationError):
                    sensor.check_validation(self.path, self.client)

    def test_artifact_uniqueness_and_identity_required(self):
        self.prepare_success()
        original = copy.deepcopy(self.client.responses[self.artifacts_endpoint])
        cases = [{"total_count": 0, "artifacts": []}, {"total_count": 2, "artifacts": original["artifacts"] * 2}]
        for key, value in (("name", "other"), ("expired", True), ("id", True), ("size_in_bytes", 0),
                           ("size_in_bytes", sensor.MAX_ARCHIVE + 1),
                           ("workflow_run", {"id": RUN_ID + 1, "head_sha": WORKFLOW_SHA}),
                           ("workflow_run", {"id": RUN_ID, "head_sha": CANDIDATE})):
            changed = copy.deepcopy(original)
            changed["artifacts"][0][key] = value
            cases.append(changed)
        for artifacts in cases:
            with self.subTest(artifacts=artifacts):
                self.client.responses[self.artifacts_endpoint] = artifacts
                with self.assertRaises(sensor.ValidationError):
                    sensor.check_validation(self.path, self.client)

    def test_archive_digest_mismatch_refuses_receipt(self):
        self.prepare_success()
        self.client.responses[self.archive_endpoint] += b"tampered"
        with self.assertRaisesRegex(sensor.ValidationError, "DIGEST"):
            sensor.check_validation(self.path, self.client)
        self.assertEqual(self.read_record()["outcome"], "PENDING")

    def test_receipt_wrong_task_candidate_workflow_and_result_refused(self):
        record, receipt = self.prepare_success()
        for key, value in (("task_id", "other-task"), ("candidate_sha", WORKFLOW_SHA),
                           ("repository", "other/repo"), ("workflow_sha", CANDIDATE),
                           ("workflow_ref", "untrusted@refs/heads/main"), ("run_id", RUN_ID + 1),
                           ("run_attempt", 2), ("build_result", "failure"),
                           ("authorization", "IMPLEMENTING"), ("schema_version", "2.0"),
                           ("validation", {"jdk": "17", "commands": ["true"]})):
            with self.subTest(key=key), self.assertRaises(sensor.ValidationError):
                sensor.validate_receipt(dict(receipt, **{key: value}), record)
        with self.assertRaises(sensor.ValidationError):
            sensor.validate_receipt(dict(receipt, unexpected=True), record)

    def test_receipt_boolean_integers_are_not_accepted(self):
        record, receipt = self.prepare_success()
        record["run_id"] = receipt["run_id"] = 1
        for key in ("run_id", "run_attempt"):
            with self.subTest(key=key), self.assertRaises(sensor.ValidationError):
                sensor.validate_receipt(dict(receipt, **{key: True}), record)

    def test_archive_exact_filename_no_extra_duplicate_or_traversal(self):
        for entries in ([("../validation.json", "{}")], [("nested/validation.json", "{}")],
                        [("validation.json", "{}"), ("secret.txt", "secret")],
                        [("validation.json", "{}"), ("validation.json", "{}")]):
            with self.subTest(entries=entries), self.assertRaises(sensor.ValidationError):
                sensor.read_archive(archive_bytes(entries=entries))

    def test_archive_size_and_compressed_bomb_refused(self):
        with self.assertRaises(sensor.ValidationError):
            sensor.read_archive(b"x" * (sensor.MAX_ARCHIVE + 1))
        raw = archive_bytes(entries=[("validation.json", "x" * 100_000)])
        self.assertLess(len(raw), sensor.MAX_ARCHIVE)
        with self.assertRaisesRegex(sensor.ValidationError, "RECEIPT_SIZE"):
            sensor.read_archive(raw)

    def test_duplicate_json_fields_refused(self):
        with self.assertRaises(ValueError):
            sensor.read_archive(archive_bytes(entries=[("validation.json", '{"run_id":117,"run_id":118}')]))

    def test_pending_task_pair_blocks_network(self):
        storage.atomic_json(storage.companion(self.path, "pending"), {})
        with self.assertRaises(sensor.ValidationError):
            self.request()
        self.assertEqual(self.client.calls, [])

    def test_missing_required_checkpoint_blocks_network(self):
        self.checkpoint.unlink()
        with self.assertRaises(sensor.ValidationError):
            self.request()
        self.assertEqual(self.client.calls, [])

    def test_cli_output_and_redaction(self):
        self.prepare_success()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sensor, "GitHub", return_value=self.client), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(["check", "--task-file", str(self.path)])
        self.assertEqual(status, 0)
        self.assertIn("GITHUB_VALIDATION=VALIDATED", out.getvalue())
        self.assertIn("AUTHORIZATION=NONE", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sensor, "check_validation", side_effect=RuntimeError("credential-bearing-secret")), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(["check", "--task-file", str(self.path)])
        self.assertEqual(status, 2)
        self.assertNotIn("credential-bearing-secret", err.getvalue() + out.getvalue())
        self.assertIn("CONFIGURATION_TASK_API_OR_JSON_CHECK_FAILED", err.getvalue())

    def test_cli_invalid_argument_does_not_echo_input(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as error:
            cli.main(["--secret=credential-bearing-secret"])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn("credential-bearing-secret", err.getvalue())


if __name__ == "__main__":
    unittest.main()
