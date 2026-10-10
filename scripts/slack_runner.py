#!/usr/bin/env python3
"""Slack approvals with bounded listeners or a persistent trusted-directory receiver."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic

from slack_decision_feedback import TASK_ID, prepare_feedback, recover_feedback, send_feedback
from task_storage import companion, load_json, task_lock, validate_pair

sys.dont_write_bytecode = True

IMPACT_LABELS = {
    "ui_ux": "화면·사용자 경험",
    "protocol_contract": "프로토콜·계약",
    "dependency": "외부 의존성",
    "android_permission": "Android 권한",
    "destructive_action": "파괴적 작업",
    "sstd_change_required": "추가 SSTD 변경",
}


def configuration(environ):
    config = {}
    for name, pattern in {
        "SLACK_BOT_TOKEN": r"xoxb-\S+",
        "SLACK_APP_TOKEN": r"xapp-\S+",
        "SLACK_TEAM_ID": r"T[A-Z0-9]+",
        "SLACK_CHANNEL_ID": r"[CG][A-Z0-9]+",
        "SLACK_APP_ID": r"A[A-Z0-9]+",
        "SLACK_APPROVER_IDS": r"[UW][A-Z0-9]+(?:,[UW][A-Z0-9]+)*",
    }.items():
        value = environ.get(name, "")
        if not re.fullmatch(pattern, value):
            raise ValueError("Missing or invalid " + name)
        config[name] = value
    return config


def approval_message(record, review=None):
    def bounded(value, limit=480):
        # Plain text blocks cannot execute embedded Markdown or mention syntax.
        return " ".join(str(value or "없음").split())[:limit]

    text = ("SST-AX 승인 요청: " + record["task_id"] +
            "\n승인은 구현 단계 진입만 허용합니다. 만료: " + record["expires_at"])
    sections = [text]
    if review:
        context = review["input_context"]
        sections.append(
            "출처: " + bounded(review["source_type"], 32) + " / " +
            bounded(review["source_reference"], 220) +
            "\nSSTD: " + bounded(context.get("sstd_base_revision"), 40) + " → " +
            bounded(context.get("sstd_revision"), 40) +
            "\nSSTC 기준: " + bounded(context.get("sstc_revision"), 40) +
            "\n위험도: " + bounded(review["risk_level"], 16) +
            "\n승인 사유: " + bounded(review["approval_reason"], 160) +
            "\n분석 요약·수정 범위: " + bounded(review["summary"], 800))
        affected = [IMPACT_LABELS[key] + ": " + bounded(value["reason"], 230)
                    for key, value in review["impacts"].items()
                    if value["status"] != "ABSENT"]
        sections.append("영향:\n" + ("\n".join(affected) or "없음") +
                        "\n미해결 질문: " + bounded(
                            "; ".join(review["unresolved_questions"]), 500))
    return {
        "text": text,
        "blocks": [
            *[{"type": "section", "text": {"type": "plain_text", "text": section}}
              for section in sections],
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": label},
                 "action_id": action, "value": record["nonce"]}
                for label, action in (("승인", "ax_approve"), ("거절", "ax_reject"))
            ]},
        ],
        "unfurl_links": False, "unfurl_media": False,
    }


def run(args, config):
    from slack_sdk.web import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_approval import prepare_request, bind_message, apply_decision, approval_review

    # SDK diagnostics may contain complete responses. Emit fixed local events only.
    logger = logging.getLogger("sst_ax.slack_transport")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.disabled = True
    web = WebClient(token=config["SLACK_BOT_TOKEN"], timeout=15,
                    retry_handlers=[], logger=logger)
    auth = web.auth_test()
    if auth.get("team_id") != config["SLACK_TEAM_ID"]:
        raise ValueError("Workspace mismatch")
    client = SocketModeClient(app_token=config["SLACK_APP_TOKEN"],
                              web_client=web, logger=logger, concurrency=1)
    done = Event()
    outcome = {"code": 0}
    records = {}
    record_lock = Lock()
    feedback = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slack-feedback")
    persistent = args.command == "serve"
    stop = getattr(args, "stop_event", None) or Event()
    root = getattr(args, "tasks_directory", None)
    if persistent and (not isinstance(root, Path) or not root.is_dir() or root.resolve() != root.absolute()):
        raise ValueError("SLACK_TRUSTED_TASK_DIRECTORY_REQUIRED")
    multiple = bool(getattr(args, "task_files", None))
    timeout = getattr(args, "timeout_seconds", None)
    if timeout is not None and (not isinstance(timeout, (int, float)) or not 0 < timeout <= 60):
        raise ValueError("Slack polling duration must be in (0, 60]")

    def deliver(path, key):
        try:
            status = send_feedback(path, key, config, web)
            print("SLACK_FEEDBACK=" + status, flush=True)
        except Exception:
            print("SLACK_FEEDBACK_REVIEW_REQUIRED", file=sys.stderr, flush=True)

    def enqueue(path, status):
        try:
            return prepare_feedback(path, status, config)
        except Exception:
            # The decision is already committed. A reply failure cannot undo it.
            print("SLACK_FEEDBACK_REVIEW_REQUIRED", file=sys.stderr, flush=True)
            return None

    def receive(connection, request):
        try:
            status = "IGNORED"
            key = None
            task_file = None
            if request.type == "interactive":
                task_file = args.task_file
                if multiple or persistent:
                    payload = request.payload
                    actions = payload.get("actions") if isinstance(payload, dict) else None
                    nonce = (actions[0].get("value") if isinstance(actions, list) and
                             len(actions) == 1 and isinstance(actions[0], dict) else None)
                    with record_lock:
                        task_file = next((path for path, record in records.items()
                                          if isinstance(nonce, str) and record["nonce"] == nonce), None)
                if task_file is None:
                    connection.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                    return
                status = apply_decision(
                    task_file, request.payload,
                    team_id=config["SLACK_TEAM_ID"],
                    channel_id=config["SLACK_CHANNEL_ID"],
                    app_id=config["SLACK_APP_ID"],
                    approver_ids=set(config["SLACK_APPROVER_IDS"].split(",")),
                )
                if status in {"IMPLEMENTING", "REJECTED"}:
                    with record_lock:
                        recovered.add(task_file)
                    key = enqueue(task_file, status)
            # Commit before ack: a crash must not acknowledge an unsaved decision.
            connection.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            if key is not None:
                feedback.submit(deliver, task_file, key)
            if status in {"IMPLEMENTING", "REJECTED"}:
                print("TASK_STATUS=" + status, flush=True)
                if not multiple and not persistent:
                    done.set()
        except Exception:
            # Never echo a Slack response, exception body, token, or payload.
            print("SLACK_DECISION_FAILED: inspect task state; recover pending save before retry", file=sys.stderr)
            outcome["code"] = 2
            done.set()

    recovered = set()

    def refresh(send_requests=True):
        paths = (trusted_tasks(root) if persistent else
                 args.task_files if multiple else [args.task_file])
        for path in paths:
            try:
                with task_lock(path):
                    state = load_json(path)
                if state["status"] != "WAITING_APPROVAL":
                    if path not in recovered:
                        key = recover_feedback(path, config)
                        recovered.add(path)
                        if key is not None:
                            feedback.submit(deliver, path, key)
                    continue
                record = prepare_request(path, config["SLACK_TEAM_ID"],
                                         config["SLACK_CHANNEL_ID"], config["SLACK_APP_ID"])
                with record_lock:
                    records[path] = record
                if send_requests and not record.get("message_ts"):
                    review = approval_review(path, record)
                    response = web.chat_postMessage(channel=config["SLACK_CHANNEL_ID"],
                                                     **approval_message(record, review))
                    if response.get("channel") != config["SLACK_CHANNEL_ID"]:
                        raise ValueError("Channel mismatch")
                    bind_message(path, record["nonce"], response["ts"])
            except Exception:
                if not persistent:
                    raise
                print("SLACK_TASK_REVIEW_REQUIRED", file=sys.stderr, flush=True)

    try:
        if args.command == "listen" and not persistent:
            paths = args.task_files if multiple else [args.task_file]
            for path in paths:
                record = prepare_request(path, config["SLACK_TEAM_ID"],
                                         config["SLACK_CHANNEL_ID"], config["SLACK_APP_ID"])
                records[path] = record
        if args.command in {"listen", "serve"}:
            client.socket_mode_request_listeners.append(receive)
        if persistent:
            # Register already bound requests before connect can deliver a click.
            refresh(send_requests=False)
        client.connect()
        print("SLACK_CONNECTED", flush=True)
        if args.command == "check":
            return 0
        if done.is_set():
            return outcome["code"]
        if not persistent:
            # Keep the bounded listener's original request/reuse behavior.
            for path, record in records.items():
                if not record.get("message_ts"):
                    review = approval_review(path, record)
                    response = web.chat_postMessage(channel=config["SLACK_CHANNEL_ID"],
                                                     **approval_message(record, review))
                    if response.get("channel") != config["SLACK_CHANNEL_ID"]:
                        raise ValueError("Channel mismatch")
                    bind_message(path, record["nonce"], response["ts"])
        else:
            refresh()
        print("SLACK_WAITING_APPROVAL", flush=True)
        deadline = None if timeout is None else monotonic() + timeout
        while not done.is_set() and not stop.wait(1):
            if persistent:
                refresh()
            if deadline is not None and monotonic() >= deadline:
                break
        return outcome["code"]
    finally:
        client.close()
        feedback.shutdown(wait=True)


def trusted_tasks(root):
    """Select only local primary task states; Slack payloads cannot choose paths."""
    paths = list(root.iterdir())
    if len(paths) > 10_000:
        raise ValueError("SLACK_TASK_SCAN_LIMIT")
    selected = []
    for path in sorted(paths):
        if path.suffix != ".json" or not TASK_ID.fullmatch(path.stem):
            continue
        if path.is_symlink() or path.resolve().parent != root.resolve():
            raise ValueError("SLACK_TASK_SYMLINK_REFUSED")
        from codex_impact import regular
        regular(path)
        regular(companion(path, "log"))
        try:
            with task_lock(path):
                if companion(path, "pending").exists():
                    raise ValueError("SLACK_TASK_RECOVERY_REQUIRED")
                state, log = load_json(path), load_json(companion(path, "log"))
                validate_pair(path, state, log)
        except BlockingIOError:
            # Controller or reply worker is committing this task. Try next scan.
            continue
        if state["status"] == "WAITING_APPROVAL" or companion(path, "slack-feedback").exists():
            selected.append(path)
    return selected


def listen_many(task_files, config, timeout_seconds=5):
    """Use one Socket Mode connection to route all pending approval nonces."""
    if not task_files:
        return 0
    return run(argparse.Namespace(command="listen", task_file=None,
                                  task_files=list(task_files), timeout_seconds=timeout_seconds), config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("check", "listen", "serve"))
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--tasks-directory", type=Path,
                        help="Trusted Controller task directory for persistent serve mode")
    parser.add_argument("--timeout-seconds", type=float,
                        help="Return after a bounded approval poll; saved requests remain live")
    args = parser.parse_args(argv)
    if args.command == "listen" and args.task_file is None:
        parser.error("listen requires --task-file")
    if args.command == "serve" and (args.tasks_directory is None or args.task_file or args.timeout_seconds is not None):
        parser.error("serve requires --tasks-directory and has no task-file or timeout")
    try:
        config = configuration(os.environ)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        if args.command == "serve":
            import signal
            args.stop_event = Event()
            previous = {number: signal.signal(number, lambda *_: args.stop_event.set())
                        for number in (signal.SIGINT, signal.SIGTERM)}
            try:
                return run(args, config)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
        return run(args, config)
    except KeyboardInterrupt:
        print("SLACK_STOPPED")
        return 130
    except ImportError:
        print("SLACK_DEPENDENCY_MISSING: install requirements-slack.txt", file=sys.stderr)
        return 2
    except Exception:
        print("SLACK_FAILED: check configuration, connectivity and task/checkpoint validity", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
