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

sys.dont_write_bytecode = True


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


def approval_message(record):
    # Only Controller-generated identifiers are sent; no source text or raw logs.
    text = ("SST-AX 승인 요청: " + record["task_id"] +
            "\n현재 Task와 checkpoint의 작업 범위를 검토한 후 선택하세요."
            "\n승인은 구현 단계 진입만 허용합니다. 만료: " + record["expires_at"])
    return {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "plain_text", "text": text}},
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
    from slack_approval import prepare_request, bind_message, apply_decision

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

    def receive(connection, request):
        try:
            status = "IGNORED"
            if request.type == "interactive":
                status = apply_decision(
                    args.task_file, request.payload,
                    team_id=config["SLACK_TEAM_ID"],
                    channel_id=config["SLACK_CHANNEL_ID"],
                    app_id=config["SLACK_APP_ID"],
                    approver_ids=set(config["SLACK_APPROVER_IDS"].split(",")),
                )
            # Commit before ack: a crash must not acknowledge an unsaved decision.
            connection.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            if status in {"IMPLEMENTING", "REJECTED"}:
                print("TASK_STATUS=" + status, flush=True)
                done.set()
        except Exception:
            # Never echo a Slack response, exception body, token, or payload.
            print("SLACK_DECISION_FAILED: inspect task state; recover pending save before retry", file=sys.stderr)
            outcome["code"] = 2
            done.set()

    try:
        if args.command == "listen":
            record = prepare_request(args.task_file, config["SLACK_TEAM_ID"],
                                     config["SLACK_CHANNEL_ID"], config["SLACK_APP_ID"])
            client.socket_mode_request_listeners.append(receive)
        client.connect()
        print("SLACK_CONNECTED", flush=True)
        if args.command == "check":
            return 0
        if done.is_set():
            return outcome["code"]
        if not record.get("message_ts"):
            response = web.chat_postMessage(channel=config["SLACK_CHANNEL_ID"],
                                             **approval_message(record))
            if response.get("channel") != config["SLACK_CHANNEL_ID"]:
                raise ValueError("Channel mismatch")
            bind_message(args.task_file, record["nonce"], response["ts"])
        print("SLACK_WAITING_APPROVAL", flush=True)
        while not done.wait(1):
            pass
        return outcome["code"]
    finally:
        client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("check", "listen"))
    parser.add_argument("--task-file", type=Path)
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
