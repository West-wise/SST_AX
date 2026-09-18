#!/usr/bin/env python3
"""Request one checkpoint-bound Slack approval without persisting credentials."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True

VERSION = "1.0.0"
TOKEN_NAMES = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
ID_NAMES = ("SLACK_TEAM_ID", "SLACK_CHANNEL_ID", "SLACK_APP_ID", "SLACK_APPROVER_IDS")


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "SLACK_APPROVAL_ERROR: invalid arguments; see --help\n")


def configuration(environ: dict[str, str], interactive: bool) -> dict[str, str]:
    """Read missing values for this process only and validate without echoing secrets."""
    values = {name: environ.get(name, "") for name in (*TOKEN_NAMES, *ID_NAMES)}
    missing = [name for name in (*TOKEN_NAMES, *ID_NAMES) if not values.get(name)]
    if missing and not interactive:
        raise ValueError("Missing Slack configuration: " + ", ".join(missing))
    for name in TOKEN_NAMES:
        if not values.get(name):
            values[name] = getpass.getpass(name + ": ")
    for name in ID_NAMES:
        if not values.get(name):
            values[name] = input(name + ": ").strip()
    from slack_runner import configuration as validate
    return validate(values)


def run(task_file: Path, reason: str, config: dict[str, str]) -> int:
    """Check transport, checkpoint an ANALYZING task, then wait for a decision."""
    from slack_runner import run as run_gateway
    from task_storage import load_json, task_lock
    from update_task_state import update

    state = load_json(task_file)
    status = state.get("status")
    if status not in {"ANALYZING", "WAITING_APPROVAL"}:
        raise ValueError("Task must be ANALYZING or WAITING_APPROVAL")
    if status == "WAITING_APPROVAL" and state.get("approval_reason") != reason:
        raise ValueError("Approval reason does not match the existing checkpoint")

    code = run_gateway(argparse.Namespace(command="check", task_file=None), config)
    if code:
        return code
    if status == "ANALYZING":
        args = argparse.Namespace(task_file=task_file, status="WAITING_APPROVAL",
                                  recover=False, record_reset_at=None,
                                  reason=reason, deferred_until=None)
        with task_lock(task_file):
            code = update(args)
        if code:
            return code
    return run_gateway(argparse.Namespace(command="listen", task_file=task_file), config)


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        reason = args.reason.strip()
        if (not reason or len(reason) > 256
                or any(ord(character) < 32 for character in reason)):
            raise ValueError("Invalid approval reason")
        config = configuration(os.environ, sys.stdin.isatty())
        return run(args.task_file.absolute(), reason, config)
    except KeyboardInterrupt:
        print("SLACK_APPROVAL_STOPPED", file=sys.stderr)
        return 130
    except ImportError:
        print("SLACK_DEPENDENCY_MISSING: install requirements-slack.txt", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError):
        print("SLACK_APPROVAL_ERROR: configuration, Task, checkpoint, or transport check failed",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
