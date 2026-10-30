"""Approval-bound completion replies; notifications never change Task outcomes."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re

from codex_impact import digest, regular
from daily_report import _proofs
from slack_decision_feedback import TASK_ID, _identity
from task_storage import atomic_json, companion, load_json, task_lock, validate_pair, process_stopped

STATES = {"WAITING", "PENDING", "SENDING", "SENT", "UNCERTAIN", "REFUSED"}
PR = re.compile(r"https://github\.com/West-wise/Server_State_Telemetry_Client/pull/[1-9][0-9]*")
TS = re.compile(r"[0-9]+\.[0-9]+")
SHA = re.compile(r"[0-9a-f]{64}")


def require(condition):
    if not condition:
        raise ValueError("SLACK_COMPLETION_RECORD_REFUSED")


def ledger_path(task_file):
    return companion(task_file, "slack-completion")


def load_delivery(task_file):
    path = ledger_path(task_file)
    if not path.exists():
        return None
    regular(path)
    value = load_json(path)
    require(isinstance(value, dict) and set(value) == {
        "schema_version", "task_id", "approval_key", "channel_id", "thread_ts",
        "snapshot_hash", "status", "candidate_sha", "pr_url", "ready_snapshot",
        "message_ts", "preflight_failures", "sender_pid"})
    require(value["schema_version"] == "1.0" and value["task_id"] == task_file.stem and
            TASK_ID.fullmatch(task_file.stem) and value["status"] in STATES and
            isinstance(value["approval_key"], str) and SHA.fullmatch(value["approval_key"]) and
            isinstance(value["snapshot_hash"], str) and SHA.fullmatch(value["snapshot_hash"]) and
            isinstance(value["channel_id"], str) and re.fullmatch(r"[CG][A-Z0-9]+", value["channel_id"]) and
            isinstance(value["thread_ts"], str) and TS.fullmatch(value["thread_ts"]) and
            type(value["preflight_failures"]) is int and 0 <= value["preflight_failures"] <= 3)
    require(value["sender_pid"] is None or type(value["sender_pid"]) is int and value["sender_pid"] > 0)
    require(value["status"] != "SENDING" or value["sender_pid"] is not None)
    bound = (value["candidate_sha"], value["pr_url"], value["ready_snapshot"])
    require(all(item is None for item in bound) or
            isinstance(bound[0], str) and re.fullmatch(r"[0-9a-f]{40}", bound[0]) and
            isinstance(bound[1], str) and PR.fullmatch(bound[1]) and
            isinstance(bound[2], str) and SHA.fullmatch(bound[2]))
    require(value["status"] not in {"PENDING", "SENDING", "SENT", "UNCERTAIN"} or bound[0] is not None)
    require(value["message_ts"] is None or isinstance(value["message_ts"], str) and TS.fullmatch(value["message_ts"]))
    require(value["status"] != "SENT" or value["message_ts"] is not None)
    return value


def arm_completion(task_file, key, identity):
    """Caller holds Task lock and has verified the newly committed approval."""
    value = load_delivery(task_file)
    expected = {"task_id": task_file.stem, "approval_key": key,
                **{name: identity[name] for name in ("channel_id", "thread_ts", "snapshot_hash")}}
    if value is not None:
        require(all(value[name] == item for name, item in expected.items()))
        return
    atomic_json(ledger_path(task_file), {"schema_version": "1.0", **expected,
        "status": "WAITING", "candidate_sha": None, "pr_url": None, "ready_snapshot": None,
        "message_ts": None, "preflight_failures": 0, "sender_pid": None})


def advance(task_file, config):
    """Caller holds lock. Bind only a verified producer/Sensor completion."""
    value = load_delivery(task_file)
    if value is None:
        return None, "NOT_ARMED"
    key, identity = _identity(task_file, "IMPLEMENTING", config)
    require(key == value["approval_key"] and all(value[name] == identity[name]
            for name in ("channel_id", "thread_ts", "snapshot_hash")))
    if value["status"] == "SENDING" and not process_stopped(value["sender_pid"]):
        return value, "SENDING"
    if value["status"] == "SENDING":
        value["status"] = "UNCERTAIN"
        atomic_json(ledger_path(task_file), value)
    if value["status"] in {"SENT", "UNCERTAIN", "REFUSED"}:
        return value, value["status"]
    regular(task_file)
    regular(companion(task_file, "log"))
    state, log = load_json(task_file), load_json(companion(task_file, "log"))
    validate_pair(task_file, state, log)
    if state["status"] != "READY_FOR_REVIEW":
        return value, "NOT_READY"
    validation, candidate, url, error = _proofs(task_file, state, log)
    if error is not None or validation != {
        "analysis": "VALID", "actions": "RECEIPT_VERIFIED", "draft_pr": "RECEIPT_VERIFIED"
    }:
        value["status"] = "REFUSED"
        atomic_json(ledger_path(task_file), value)
        return value, "REFUSED"
    binding = {"candidate_sha": candidate, "pr_url": url,
               "ready_snapshot": digest(load_json(companion(task_file, "pipeline"))["final_snapshot"])}
    if value["status"] == "WAITING":
        value.update(binding, status="PENDING")
        atomic_json(ledger_path(task_file), value)
    else:
        require(all(value[name] == item for name, item in binding.items()))
    return value, value["status"]


def send_completion(task_file: Path, config: dict, web) -> str:
    with task_lock(task_file):
        value, status = advance(task_file, config)
        if status != "PENDING":
            return status
        value.update(status="SENDING", sender_pid=os.getpid())
        atomic_json(ledger_path(task_file), value)
    text = ("SST-AX 작업 완료: " + task_file.stem +
            "\n구현 및 필수 검증을 마쳤습니다. PR을 검토해 주세요.\nPR: " + value["pr_url"])
    status, timestamp = "UNCERTAIN", None
    try:
        response = web.chat_postMessage(channel=value["channel_id"], thread_ts=value["thread_ts"],
            text=text, parse="none", unfurl_links=False, unfurl_media=False)
        observed = response.get("ts")
        if (response.get("ok") is True and response.get("channel") == value["channel_id"] and
                isinstance(observed, str) and TS.fullmatch(observed)):
            status, timestamp = "SENT", observed
    except Exception:
        pass  # SDK response/exception bodies may contain credentials.
    with task_lock(task_file):
        stored = load_delivery(task_file)
        require(stored == value)
        stored.update(status=status, message_ts=timestamp)
        atomic_json(ledger_path(task_file), stored)
    return status


def deliver_completions(task_files, config, web=None) -> int:
    """No Socket Mode connection, extra token scope, or model call is needed."""
    pending, review = [], False
    for path in task_files:
        try:
            with task_lock(path):
                _, status = advance(path, config)
            if status == "PENDING":
                pending.append(path)
            elif status in {"UNCERTAIN", "REFUSED"}:
                review = True
        except BlockingIOError:
            continue
        except Exception:
            review = True
    if not pending:
        return 2 if review else 0
    try:
        if web is None:
            from slack_sdk.web import WebClient
            logger = logging.getLogger("sst_ax.slack_completion")
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
            logger.disabled = True
            web = WebClient(token=config["SLACK_BOT_TOKEN"], timeout=15, retry_handlers=[], logger=logger)
        require(web.auth_test().get("team_id") == config["SLACK_TEAM_ID"])
    except Exception:
        for path in pending:
            with task_lock(path):
                value = load_delivery(path)
                if value["status"] == "PENDING":
                    value["preflight_failures"] += 1
                    if value["preflight_failures"] >= 3:
                        value["status"] = "REFUSED"
                    atomic_json(ledger_path(path), value)
        return 2
    for path in pending:
        try:
            if send_completion(path, config, web) not in {"SENT", "SENDING"}:
                review = True
        except BlockingIOError:
            continue
        except Exception:
            review = True
    return 2 if review else 0
