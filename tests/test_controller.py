"""Offline polling, pinned context and shared Controller route integration tests."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_impact
import controller_inputs
import run_controller
import sst_ax_controller as controller
import task_storage as storage
import sstc_candidate
import sstc_worker
import slack_approval
import test_sstc_pipeline as publication_fixture

SESSION = "0199a213-81c0-7800-8aa1-bbab2a035a53"
SERVER = "repos/West-wise/Server_State_Telemetry_Demon"
CLIENT = "repos/West-wise/Server_State_Telemetry_Client"


class FakeIntake:
    def __init__(self, owner):
        self.owner = owner
        self.issues = []
        self.releases = []
        self.tag_revisions = {}
        self.calls = []

    def api(self, endpoint, payload=None, *, binary=False):
        self.calls.append((endpoint, payload))
        if endpoint == SERVER + "/commits/main":
            return {"sha": self.owner.server_revision, "parents": [{"sha": self.owner.server_base}]}
        if endpoint.startswith(SERVER + "/commits/"):
            tag = endpoint.rsplit("/", 1)[-1]
            revision = self.tag_revisions.get(tag, self.owner.server_revision)
            parent = self.owner.git(self.owner.server, "rev-parse", revision + "^")
            return {"sha": revision, "parents": [{"sha": parent}]}
        if endpoint.startswith(SERVER + "/compare/"):
            base, head = endpoint.split("/compare/")[1].split("?")[0].split("...")
            merge = self.owner.git(self.owner.server, "merge-base", base, head)
            status = "identical" if base == head else "ahead" if merge == base else "behind" if merge == head else "diverged"
            return {"status": status, "base_commit": {"sha": base}, "merge_base_commit": {"sha": merge}}
        if endpoint == SERVER + "/releases?per_page=100":
            return copy.deepcopy(self.releases)
        if endpoint == CLIENT + "/issues?state=open&per_page=100":
            return copy.deepcopy(self.issues)
        if endpoint == CLIENT + "/commits/main":
            return {"sha": self.owner.client_revision}
        raise AssertionError("Unexpected API request: " + endpoint)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server, self.client = self.root / "SSTD", self.root / "SSTC"
        self.environment = dict(os.environ)
        self.environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                                GIT_TERMINAL_PROMPT="0")
        for repository in (self.server, self.client):
            repository.mkdir()
            self.git(repository, "init", "-q")
            self.git(repository, "config", "user.name", "Test")
            self.git(repository, "config", "user.email", "test@example.invalid")
            self.git(repository, "config", "core.autocrlf", "false")
        self.write(self.server, "src/Protocol.cpp", b'packet["cpu_usage_pct"] = cpu * 100.0;\n')
        self.write(self.server, "include/Metrics.h", b"double cpu_fraction;\n")
        self.server_base = self.commit(self.server)
        self.write(self.server, "src/Protocol.cpp", b'packet["cpu_usage_pct"] = cpu;\n')
        self.write(self.server, "src/Logging.cpp", b"void log() {}\n")
        self.server_revision = self.commit(self.server)
        for name, data in {
            "AGENTS.md": b"Use repository CI commands and keep existing navigation.\n",
            ".github/workflows/android-ci.yml": b"name: Android CI\njobs:\n  build:\n    steps:\n      - uses: actions/setup-java@v4\n        with:\n          java-version: '17'\n      - run: chmod +x gradlew\n      - run: ./gradlew testDebugUnitTest assembleDebug\n",
            "app/src/main/java/example/PacketDecoder.kt": b'fun decode(v:Double) = v / 100.0\n',
            "app/src/main/java/example/Units.kt": b"fun percent(v:Double) = v * 100.0\n",
            "app/src/main/java/example/Repository.kt": b"class Repository\n",
            "app/src/main/java/example/StatusViewModel.kt": b"class StatusViewModel\n",
            "app/src/main/java/example/Screen.kt": b"fun display(value:Double) = value.toString()\n",
            "app/src/test/java/example/PacketTest.kt": b"class PacketTest\n",
            "app/build.gradle.kts": b"plugins {}\n",
        }.items():
            self.write(self.client, name, data)
        self.client_revision = self.commit(self.client)
        self.config = {"sstd_repository": self.server, "sstc_repository": self.client,
                       "state_directory": self.root / "state", "worktree_directory": self.root / "worktrees",
                       "poll_seconds": 15, "bootstrap_sstd_base": self.server_base}
        self.api = FakeIntake(self)

    def git(self, repository, *args):
        result = subprocess.run(["git", *args], cwd=repository, env=self.environment,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode().strip()

    def write(self, repository, name, data):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def commit(self, repository):
        self.git(repository, "add", ".")
        self.git(repository, "commit", "-qm", "fixture")
        return self.git(repository, "rev-parse", "HEAD")

    def issue(self, body="Requested internal parsing test", number=7):
        return {"number": number, "title": "[AX] Request", "body": body}

    def journal(self):
        return storage.load_json(self.config["state_directory"] / "controller.json")

    def fake_analysis(self, decision="REQUIRED", risk="LOW", present=()):
        def invoke(command, prompt, cwd, on_session, *, timeout=None, on_tick=None):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, codex_impact.TIMEOUT)
            on_tick()
            on_session(SESSION)
            payload = json.loads(prompt[prompt.index(b'{"bodies"'):])
            manifest = payload["manifest"]
            if manifest["source_type"] == "SSTD_CHANGE":
                source = next(body for name, body in payload["bodies"].items()
                              if any(item["evidence_id"] == name and item["kind"] == "source_diff"
                                     for item in manifest["evidence"]))
                if decision == "NOT_REQUIRED":
                    self.assertIn("Logging.cpp", source)
                    self.assertNotIn("cpu_usage_pct", source)
                else:
                    self.assertIn('-packet["cpu_usage_pct"] = cpu * 100.0;', source)
                    self.assertIn('+packet["cpu_usage_pct"] = cpu;', source)
            ids = [item["evidence_id"] for item in manifest["evidence"]]
            keys = ("ui_ux", "protocol_contract", "dependency", "android_permission",
                    "destructive_action", "sstd_change_required")
            reasons = {"ui_ux": "UI_CHANGE", "protocol_contract": "PROTOCOL_CHANGE"}
            present_keys = tuple(present) + (("protocol_contract",) if manifest["source_type"] == "SSTD_CHANGE"
                                             and decision == "REQUIRED" and "protocol_contract" not in present else ())
            result = {"schema_version": "1.0", "task_id": manifest["task_id"],
                      "source_type": manifest["source_type"], "input_context": manifest["input_context"],
                      "change_required": decision, "summary": "Reviewed fixed source and all Android consumers",
                      "risk_level": risk, "evidence": [{"evidence_id": value, "reason": "Read fixed bytes"} for value in ids],
                      "impacts": {key: {"status": "UNKNOWN" if decision == "UNDETERMINED" else
                                                   "PRESENT" if key in present_keys else "ABSENT",
                                        "reason": "Reviewed fixed consumer relationship", "evidence_ids": ids} for key in keys},
                      "approval_reasons": [reasons[key] for key in present_keys] + (["HIGH_RISK"] if risk == "HIGH" else []),
                      "unresolved_questions": ["Clarify source units"] if decision == "UNDETERMINED" else []}
            events = [{"type": "thread.started", "thread_id": SESSION},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}},
                      {"type": "turn.completed"}]
            return 0, b"\n".join(json.dumps(event).encode() for event in events), None
        return patch.object(codex_impact, "invoke", side_effect=invoke)

    def test_context_selection_includes_all_consumers_tests_ci_and_server_support(self):
        selected = controller_inputs.select_context(self.client, self.client_revision, self.server,
                                                    self.server_revision, self.server_base)
        self.assertEqual(selected["sstd_path"], ["src/Logging.cpp", "src/Protocol.cpp"])
        self.assertIn("include/Metrics.h", selected["sstd_context"])
        self.assertIn("app/src/main/java/example/Units.kt", selected["sstc_context"])
        self.assertIn("app/src/main/java/example/Screen.kt", selected["sstc_context"])
        self.assertIn("app/src/test/java/example/PacketTest.kt", selected["sstc_context"])
        self.assertIn(".github/workflows/android-ci.yml", selected["sstc_context"])
        self.write(self.client, "app/src/main/java/example/Units.kt", b"unapproved dirty value\n")
        self.assertEqual(controller_inputs.select_context(self.client, self.client_revision),
                         {**selected, "sstd_path": [], "sstd_context": []})

    def test_oversized_context_selection_escalates_instead_of_truncating_selection(self):
        for number in range(129):
            self.write(self.client, f"app/src/main/java/example/Added{number}.kt", b"data\n")
        self.client_revision = self.commit(self.client)
        with self.assertRaisesRegex(ValueError, "CONTEXT_BUDGET_REQUIRES_REVIEW"):
            controller_inputs.select_context(self.client, self.client_revision)

    def test_same_commit_release_and_repeated_polls_create_one_source_task(self):
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
            self.api.releases = [{"id": 1, "tag_name": "v1", "draft": False, "prerelease": False}]
            controller.run_once(self.config, self.api)
        events = self.journal()["events"]
        self.assertEqual(len(events), 1)
        event = next(iter(events.values()))
        self.assertEqual(event["base_revision"], self.server_base)
        self.assertEqual(event["source_revision"], self.server_revision)
        self.assertEqual(event["sstc_revision"], self.client_revision)

    def test_later_release_of_intermediate_main_commit_never_creates_backward_task(self):
        intermediate = self.server_revision
        self.write(self.server, "src/Logging.cpp", b"void log() { /* later main */ }\n")
        self.server_revision = self.commit(self.server)
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
            first = self.journal()["events"]
            self.api.tag_revisions["vB"] = intermediate
            self.api.releases = [{"id": 3, "tag_name": "vB", "draft": False, "prerelease": False}]
            controller.run_once(self.config, self.api)
        after = self.journal()["events"]
        self.assertEqual(set(after), set(first))
        for key, event in first.items():
            for field in ("task_id", "source_revision", "base_revision", "sstc_revision"):
                self.assertEqual(after[key][field], event[field])
        self.assertEqual(self.journal()["release_ids"], [3])
        self.assertNotIn("sstd:" + intermediate, self.journal()["events"])

    def test_new_release_ahead_of_observed_main_remains_an_input(self):
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
            self.write(self.server, "src/Logging.cpp", b"void log() { /* released ahead */ }\n")
            released = self.commit(self.server)
            self.api.tag_revisions["vNext"] = released
            self.api.releases = [{"id": 4, "tag_name": "vNext", "draft": False, "prerelease": False}]
            controller.run_once(self.config, self.api)
        event = self.journal()["events"]["sstd:" + released]
        self.assertEqual(event["base_revision"], self.server_revision)
        self.assertEqual(event["source_revision"], released)

    def test_unknown_release_ancestry_never_creates_a_task(self):
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
        intermediate = self.server_revision
        self.write(self.server, "src/Logging.cpp", b"void log() { /* later main */ }\n")
        self.server_revision = self.commit(self.server)
        self.api.tag_revisions["vB"] = intermediate
        self.api.releases = [{"id": 5, "tag_name": "vB", "draft": False, "prerelease": False}]
        saved = self.journal()
        original_api = self.api.api
        def invalid_comparison(endpoint, *args, **kwargs):
            return {} if "/compare/" in endpoint else original_api(endpoint, *args, **kwargs)
        with patch.object(self.api, "api", side_effect=invalid_comparison):
            with self.assertRaisesRegex(ValueError, "RELEASE_ANCESTRY_REQUIRES_REVIEW"):
                controller.run_once(self.config, self.api)
        self.assertEqual(self.journal(), saved)

    def test_issue_edit_creates_new_snapshot_and_pull_requests_are_ignored(self):
        self.api.issues = [self.issue(), {**self.issue(number=8), "pull_request": {}}]
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
            first = self.journal()
            self.api.issues[0]["body"] = "Edited acceptance criterion"
            controller.run_once(self.config, self.api)
        issue_events = [event for event in self.journal()["events"].values() if event["source_type"] == "SSTC_FEATURE"]
        self.assertEqual(len(issue_events), 2)
        self.assertNotEqual(issue_events[0]["task_id"], issue_events[1]["task_id"])
        for event in issue_events:
            request = self.config["state_directory"] / "tasks" / (event["task_id"] + ".request.md")
            self.assertEqual(hashlib.sha256(request.read_bytes()).hexdigest(), event["request_sha256"])
        original = next(event for event in first["events"].values() if event["source_type"] == "SSTC_FEATURE")
        self.assertIn("Requested internal parsing test", original["request"])

    def test_bad_issue_is_rejected_without_persisting_body_or_blocking_other_tasks(self):
        self.api.issues = [self.issue("xoxb-" + "S" * 24), self.issue("", number=8),
                           self.issue("X" * 65537, number=9), self.issue("Valid request", number=10)]
        with patch.object(codex_impact, "preflight"), self.fake_analysis("REQUIRED", "HIGH", ("ui_ux",)):
            result = controller.run_once(self.config, self.api, lambda pending: 0)
        self.assertEqual(list(result["outcomes"].values()).count("ANALYSIS_FAILED"), 3)
        self.assertEqual(list(result["outcomes"].values()).count("WAITING_APPROVAL"), 2)
        journal = self.journal()
        self.assertNotIn("S" * 24, json.dumps(journal))
        for event in journal["events"].values():
            if event.get("input_error"):
                request = self.config["state_directory"] / "tasks" / (event["task_id"] + ".request.md")
                self.assertFalse(request.exists())

    def test_reserved_task_recovers_after_journal_saved_before_task_transaction(self):
        state = self.config["state_directory"]
        state.mkdir()
        event = {"key": "sstd:" + self.server_revision, "source_type": "SSTD_CHANGE",
                 "source_reference": self.server_revision, "source_revision": self.server_revision,
                 "base_revision": self.server_base, "task_id": "sstd-sync-20261006-0001",
                 "sstc_revision": self.client_revision, "failures": 0, "active_seconds": 0}
        storage.atomic_json(state / "controller.json", {"schema_version": "1.0", "sstd_cursor": self.server_revision,
                                                        "events": {event["key"]: event}})
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
        task = state / "tasks" / (event["task_id"] + ".json")
        self.assertTrue(task.is_file())
        self.assertEqual(len(self.journal()["events"]), 1)
        storage.validate_pair(task, storage.load_json(task), storage.load_json(storage.companion(task, "log")))

    def test_entire_discovery_batch_survives_crash_before_first_new_task(self):
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
        releases = []
        for number in (1, 2):
            self.write(self.server, "src/Logging.cpp", f"void log() {{ /* release {number} */ }}\n".encode())
            revision = self.commit(self.server)
            self.api.tag_revisions[f"v{number}"] = revision
            releases.append({"id": number, "tag_name": f"v{number}", "draft": False, "prerelease": False})
        self.api.releases = releases
        self.api.issues = [self.issue(number=7), self.issue(number=8)]
        first_release = "sstd:" + self.api.tag_revisions["v1"]
        original = controller.materialize

        def interrupted(event, tasks):
            if event["key"] == first_release:
                raise KeyboardInterrupt
            return original(event, tasks)

        with patch.object(controller, "materialize", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                controller.run_once(self.config, self.api)
        reserved = self.journal()
        self.assertIn("sstd:" + self.api.tag_revisions["v2"], reserved["events"])
        self.assertEqual(reserved["release_ids"], [1, 2])
        self.assertEqual(len(reserved["events"]), 5)
        self.assertEqual(len({event["task_id"] for event in reserved["events"].values()}), 5)
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
        recovered = self.journal()["events"]
        self.assertEqual(set(recovered), set(reserved["events"]))
        for key, event in reserved["events"].items():
            for field in ("task_id", "source_reference", "sstc_revision"):
                self.assertEqual(recovered[key][field], event[field])
        for event in reserved["events"].values():
            task = self.config["state_directory"] / "tasks" / (event["task_id"] + ".json")
            storage.validate_pair(task, storage.load_json(task), storage.load_json(storage.companion(task, "log")))

    def test_historical_releases_baseline_and_draft_publication_is_new_input(self):
        self.api.releases = [{"id": 1, "tag_name": "v1", "draft": False, "prerelease": False},
                             {"id": 2, "tag_name": "v1", "draft": True, "prerelease": False}]
        with patch.object(controller, "drive", return_value="RECEIVED"):
            controller.run_once(self.config, self.api)
            self.assertEqual(self.journal()["release_ids"], [1])
            self.api.releases[1]["draft"] = False
            controller.run_once(self.config, self.api)
        self.assertEqual(self.journal()["release_ids"], [1, 2])
        self.assertEqual(len(self.journal()["events"]), 1)
        self.assertIn(SERVER + "/commits/v1", [endpoint for endpoint, _ in self.api.calls])

    def test_repeated_validation_transport_failures_terminate_without_restarting_codex(self):
        self.config["bootstrap_sstd_base"] = self.server_revision
        self.api.issues = [self.issue("Add internal decoder check")]
        with patch.object(codex_impact, "preflight"), self.fake_analysis():
            first = controller.run_once(self.config, self.api)
        task_id = next(iter(first["outcomes"]))
        path = self.config["state_directory"] / "tasks" / (task_id + ".json")
        controller.move(path, "VALIDATING", "Synthetic validated candidate queued")
        import sstc_pipeline
        with patch.object(sstc_pipeline, "check", side_effect=ValueError("temporary transport failure")), \
             patch.object(sstc_worker, "run") as invoke:
            for _ in range(4):
                controller.run_once(self.config, self.api)
        invoke.assert_not_called()
        event = next(iter(self.journal()["events"].values()))
        self.assertEqual(event["failures"], 3)
        self.assertEqual(storage.load_json(path)["status"], "SECURITY_REVIEW_FAILED")
        self.assertEqual(event["last_error"], "CONTROLLER_INPUT_EXECUTION_OR_CONTEXT_FAILED")
        self.assertEqual(storage.load_json(storage.companion(path, "log"))["stop_reason"], "CONTROLLER_FAILURE_LIMIT")
        self.assertEqual(storage.load_json(storage.checkpoint_path(path))["state"], storage.load_json(path))

    def test_analysis_environment_failure_preserves_attempt_and_terminates_on_limit(self):
        self.config["bootstrap_sstd_base"] = self.server_revision
        self.api.issues = [self.issue("Add internal decoder check")]
        statuses = []
        with patch.object(codex_impact, "run", return_value="EXECUTABLE_NOT_FOUND") as invoke:
            for _ in range(4):
                result = controller.run_once(self.config, self.api)
                task_id, status = next(iter(result["outcomes"].items()))
                statuses.append(status)
        self.assertEqual(statuses, ["ANALYZING", "ANALYZING", "ANALYSIS_FAILED", "ANALYSIS_FAILED"])
        self.assertEqual(invoke.call_count, 3)
        path = self.config["state_directory"] / "tasks" / (task_id + ".json")
        self.assertEqual(storage.load_json(path)["attempt"], 0)
        self.assertEqual(next(iter(self.journal()["events"].values()))["last_error"], "EXECUTABLE_NOT_FOUND")
        self.assertEqual(storage.load_json(storage.checkpoint_path(path))["state"]["status"], "ANALYSIS_FAILED")

    def test_repeated_implementation_failures_finish_task_and_preserve_candidate(self):
        self.config["bootstrap_sstd_base"] = self.server_revision
        self.api.issues = [self.issue("Add internal decoder check")]
        with patch.object(codex_impact, "preflight"), self.fake_analysis():
            result = controller.run_once(self.config, self.api)
        task_id = next(iter(result["outcomes"]))
        path = self.config["state_directory"] / "tasks" / (task_id + ".json")
        worktree = self.config["worktree_directory"] / task_id
        worktree.mkdir()
        candidate = worktree / "preserved.txt"
        candidate.write_bytes(b"Unpublished candidate\n")
        with patch.object(sstc_worker, "run", side_effect=OSError("temporary unavailable")) as invoke:
            for _ in range(4):
                controller.run_once(self.config, self.api)
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(storage.load_json(path)["status"], "IMPLEMENTATION_FAILED")
        self.assertEqual(candidate.read_bytes(), b"Unpublished candidate\n")
        self.assertEqual(storage.load_json(storage.checkpoint_path(path))["state"], storage.load_json(path))

    def test_real_source_collection_analysis_and_no_change_policy_complete(self):
        self.write(self.server, "src/Protocol.cpp", b'packet["cpu_usage_pct"] = cpu * 100.0;\n')
        self.server_revision = self.commit(self.server)
        with patch.object(codex_impact, "preflight"), self.fake_analysis("NOT_REQUIRED", "NONE"):
            result = controller.run_once(self.config, self.api)
        task_id, status = next(iter(result["outcomes"].items()))
        self.assertEqual(status, "COMPLETED")
        event = next(iter(self.journal()["events"].values()))
        manifest = storage.load_json(self.config["state_directory"] / "tasks" / (task_id + ".inputs/manifest.json"))
        self.assertEqual(manifest["input_context"]["sstd_base_revision"], self.server_base)
        self.assertEqual(manifest["input_context"]["sstd_revision"], self.server_revision)
        self.assertEqual(manifest["input_context"]["sstc_revision"], self.client_revision)
        self.assertEqual(event["context_coverage"], "ALL_PINNED_ANDROID_SOURCE_CONSUMERS")

    def test_real_source_high_risk_waits_for_one_bounded_slack_listener(self):
        self.api.issues = [self.issue()]
        pending = []
        with patch.object(codex_impact, "preflight"), self.fake_analysis("REQUIRED", "HIGH", ("ui_ux",)), \
             patch.object(controller, "drive", wraps=controller.drive):
            result = controller.run_once(self.config, self.api, lambda paths: pending.extend(paths) or 0)
        self.assertEqual(set(result["outcomes"].values()), {"WAITING_APPROVAL"})
        self.assertEqual(len(pending), 2)
        for path in pending:
            checkpoint = storage.load_json(storage.checkpoint_path(path))
            self.assertEqual(checkpoint["state"]["status"], "WAITING_APPROVAL")
        before = {path: path.read_bytes() for task in pending for path in
                  (task, storage.companion(task, "log"), storage.checkpoint_path(task))}
        controller.run_once(self.config, self.api, lambda paths: 0)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_undetermined_source_analysis_never_launches_worker_or_actions(self):
        with patch.object(codex_impact, "preflight"), self.fake_analysis("UNDETERMINED", "LOW"):
            result = controller.run_once(self.config, self.api)
        self.assertEqual(set(result["outcomes"].values()), {"ANALYSIS_FAILED"})
        self.assertTrue(all(payload is None for endpoint, payload in self.api.calls))

    def test_existing_slack_wait_is_polled_before_any_heavy_stage(self):
        with patch.object(codex_impact, "preflight"), self.fake_analysis("REQUIRED", "HIGH"):
            controller.run_once(self.config, self.api, lambda paths: 0)
        self.api.issues = [self.issue("A newly arrived request")]
        ordering = []
        def approval(paths):
            ordering.append(("approval", list(paths)))
            return 0
        def drive(path, event, config, client):
            ordering.append(("drive", path))
            return storage.load_json(path)["status"]
        with patch.object(controller, "drive", side_effect=drive):
            controller.run_once(self.config, self.api, approval)
        self.assertEqual(ordering[0][0], "approval")
        self.assertEqual(len(ordering[0][1]), 1)
        self.assertEqual([item[0] for item in ordering], ["approval", "drive", "drive"])

    def test_issue_low_policy_runs_existing_session_and_repeated_poll_finishes_draft(self):
        self.config["bootstrap_sstd_base"] = self.server_revision
        self.api.issues = [self.issue("Add an internal decoder regression test")]
        with patch.object(codex_impact, "preflight"), self.fake_analysis("REQUIRED", "LOW"):
            result = controller.run_once(self.config, self.api)
        task_id, status = next(iter(result["outcomes"].items()))
        self.assertEqual(status, "IMPLEMENTING")
        self.finish_draft(task_id)

    def test_sstd_protocol_approval_resumes_original_task_to_actions_and_draft(self):
        with patch.object(codex_impact, "preflight"), self.fake_analysis("REQUIRED", "HIGH"):
            result = controller.run_once(self.config, self.api, lambda paths: 0)
        task_id, status = next(iter(result["outcomes"].items()))
        self.assertEqual(status, "WAITING_APPROVAL")
        task = self.config["state_directory"] / "tasks" / (task_id + ".json")
        frozen = storage.checkpoint_path(task).read_bytes()
        request = slack_approval.prepare_request(task, "T1", "C1", "A1")
        slack_approval.bind_message(task, request["nonce"], "123.456")
        payload = {"type": "block_actions", "team": {"id": "T1"}, "api_app_id": "A1",
                   "user": {"id": "U1"}, "channel": {"id": "C1"},
                   "container": {"message_ts": "123.456"},
                   "actions": [{"action_id": "ax_approve", "value": request["nonce"]}]}
        self.assertEqual(slack_approval.apply_decision(task, payload, team_id="T1", channel_id="C1",
                                                      app_id="A1", approver_ids={"U1"}), "IMPLEMENTING")
        self.finish_draft(task_id)
        self.assertEqual(storage.checkpoint_path(task).read_bytes(), frozen)

    def test_approved_high_worker_waits_for_observed_reset_then_resumes_same_session(self):
        with patch.object(codex_impact, "preflight"), self.fake_analysis("REQUIRED", "HIGH"):
            result = controller.run_once(self.config, self.api, lambda paths: 0)
        task_id = next(iter(result["outcomes"]))
        task = self.config["state_directory"] / "tasks" / (task_id + ".json")
        request = slack_approval.prepare_request(task, "T1", "C1", "A1")
        slack_approval.bind_message(task, request["nonce"], "123.456")
        payload = {"type": "block_actions", "team": {"id": "T1"}, "api_app_id": "A1",
                   "user": {"id": "U1"}, "channel": {"id": "C1"},
                   "container": {"message_ts": "123.456"},
                   "actions": [{"action_id": "ax_approve", "value": request["nonce"]}]}
        slack_approval.apply_decision(task, payload, team_id="T1", channel_id="C1",
                                     app_id="A1", approver_ids={"U1"})
        with patch.object(sstc_worker, "invoke", return_value=(1, b"", "RATE_LIMIT")):
            self.assertEqual(controller.run_once(self.config, self.api)["outcomes"][task_id], "DEFERRED_RATE_LIMIT")
        frozen = {path: path.read_bytes() for path in (task, storage.companion(task, "log"),
                                                     storage.checkpoint_path(task))}
        with patch.object(sstc_worker, "invoke") as invoked:
            self.assertEqual(controller.run_once(self.config, self.api)["outcomes"][task_id], "DEFERRED_RATE_LIMIT")
        invoked.assert_not_called()
        for path, raw in frozen.items():
            self.assertEqual(path.read_bytes(), raw)
        from update_task_state import update
        args = SimpleNamespace(task_file=task, recover=False, record_reset_at="2000-01-01T00:00:00Z",
                               deferred_until=None)
        with storage.task_lock(task):
            self.assertEqual(update(args), 0)
        self.finish_draft(task_id)
        self.assertEqual(len(storage.load_json(storage.companion(task, "log"))["approvals"]), 1)

    def finish_draft(self, task_id):
        path = self.config["state_directory"] / "tasks" / (task_id + ".json")
        owner = SimpleNamespace(path=path, revision=self.client_revision,
                                branch="ax/sstc-sync/" + task_id,
                                base_tree=self.git(self.client, "rev-parse", self.client_revision + "^{tree}"),
                                tree=None)
        remote = publication_fixture.FakeGitHub(owner)
        remote.receipt_change = lambda receipt: receipt.update(workflow_sha=self.client_revision)
        original_api = remote.api
        def api(endpoint, payload=None, *, binary=False):
            if endpoint in {SERVER + "/commits/main", SERVER + "/releases?per_page=100",
                            CLIENT + "/issues?state=open&per_page=100", CLIENT + "/commits/main"}:
                return self.api.api(endpoint, payload, binary=binary)
            response = original_api(endpoint, payload, binary=binary)
            if endpoint == CLIENT + "/actions/runs/117":
                response["head_sha"] = self.client_revision
            if endpoint.startswith(CLIENT + "/actions/runs/117/artifacts?"):
                response["artifacts"][0]["workflow_run"]["head_sha"] = self.client_revision
            return response
        remote.api = api
        invoked = []
        def implement(executable, session, prompt, cwd, **kwargs):
            invoked.append(session)
            self.assertEqual(session, SESSION)
            self.write(cwd, "app/src/test/java/example/AddedTest.kt", b"class AddedTest\n")
            files = sstc_candidate.candidate_snapshot(cwd, self.client_revision, owner.branch)
            owner.tree, _ = sstc_candidate.tree_payload(cwd, self.client_revision, owner.branch, files)
            return 0, (json.dumps({"type": "thread.started", "thread_id": session}) +
                       '\n{"type":"turn.completed"}\n').encode(), None
        with patch.object(sstc_worker, "invoke", side_effect=implement):
            self.assertEqual(controller.run_once(self.config, remote)["outcomes"][task_id], "VALIDATING")
        frozen = path.read_bytes()
        self.assertEqual(controller.run_once(self.config, remote)["outcomes"][task_id], "VALIDATING")
        self.assertEqual(path.read_bytes(), frozen)
        remote.run_status = "completed"
        self.assertEqual(controller.run_once(self.config, remote)["outcomes"][task_id], "READY_FOR_REVIEW")
        self.assertEqual(invoked, [SESSION])
        self.assertEqual(len(self.journal()["events"]), 1)
        self.assertTrue(remote.pull["draft"])
        self.assertEqual(sum(endpoint == CLIENT + "/actions/workflows/sstc-validation.yml/dispatches"
                             for endpoint, _, _ in remote.calls), 1)
        self.assertEqual(storage.load_json(path)["draft_pr_url"], remote.pull["html_url"])

    def test_missing_rules_and_oversized_inputs_cannot_complete_task(self):
        self.git(self.client, "rm", "AGENTS.md")
        self.client_revision = self.commit(self.client)
        with patch.object(codex_impact, "invoke") as invoked:
            result = controller.run_once(self.config, self.api)
        self.assertEqual(set(result["outcomes"].values()), {"ANALYSIS_FAILED"})
        invoked.assert_not_called()

    def test_configuration_requires_explicit_base_and_never_accepts_inline_token(self):
        config = {key: str(value) if isinstance(value, Path) else value for key, value in self.config.items()}
        path = self.root / "config.json"
        del config["bootstrap_sstd_base"]
        storage.atomic_json(path, config)
        with self.assertRaises(ValueError):
            controller.configuration(path)
        config["bootstrap_sstd_base"] = self.server_base
        config["GH_TOKEN"] = "private credential value"
        storage.atomic_json(path, config)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(run_controller.main(["--config", str(path), "--once"]), 2)
        self.assertNotIn("private credential", output.getvalue())


if __name__ == "__main__":
    unittest.main()
