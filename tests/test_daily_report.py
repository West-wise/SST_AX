"""Calendar boundaries, proof bindings and journalled no-duplicate delivery."""
import copy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_report as daily
import task_storage as storage
import test_codex_impact as analysis_fixture
import test_sstc_pipeline as pipeline_fixture

DAY = date(2026, 10, 8)
CONFIG = {"SLACK_TEAM_ID": "TWORKSPACE", "SLACK_DAILY_REPORT_CHANNEL_ID": "CREPORT",
          "SLACK_BOT_TOKEN": "xoxb-test-placeholder"}


class FakeSlack:
    def __init__(self, response=None, error=None, team="TWORKSPACE"):
        self.calls, self.error, self.team = [], error, team
        self.response = response or {"ok": True, "channel": "CREPORT", "ts": "1791460800.123456"}

    def auth_test(self):
        return {"ok": True, "team_id": self.team}

    def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class DailyReportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.reports = self.root / "reports"

    def task(self, number=1, created="2026-10-07T15:00:00Z", status="RECEIVED", updated=None, commands=None):
        ident = f"sstc-feature-20261008-{number:04d}"
        path = self.tasks / (ident + ".json")
        updated = updated or created
        state = {"task_id": ident, "source_type": "SSTC_FEATURE", "source_reference": "untrusted <!channel> xoxb-private /home/private",
                 "risk_level": "LOW", "status": status, "created_at": created, "updated_at": updated, "attempt": 0}
        transitions = [{"from_status": None, "to_status": "RECEIVED", "occurred_at": created, "reason": "private text"}]
        if status != "RECEIVED":
            transitions.append({"from_status": "RECEIVED", "to_status": status, "occurred_at": updated, "reason": "private text"})
        log = {key: state[key] for key in ("task_id", "source_type", "source_reference", "status")}
        log.update(state_transitions=transitions, commands=commands or [], raw_secret="xoxb-never-publish",
                   request="<!everyone> https://evil.invalid/private")
        storage.atomic_json(path, state)
        storage.atomic_json(storage.companion(path, "log"), log)
        return path

    def report(self):
        return daily.build_report(self.tasks, DAY)

    def test_korean_day_half_open_utc_offsets_and09_due_time(self):
        self.task(1, "2026-10-07T14:59:59Z")
        self.task(2, "2026-10-07T15:00:00Z")
        self.task(3, "2026-10-08T14:59:59Z")
        self.task(4, "2026-10-08T15:00:00Z")
        self.task(5, "2026-10-08T00:00:00+09:00")
        self.task(6, "2026-10-08T15:00:00Z", "WAITING_APPROVAL")
        report = self.report()
        self.assertEqual([item["task_id"][-4:] for item in report["items"]], ["0002", "0003", "0005"])
        self.assertIsNone(daily.due_date(datetime(2026, 10, 9, 8, 59, 59, tzinfo=daily.KST)))
        self.assertEqual(daily.due_date(datetime(2026, 10, 9, 0, tzinfo=timezone.utc)), DAY)
        with self.assertRaisesRegex(daily.ReportError, "INVALID_REPORT_CLOCK"):
            daily.due_date(datetime(2026, 10, 9, 9))

    def test_transition_command_activity_and_prior_backlog_are_distinct(self):
        self.task(1, "2026-10-05T00:00:00Z", "WAITING_APPROVAL")
        self.task(2, "2026-10-05T00:00:00Z", "ANALYZING", "2026-10-08T01:00:00Z")
        self.task(3, "2026-10-05T00:00:00Z", "DEFERRED_RATE_LIMIT")
        self.task(4, "2026-10-05T00:00:00Z", "ANALYSIS_FAILED")
        self.task(5, "2026-10-05T00:00:00Z", commands=[{"started_at": "2026-10-08T01:00:00Z",
                     "finished_at": "2026-10-08T01:00:01Z", "exit_code": 0, "command": "private command"}])
        report = self.report()
        self.assertEqual(report["counts"]["daily_tasks"], 2)
        self.assertEqual(report["counts"]["backlog_tasks"], 3)
        self.assertEqual(report["items"][0]["activity"], [])
        self.assertEqual(report["items"][1]["activity"], ["TRANSITION"])
        self.assertEqual(report["items"][4]["activity"], ["COMMAND"])
        self.assertIn("이전부터 대기/실패", daily.render_report(report)["text"])

    def test_projection_never_copies_secret_paths_requests_mentions_or_raw_urls(self):
        self.task(status="READY_FOR_REVIEW")
        output = json.dumps(daily.render_report(self.report()), ensure_ascii=False)
        for forbidden in ("xoxb-", "private", "/home", "<!", "evil.invalid"):
            self.assertNotIn(forbidden, output)
        self.assertIn("미검증", output)
        self.assertEqual(self.report()["counts"]["draft_pr_verified"], 0)

    def test_invalid_dates_duplicate_json_and_symlink_are_safe_record_errors(self):
        path = self.task(created="2026-10-08T00:00:00")
        self.task(2).write_text('{"task_id":"one", "task_id":"xoxb-secret"}')
        link = self.tasks / "sstc-feature-20261008-0003.json"
        link.symlink_to(path)
        report = self.report()
        self.assertEqual(report["counts"]["invalid_records"], 3)
        self.assertTrue(all(item["error"] == "INVALID_TASK_RECORD" for item in report["items"]))
        self.assertNotIn("xoxb", json.dumps(report))

    def test_report_rejects_mentions_host_changes_duplicate_ids_counts_and_size(self):
        self.task()
        base = self.report()
        edits = []
        mutated = copy.deepcopy(base)
        mutated["items"][0]["task_id"] = "<!channel>"
        edits.append(mutated)
        mutated = copy.deepcopy(base)
        mutated["items"][0]["draft_pr_url"] = "https://github.com.evil.invalid/West-wise/Server_State_Telemetry_Client/pull/5"
        edits.append(mutated)
        mutated = copy.deepcopy(base)
        mutated["items"].append(mutated["items"][0])
        edits.append(mutated)
        mutated = copy.deepcopy(base)
        mutated["counts"]["analysis_valid"] = True
        edits.append(mutated)
        mutated = copy.deepcopy(base)
        mutated["items"][0]["risk_level"] = {"secret": "xoxb-private"}
        edits.append(mutated)
        for mutated in edits:
            with self.subTest(mutated=mutated):
                with self.assertRaises(daily.ReportError):
                    daily.validate_report(mutated)
        for number in range(2, daily.MAX_TASKS + 2):
            self.task(number)
        with self.assertRaisesRegex(daily.ReportError, "REPORT_SIZE_LIMIT"):
            self.report()

    def test_empty_day_sent_once_and_sent_snapshot_survives_current_changes(self):
        report = self.report()
        web = FakeSlack()
        self.assertIn("변경 및 대기 사항 없음", daily.render_report(report)["text"])
        self.assertEqual(daily.send_report(report, self.reports, CONFIG, web), "SENT")
        frozen = (self.reports / "2026-10-08.snapshot.json").read_bytes()
        self.task()
        self.assertEqual(daily.send_report(self.report(), self.reports, CONFIG, web), "ALREADY_SENT")
        self.assertEqual(len(web.calls), 1)
        self.assertEqual((self.reports / "2026-10-08.snapshot.json").read_bytes(), frozen)
        changed_config = {**CONFIG, "SLACK_DAILY_REPORT_CHANNEL_ID": "COTHER"}
        with self.assertRaisesRegex(daily.ReportError, "REPORT_DESTINATION_OR_SNAPSHOT_CHANGED"):
            daily.send_report(report, self.reports, changed_config, web)
        persisted = "".join(path.read_text() for path in self.reports.glob("*.json"))
        self.assertNotIn(CONFIG["SLACK_BOT_TOKEN"], persisted)
        self.assertEqual(web.calls[0]["parse"], "none")
        self.assertFalse(web.calls[0]["link_names"])

    def test_lost_reply_is_uncertain_without_retries_or_raw_exception(self):
        web = FakeSlack(error=RuntimeError("xoxb-secret hidden error https://evil.invalid"))
        report = self.report()
        self.assertEqual(daily.send_report(report, self.reports, CONFIG, web), "UNCERTAIN")
        self.assertEqual(daily.send_report(report, self.reports, CONFIG, web), "UNCERTAIN")
        self.assertEqual(len(web.calls), 1)
        self.assertNotIn("xoxb-secret", (self.reports / "2026-10-08.delivery.json").read_text())

    def test_crash_after_sending_journal_is_never_automatically_repeated(self):
        report = self.report()
        web = FakeSlack()
        def crash(**kwargs):
            raise KeyboardInterrupt()
        web.chat_postMessage = crash
        with self.assertRaises(KeyboardInterrupt):
            daily.send_report(report, self.reports, CONFIG, web)
        healthy = FakeSlack()
        self.assertEqual(daily.send_report(report, self.reports, CONFIG, healthy), "UNCERTAIN")
        self.assertEqual(healthy.calls, [])

    def test_explicit_not_sent_retries_only_identical_snapshot_up_to_three(self):
        report = self.report()
        web = FakeSlack(response={"ok": False, "error": "ratelimited"})
        for _ in range(3):
            self.assertEqual(daily.send_report(report, self.reports, CONFIG, web), "NOT_SENT")
        self.assertEqual(daily.send_report(report, self.reports, CONFIG, web), "RETRY_LIMIT")
        self.assertEqual(len(web.calls), 3)
        self.task()
        with self.assertRaisesRegex(daily.ReportError, "REPORT_SNAPSHOT_CHANGED"):
            daily.send_report(self.report(), self.reports, CONFIG, web)

    def test_wrong_workspace_blocks_post_wrong_channel_or_timestamp_is_uncertain(self):
        report = self.report()
        web = FakeSlack(team="TOTHER")
        self.assertEqual(daily.send_report(report, self.reports, CONFIG, web), "AUTH_CHECK_FAILED")
        self.assertEqual(web.calls, [])
        for response in ({"ok": True, "channel": "COTHER", "ts": "1791460800.123456"},
                         {"ok": True, "channel": "CREPORT", "ts": "<!channel>"}):
            with self.subTest(response=response):
                directory = self.root / ("attempt-" + str(len(str(response))))
                fresh = FakeSlack(response=response)
                self.assertEqual(daily.send_report(report, directory, CONFIG, fresh), "UNCERTAIN")
                self.assertEqual(daily.send_report(report, directory, CONFIG, fresh), "UNCERTAIN")
                self.assertEqual(len(fresh.calls), 1)

    def test_serialized_workers_post_only_once(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        report, web = self.report(), FakeSlack()
        barrier = threading.Barrier(2)
        def invoke():
            barrier.wait()
            return daily.send_report(report, self.reports, CONFIG, web)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: invoke(), range(2)))
        self.assertEqual(sorted(results), ["ALREADY_SENT", "SENT"])
        self.assertEqual(len(web.calls), 1)

    def test_real_analysis_revalidation_detects_changed_result_preserves_task_bytes(self):
        fixture = analysis_fixture.CodexImpactTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.assertEqual(fixture.execute(), "VALID")
        day = datetime.fromisoformat(fixture.pair()[0]["created_at"].replace("Z", "+00:00")).astimezone(daily.KST).date()
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.path.parent.glob("*.json")}
        report = daily.build_report(fixture.path.parent, day)
        self.assertEqual(report["items"][0]["validation"]["analysis"], "VALID")
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})
        record = fixture.pair()[1]["codex_analysis"]
        output = fixture.path.parent / record["result_file"]
        result = storage.load_json(output)
        result["summary"] = "changed summary"
        storage.atomic_json(output, result)
        report = daily.build_report(fixture.path.parent, day)
        self.assertEqual(report["items"][0]["validation"]["analysis"], "INVALID")
        self.assertEqual(report["items"][0]["error"], "INVALID_ANALYSIS_PROOF")

    def test_actual_pipeline_producer_receipts_and_tampered_candidate_binding(self):
        fixture = pipeline_fixture.SstcPipelineTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        # The older fixture omits inputs_directory; retain its bundle under the
        # standard discovery name used by validated_analysis's legacy fallback.
        retained_inputs = fixture.path.parent / (fixture.path.stem + ".inputs")
        fixture.inputs.rename(retained_inputs)
        fixture.inputs = retained_inputs
        fixture.publish()
        fixture.client.run_status = "completed"
        self.assertEqual(pipeline_fixture.pipeline.check(fixture.path, fixture.client)["outcome"], "READY_FOR_REVIEW")
        day = datetime.fromisoformat(fixture.pair()[0]["created_at"].replace("Z", "+00:00")).astimezone(daily.KST).date()
        report = daily.build_report(fixture.path.parent, day)
        item = report["items"][0]
        self.assertEqual(item["validation"], {"analysis": "VALID", "actions": "RECEIPT_VERIFIED", "draft_pr": "RECEIPT_VERIFIED"})
        self.assertEqual(item["draft_pr_url"], "https://github.com/West-wise/Server_State_Telemetry_Client/pull/9")
        path = storage.companion(fixture.path, "github-validation")
        sensor = storage.load_json(path)
        sensor["receipt"]["candidate_sha"] = "f" * 40
        storage.atomic_json(path, sensor)
        item = daily.build_report(fixture.path.parent, day)["items"][0]
        self.assertEqual(item["validation"]["actions"], "INVALID")
        self.assertIsNone(item["candidate_sha"])
        self.assertIsNone(item["draft_pr_url"])


if __name__ == "__main__":
    unittest.main()
