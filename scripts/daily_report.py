"""Read-only Korean daily summaries and conservative, journalled Slack delivery."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

from codex_impact import digest as analysis_digest, regular
from execution_policy import validated_analysis
from github_validation import REPOSITORY, digest, snapshot, validate_receipt
from impact_validation import load_document
from task_storage import atomic_json, companion, validate_pair
from update_task_state import utc_now
from validate_task_state import DEFAULT_SCHEMA_PATH

KST = ZoneInfo("Asia/Seoul")
MAX_TASKS, MAX_SCAN = 40, 10_000
TASK_ID = re.compile(r"(sstd-sync|sstc-feature)-[0-9]{8}-[0-9]{4}")
SHA = re.compile(r"[0-9a-f]{40}")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
STATUSES = set(load_document(DEFAULT_SCHEMA_PATH)["properties"]["status"]["enum"])
FAILURES = {"ANALYSIS_FAILED", "IMPLEMENTATION_FAILED", "BUILD_FAILED", "TEST_FAILED",
            "SECURITY_REVIEW_FAILED", "PROTOCOL_APPROVAL_REQUIRED"}
BACKLOG = FAILURES | {"WAITING_APPROVAL", "DEFERRED_RATE_LIMIT"}
ERRORS = {None, "INVALID_TASK_RECORD", "INVALID_ANALYSIS_PROOF", "INVALID_PIPELINE_PROOF"}
STATUS_LABELS = {
    "RECEIVED": "접수", "ANALYZING": "분석 중", "WAITING_APPROVAL": "승인 대기",
    "IMPLEMENTING": "구현 중", "VALIDATING": "검증 중", "READY_FOR_REVIEW": "Draft PR 검토 대기",
    "COMPLETED": "정책 완료", "REJECTED": "거절", "DEFERRED_RATE_LIMIT": "사용량 제한 대기",
    "ANALYSIS_FAILED": "분석 실패", "IMPLEMENTATION_FAILED": "구현 실패", "BUILD_FAILED": "빌드 실패",
    "TEST_FAILED": "테스트 실패", "SECURITY_REVIEW_FAILED": "보안 검토 실패",
    "PROTOCOL_APPROVAL_REQUIRED": "SSTD 계약 결정 대기", "UNKNOWN": "기록 확인 필요",
}


class ReportError(ValueError):
    """Only fixed error codes leave this module; never SDK/log text."""


def _require(condition, code="INVALID_REPORT"):
    if not condition:
        raise ReportError(code)


def _instant(value):
    _require(isinstance(value, str) and TIMESTAMP.fullmatch(value), "INVALID_TASK_RECORD")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ReportError("INVALID_TASK_RECORD") from None
    _require(result.utcoffset() is not None, "INVALID_TASK_RECORD")
    return result


def _date(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ReportError("INVALID_REPORT") from None


def due_date(now: datetime) -> date | None:
    """The complete previous Korean calendar day becomes due at 09:00 KST."""
    _require(isinstance(now, datetime) and now.utcoffset() is not None, "INVALID_REPORT_CLOCK")
    local = now.astimezone(KST)
    return local.date() - timedelta(days=1) if local.time() >= time(9) else None


def _read(path):
    regular(path)
    return load_document(path)


def _pair(path):
    _require(not companion(path, "pending").exists(), "INVALID_TASK_RECORD")
    state, log = _read(path), _read(companion(path, "log"))
    validate_pair(path, state, log)
    created, updated = _instant(state["created_at"]), _instant(state["updated_at"])
    _require(created <= updated, "INVALID_TASK_RECORD")
    history, previous, last = log["state_transitions"], None, created
    for index, entry in enumerate(history):
        _require(isinstance(entry, dict), "INVALID_TASK_RECORD")
        occurred = _instant(entry.get("occurred_at"))
        _require(entry.get("from_status") == previous and entry.get("to_status") in STATUSES and
                 created <= last <= occurred <= updated, "INVALID_TASK_RECORD")
        if index == 0:
            _require(entry["to_status"] == "RECEIVED" and occurred == created, "INVALID_TASK_RECORD")
        previous, last = entry["to_status"], occurred
    commands = log.get("commands")
    _require(isinstance(commands, list), "INVALID_TASK_RECORD")
    for entry in commands:
        _require(isinstance(entry, dict), "INVALID_TASK_RECORD")
        started = _instant(entry["started_at"]) if entry.get("started_at") is not None else None
        finished = _instant(entry["finished_at"]) if entry.get("finished_at") is not None else None
        _require(not finished or started and started <= finished, "INVALID_TASK_RECORD")
        _require(not started or started >= created, "INVALID_TASK_RECORD")
        _require(entry.get("exit_code") is None or type(entry["exit_code"]) is int, "INVALID_TASK_RECORD")
    _require(state["source_type"] == ("SSTD_CHANGE" if path.stem.startswith("sstd-sync-") else "SSTC_FEATURE"),
             "INVALID_TASK_RECORD")
    return state, log


def _activity(state, log, start, end):
    flags = []
    if start <= _instant(state["created_at"]) < end:
        flags.append("CREATED")
    if any(start <= _instant(entry["occurred_at"]) < end for entry in log["state_transitions"][1:]):
        flags.append("TRANSITION")
    if any(start <= _instant(entry[key]) < end for entry in log["commands"]
           for key in ("started_at", "finished_at") if entry.get(key) is not None):
        flags.append("COMMAND")
    return flags


def _proofs(path, state, log):
    validation = {"analysis": "UNVERIFIED", "actions": "UNVERIFIED", "draft_pr": "UNVERIFIED"}
    candidate, url, error = None, None, None
    ctx = None
    if isinstance(log.get("codex_analysis"), dict) and log["codex_analysis"].get("outcome") == "VALID":
        try:
            ctx = validated_analysis(path)
            _require(ctx["state"] == state and ctx["log"] == log)
            validation["analysis"] = "VALID"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            validation["analysis"], error = "INVALID", "INVALID_ANALYSIS_PROOF"
    pipeline_path = companion(path, "pipeline")
    if not pipeline_path.exists():
        return validation, candidate, url, error
    try:
        pipeline = _read(pipeline_path)
        _require(ctx is not None and pipeline.get("schema_version") == "1.0" and
                 pipeline.get("task_id") == path.stem and pipeline.get("repository") == REPOSITORY and
                 pipeline.get("analysis_record") == ctx["record"] and
                 pipeline.get("session_id") == ctx["session_id"] and
                 pipeline.get("source_revision") == ctx["manifest"]["input_context"]["sstc_revision"])
        proposed = pipeline.get("candidate_sha")
        if proposed is None:
            return validation, candidate, url, error
        _require(isinstance(proposed, str) and SHA.fullmatch(proposed))
        authority = _read(companion(path, "execution-authority"))
        _require(analysis_digest(authority) == pipeline.get("authority_sha256") and
                 authority.get("task_id") == path.stem and authority.get("session_id") == ctx["session_id"] and
                 authority.get("manifest_sha256") == analysis_digest(ctx["manifest"]) and
                 authority.get("result_sha256") == analysis_digest(ctx["result"]) and
                 authority.get("analysis_record") == ctx["record"])
        current = snapshot(path)
        expected = pipeline.get("final_snapshot") if pipeline.get("outcome") in {
            "READY_FOR_REVIEW", "BUILD_FAILED", "IMPLEMENTATION_FAILED", "FINALIZING"} else pipeline.get("task_snapshot")
        _require(current == expected)
        sensor_path = companion(path, "github-validation")
        if not sensor_path.exists():
            return validation, candidate, url, error
        sensor = _read(sensor_path)
        _require(sensor.get("task_id") == path.stem and sensor.get("repository") == REPOSITORY and
                 sensor.get("candidate_sha") == proposed and sensor.get("task_snapshot") == pipeline.get("task_snapshot") and
                 sensor.get("run_id") == pipeline.get("run_id"))
        if sensor.get("outcome") != "VALIDATED":
            return validation, candidate, url, error
        validate_receipt(sensor.get("receipt"), sensor)
        _require(log.get("sstc_candidate") == {key: pipeline[key] for key in (
            "repository", "source_revision", "candidate_sha", "tree_sha", "branch", "candidate_files")})
        validation["actions"], candidate = "RECEIPT_VERIFIED", proposed
        if state["status"] == "READY_FOR_REVIEW":
            url = state.get("draft_pr_url")
            _require(_pr_url(url) and pipeline.get("outcome") == "READY_FOR_REVIEW" and pipeline.get("draft_pr_url") == url)
            number = int(url.rsplit("/", 1)[1])
            _require(log.get("draft_pr") == {"number": number, "url": url, "candidate_sha": proposed,
                "run_id": sensor["run_id"], "draft": True})
            command = log["commands"][-1]
            _require(command.get("name") == "sstc-pipeline-check" and type(command.get("exit_code")) is int and
                     command["exit_code"] == 0 and command.get("finished_at"))
            validation["draft_pr"] = "RECEIPT_VERIFIED"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        validation["actions"] = "INVALID"
        validation["draft_pr"] = "INVALID" if state["status"] == "READY_FOR_REVIEW" else "UNVERIFIED"
        candidate, url, error = None, None, "INVALID_PIPELINE_PROOF"
    return validation, candidate, url, error


def _pr_url(value):
    return isinstance(value, str) and re.fullmatch(r"https://github\.com/" + re.escape(REPOSITORY) + r"/pull/[1-9][0-9]*", value)


def _invalid(task_id):
    return {"task_id": task_id, "source_type": "SSTD_CHANGE" if task_id.startswith("sstd-sync-") else "SSTC_FEATURE",
            "status": "UNKNOWN", "risk_level": "UNKNOWN", "activity": [], "backlog": "NONE",
            "validation": {"analysis": "UNVERIFIED", "actions": "UNVERIFIED", "draft_pr": "UNVERIFIED"},
            "candidate_sha": None, "draft_pr_url": None, "error": "INVALID_TASK_RECORD"}


def _counts(items):
    return {"daily_tasks": sum(bool(item["activity"]) for item in items),
            "backlog_tasks": sum(item["backlog"] != "NONE" for item in items),
            "invalid_records": sum(item["error"] is not None for item in items),
            "analysis_valid": sum(bool(item["activity"]) and item["validation"]["analysis"] == "VALID" for item in items),
            "actions_verified": sum(bool(item["activity"]) and item["validation"]["actions"] == "RECEIPT_VERIFIED" for item in items),
            "draft_pr_verified": sum(bool(item["activity"]) and item["validation"]["draft_pr"] == "RECEIPT_VERIFIED" for item in items)}


def build_report(tasks_directory: Path, report_date: date) -> dict:
    """Aggregate recorded activities in [00:00, next 00:00) KST without Task writes."""
    _require(type(report_date) is date, "INVALID_REPORT_DATE")
    _require(tasks_directory.is_dir() and not tasks_directory.is_symlink(), "TASK_DIRECTORY_REQUIRED")
    start = datetime.combine(report_date, time(), KST)
    end = start + timedelta(days=1)
    items, scanned = [], 0
    for path in sorted(tasks_directory.glob("*.json")):
        if not TASK_ID.fullmatch(path.stem):
            continue
        scanned += 1
        _require(scanned <= MAX_SCAN, "REPORT_SCAN_LIMIT")
        try:
            state, log = _pair(path)
            if _instant(state["created_at"]) >= end:
                continue
            activity = _activity(state, log, start, end)
            backlog = ("FAILED" if state["status"] in FAILURES else state["status"]) if state["status"] in BACKLOG else "NONE"
            if not activity and backlog == "NONE":
                continue
            validation, candidate, url, error = _proofs(path, state, log)
            _require(_pair(path) == (state, log), "INVALID_TASK_RECORD")
            item = {"task_id": path.stem, "source_type": state["source_type"], "status": state["status"],
                    "risk_level": state["risk_level"], "activity": activity, "backlog": backlog,
                    "validation": validation, "candidate_sha": candidate, "draft_pr_url": url, "error": error}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            item = _invalid(path.stem)
        items.append(item)
        _require(len(items) <= MAX_TASKS, "REPORT_SIZE_LIMIT")
    report = {"schema_version": "1.0", "report_date": report_date.isoformat(), "timezone": "Asia/Seoul",
              "period_start": start.isoformat(), "period_end": end.isoformat(),
              "source_types": {kind: sum(bool(item["activity"]) and item["source_type"] == kind for item in items)
                               for kind in ("SSTD_CHANGE", "SSTC_FEATURE")}, "counts": _counts(items), "items": items}
    validate_report(report)
    return report


def _validate_report(report: dict) -> None:
    """Closed fields, safe projections, date boundaries and count/proof consistency."""
    _require(isinstance(report, dict) and set(report) == {"schema_version", "report_date", "timezone", "period_start",
                                                      "period_end", "source_types", "counts", "items"})
    day = _date(report["report_date"])
    start = datetime.combine(day, time(), KST)
    _require(report["schema_version"] == "1.0" and report["timezone"] == "Asia/Seoul" and
             report["period_start"] == start.isoformat() and report["period_end"] == (start + timedelta(days=1)).isoformat())
    items, seen = report["items"], set()
    _require(isinstance(items, list) and len(items) <= MAX_TASKS, "REPORT_SIZE_LIMIT")
    for item in items:
        _require(isinstance(item, dict) and set(item) == {"task_id", "source_type", "status", "risk_level", "activity",
            "backlog", "validation", "candidate_sha", "draft_pr_url", "error"})
        ident = item["task_id"]
        _require(isinstance(ident, str) and TASK_ID.fullmatch(ident) and ident not in seen)
        seen.add(ident)
        _require(item["source_type"] == ("SSTD_CHANGE" if ident.startswith("sstd-sync-") else "SSTC_FEATURE") and
                 item["status"] in STATUSES | {"UNKNOWN"} and item["risk_level"] in {"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"})
        _require(isinstance(item["activity"], list) and item["activity"] == [name for name in ("CREATED", "TRANSITION", "COMMAND") if name in item["activity"]])
        expected_backlog = "FAILED" if item["status"] in FAILURES else item["status"] if item["status"] in BACKLOG else "NONE"
        _require(item["backlog"] == expected_backlog and item["error"] in ERRORS)
        v = item["validation"]
        _require(isinstance(v, dict) and set(v) == {"analysis", "actions", "draft_pr"} and
                 v["analysis"] in {"UNVERIFIED", "VALID", "INVALID"} and
                 all(v[key] in {"UNVERIFIED", "RECEIPT_VERIFIED", "INVALID"} for key in ("actions", "draft_pr")))
        _require(item["candidate_sha"] is None or isinstance(item["candidate_sha"], str) and SHA.fullmatch(item["candidate_sha"]))
        _require((item["candidate_sha"] is not None) == (v["actions"] == "RECEIPT_VERIFIED"))
        _require(item["draft_pr_url"] is None or _pr_url(item["draft_pr_url"]))
        _require((item["draft_pr_url"] is not None) == (v["draft_pr"] == "RECEIPT_VERIFIED"))
        _require(v["actions"] != "RECEIPT_VERIFIED" or v["analysis"] == "VALID")
        _require(v["draft_pr"] != "RECEIPT_VERIFIED" or v["actions"] == "RECEIPT_VERIFIED" and item["status"] == "READY_FOR_REVIEW")
    expected_sources = {kind: sum(bool(item["activity"]) and item["source_type"] == kind for item in items)
                        for kind in ("SSTD_CHANGE", "SSTC_FEATURE")}
    _require(report["source_types"] == expected_sources and report["counts"] == _counts(items))
    _require(all(type(value) is int for value in (*report["counts"].values(), *report["source_types"].values())))


def validate_report(report: dict) -> None:
    try:
        _validate_report(report)
    except ReportError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ReportError("INVALID_REPORT") from None


def render_report(report: dict) -> dict:
    validate_report(report)
    counts = report["counts"]
    title = "SST-AX 일일 보고 · " + report["report_date"]
    description = ("한국 시간 전날 00:00~24:00 활동 / 상태는 집계 시점 기준\n"
                   f"활동 Task {counts['daily_tasks']}개 · 대기/실패 {counts['backlog_tasks']}개 · 기록 확인 {counts['invalid_records']}개\n"
                   f"분석 VALID 재검증 {counts['analysis_valid']}개 · Actions 증거 대조 {counts['actions_verified']}개 · Draft PR 증거 대조 {counts['draft_pr_verified']}개")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title}},
              {"type": "section", "text": {"type": "plain_text", "text": description}}]
    lines = [title, description]
    if not report["items"]:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": "변경 및 대기 사항 없음"}})
        lines.append("변경 및 대기 사항 없음")
    for item in report["items"]:
        kind = "SSTD 변경" if item["source_type"] == "SSTD_CHANGE" else "독립 기능 요청"
        group = "당일 활동" if item["activity"] else "이전부터 대기/실패" if item["backlog"] != "NONE" else "기록 확인"
        v = item["validation"]
        analysis = {"VALID": "VALID 재검증", "UNVERIFIED": "미검증", "INVALID": "증거 불일치"}[v["analysis"]]
        actions = {"RECEIPT_VERIFIED": "보존 증거 대조 완료", "UNVERIFIED": "미검증", "INVALID": "증거 불일치"}[v["actions"]]
        line = f"{item['task_id']} · {kind} · {STATUS_LABELS[item['status']]}\n{group} · 분석 {analysis} · Actions {actions}"
        if item["candidate_sha"]:
            line += "\n후보 SHA: " + item["candidate_sha"]
        if item["draft_pr_url"]:
            line += "\nDraft PR: " + item["draft_pr_url"]
        if item["error"]:
            line += "\n기록 또는 증거 확인 필요"
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": line}})
        lines.append(line)
    note = "회귀 테스트 수·분석 의미 정확도·실제 기기·merge·Release·배포 완료는 이 보고로 확인하지 않습니다."
    blocks.append({"type": "section", "text": {"type": "plain_text", "text": note}})
    lines.append(note)
    payload = {"text": "\n\n".join(lines), "blocks": blocks}
    _require(len(blocks) <= 50 and len(payload["text"]) <= 40_000 and
             all(len(block["text"]["text"]) <= (150 if block["type"] == "header" else 3000) for block in blocks), "REPORT_SIZE_LIMIT")
    return payload


@contextmanager
def _lock(directory, day):
    import fcntl
    _require(not any(part.is_symlink() for part in (directory, *directory.parents)), "REPORT_DIRECTORY_UNSAFE")
    directory.mkdir(parents=True, exist_ok=True)
    _require(not directory.is_symlink(), "REPORT_DIRECTORY_UNSAFE")
    descriptor = os.open(directory / (day + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _save_snapshot(path, report):
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".report-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path)  # Never replace an existing immutable snapshot.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _configuration(config):
    _require(isinstance(config, dict), "REPORT_CONFIGURATION_REQUIRED")
    patterns = {"SLACK_TEAM_ID": r"T[A-Z0-9]+", "SLACK_DAILY_REPORT_CHANNEL_ID": r"[CG][A-Z0-9]+", "SLACK_BOT_TOKEN": r"xoxb-\S+"}
    for key, pattern in patterns.items():
        _require(isinstance(config.get(key), str) and re.fullmatch(pattern, config[key]), "REPORT_CONFIGURATION_REQUIRED")


def _new_web(token):
    from slack_sdk import WebClient
    logger = logging.Logger("sst_ax.daily_report.sdk")
    logger.disabled = True
    logger.addHandler(logging.NullHandler())
    return WebClient(token=token, logger=logger, retry_handlers=[])


def _response(value):
    data = value if isinstance(value, dict) else getattr(value, "data", None)
    _require(isinstance(data, dict), "REPORT_RESPONSE_UNCERTAIN")
    return data, getattr(value, "status_code", 200)


def _not_sent(response):
    try:
        data, status = _response(response)
        return (status == 429 or data.get("ok") is False and data.get("error") in {
            "ratelimited", "channel_not_found", "not_in_channel", "is_archived", "missing_scope", "invalid_auth",
            "account_inactive", "no_permission"})
    except (ValueError, TypeError, AttributeError):
        return False


def _ledger(path, day, config, frozen):
    record = _read(path)
    _require(isinstance(record, dict) and set(record) == {"schema_version", "report_date", "team_id", "channel_id",
        "snapshot_sha256", "status", "attempts", "updated_at", "message_ts"}, "REPORT_LEDGER_INVALID")
    _require(record["schema_version"] == "1.0" and record["report_date"] == day and
             record["team_id"] == config["SLACK_TEAM_ID"] and record["channel_id"] == config["SLACK_DAILY_REPORT_CHANNEL_ID"] and
             record["snapshot_sha256"] == digest(frozen), "REPORT_DESTINATION_OR_SNAPSHOT_CHANGED")
    _require(record["status"] in {"READY", "SENDING", "SENT", "NOT_SENT", "UNCERTAIN"} and
             type(record["attempts"]) is int and 0 <= record["attempts"] <= 3, "REPORT_LEDGER_INVALID")
    _instant(record["updated_at"])
    _require(record["message_ts"] is None or isinstance(record["message_ts"], str) and re.fullmatch(r"[0-9]{10}\.[0-9]{6}", record["message_ts"]), "REPORT_LEDGER_INVALID")
    _require(record["status"] != "SENT" or record["message_ts"] is not None and record["attempts"] > 0, "REPORT_LEDGER_INVALID")
    return record


def send_report(report: dict, report_directory: Path, config: dict, web=None) -> str:
    """Persist before POST; lost/mismatched replies stop automatic retransmission."""
    _configuration(config)
    _require(isinstance(report, dict), "INVALID_REPORT")
    day = _date(report.get("report_date")).isoformat()
    try:
        with _lock(report_directory, day):
            frozen_path = report_directory / (day + ".snapshot.json")
            ledger_path = report_directory / (day + ".delivery.json")
            frozen = _read(frozen_path) if frozen_path.exists() else None
            if frozen is not None:
                validate_report(frozen)
                _require(frozen["report_date"] == day, "REPORT_SNAPSHOT_CHANGED")
            _require(not ledger_path.exists() or frozen is not None, "REPORT_SNAPSHOT_REQUIRED")
            record = _ledger(ledger_path, day, config, frozen) if ledger_path.exists() else None
            if record and record["status"] == "SENT":
                return "ALREADY_SENT"
            if record and record["status"] in {"SENDING", "UNCERTAIN"}:
                if record["status"] == "SENDING":
                    record.update(status="UNCERTAIN", updated_at=utc_now())
                    atomic_json(ledger_path, record)
                return "UNCERTAIN"
            validate_report(report)
            payload = render_report(report)
            _require(frozen is None or frozen == report, "REPORT_SNAPSHOT_CHANGED")
            if frozen is None:
                _save_snapshot(frozen_path, report)
                frozen = report
            if record is None:
                record = {"schema_version": "1.0", "report_date": day, "team_id": config["SLACK_TEAM_ID"],
                          "channel_id": config["SLACK_DAILY_REPORT_CHANNEL_ID"], "snapshot_sha256": digest(frozen),
                          "status": "READY", "attempts": 0, "updated_at": utc_now(), "message_ts": None}
                atomic_json(ledger_path, record)
            if record["attempts"] >= 3:
                return "RETRY_LIMIT"
            client = web or _new_web(config["SLACK_BOT_TOKEN"])
            try:
                auth, status = _response(client.auth_test())
            except Exception:
                return "AUTH_CHECK_FAILED"
            if status != 200 or auth.get("ok") is not True or auth.get("team_id") != config["SLACK_TEAM_ID"]:
                return "AUTH_CHECK_FAILED"
            record.update(status="SENDING", attempts=record["attempts"] + 1, updated_at=utc_now())
            atomic_json(ledger_path, record)
            try:
                response = client.chat_postMessage(channel=config["SLACK_DAILY_REPORT_CHANNEL_ID"],
                    text=payload["text"], blocks=payload["blocks"], unfurl_links=False, unfurl_media=False,
                    parse="none", link_names=False)
                data, status = _response(response)
                if status == 200 and data.get("ok") is True and data.get("channel") == record["channel_id"] and \
                        isinstance(data.get("ts"), str) and re.fullmatch(r"[0-9]{10}\.[0-9]{6}", data["ts"]):
                    record.update(status="SENT", message_ts=data["ts"])
                else:
                    record["status"] = "NOT_SENT" if _not_sent(response) else "UNCERTAIN"
            except Exception as error:
                record["status"] = "NOT_SENT" if _not_sent(getattr(error, "response", None)) else "UNCERTAIN"
            record["updated_at"] = utc_now()
            atomic_json(ledger_path, record)
            return record["status"]
    except ReportError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ReportError("REPORT_STORAGE_ERROR") from None
