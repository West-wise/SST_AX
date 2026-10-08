#!/usr/bin/env python3
"""One-task Slack approval listener. Secrets are read only from environment."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from threading import Event
from time import monotonic

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
    records = []
    multiple = bool(getattr(args, "task_files", None))
    timeout = getattr(args, "timeout_seconds", None)
    if timeout is not None and (not isinstance(timeout, (int, float)) or not 0 < timeout <= 60):
        raise ValueError("Slack polling duration must be in (0, 60]")

    def receive(connection, request):
        try:
            status = "IGNORED"
            if request.type == "interactive":
                task_file = args.task_file
                if multiple:
                    actions = request.payload.get("actions", [])
                    nonce = actions[0].get("value") if len(actions) == 1 else None
                    task_file = next((path for path, record in records
                                      if record["nonce"] == nonce), None)
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
            # Commit before ack: a crash must not acknowledge an unsaved decision.
            connection.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            if status in {"IMPLEMENTING", "REJECTED"}:
                print("TASK_STATUS=" + status, flush=True)
                if not multiple:
                    done.set()
        except Exception:
            # Never echo a Slack response, exception body, token, or payload.
            print("SLACK_DECISION_FAILED: inspect task state; recover pending save before retry", file=sys.stderr)
            outcome["code"] = 2
            done.set()

    try:
        if args.command == "listen":
            paths = args.task_files if multiple else [args.task_file]
            for path in paths:
                record = prepare_request(path, config["SLACK_TEAM_ID"],
                                         config["SLACK_CHANNEL_ID"], config["SLACK_APP_ID"])
                records.append((path, record))
            client.socket_mode_request_listeners.append(receive)
        client.connect()
        print("SLACK_CONNECTED", flush=True)
        if args.command == "check":
            return 0
        if done.is_set():
            return outcome["code"]
        for path, record in records:
            if not record.get("message_ts"):
                review = approval_review(path, record)
                response = web.chat_postMessage(channel=config["SLACK_CHANNEL_ID"],
                                                 **approval_message(record, review))
                if response.get("channel") != config["SLACK_CHANNEL_ID"]:
                    raise ValueError("Channel mismatch")
                bind_message(path, record["nonce"], response["ts"])
        print("SLACK_WAITING_APPROVAL", flush=True)
        deadline = None if timeout is None else monotonic() + timeout
        while not done.wait(1):
            if deadline is not None and monotonic() >= deadline:
                break
        return outcome["code"]
    finally:
        client.close()


def listen_many(task_files, config, timeout_seconds=5):
    """Use one Socket Mode connection to route all pending approval nonces."""
    if not task_files:
        return 0
    return run(argparse.Namespace(command="listen", task_file=None,
                                  task_files=list(task_files), timeout_seconds=timeout_seconds), config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("check", "listen"))
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--timeout-seconds", type=float,
                        help="Return after a bounded approval poll; saved requests remain live")
    args = parser.parse_args(argv)
    if args.command == "listen" and args.task_file is None:
        parser.error("listen requires --task-file")
    try:
        config = configuration(os.environ)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
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
