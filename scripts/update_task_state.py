#!/usr/bin/env python3
"""Apply one allowed SST-AX task-state transition and record it in the task log."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_task_state import DEFAULT_SCHEMA_PATH, load_json, validate_task_state

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "RECEIVED": {"ANALYZING", "REJECTED"},
    "ANALYZING": {
        "WAITING_APPROVAL",
        "IMPLEMENTING",
        "COMPLETED",
        "DEFERRED_RATE_LIMIT",
        "ANALYSIS_FAILED",
        "PROTOCOL_APPROVAL_REQUIRED",
    },
    "WAITING_APPROVAL": {"IMPLEMENTING", "REJECTED", "DEFERRED_RATE_LIMIT"},
    "IMPLEMENTING": {
        "VALIDATING",
        "WAITING_APPROVAL",
        "DEFERRED_RATE_LIMIT",
        "IMPLEMENTATION_FAILED",
    },
    "VALIDATING": {
        "READY_FOR_REVIEW",
        "BUILD_FAILED",
        "TEST_FAILED",
        "SECURITY_REVIEW_FAILED",
    },
    "DEFERRED_RATE_LIMIT": {"ANALYZING", "IMPLEMENTING", "WAITING_APPROVAL", "REJECTED"},
}
TERMINAL_STATUSES = {
    "READY_FOR_REVIEW",
    "COMPLETED",
    "REJECTED",
    "ANALYSIS_FAILED",
    "IMPLEMENTATION_FAILED",
    "BUILD_FAILED",
    "TEST_FAILED",
    "SECURITY_REVIEW_FAILED",
    "PROTOCOL_APPROVAL_REQUIRED",
}
REASON_REQUIRED_STATUSES = TERMINAL_STATUSES | {
    "WAITING_APPROVAL",
    "DEFERRED_RATE_LIMIT",
}


def utc_now() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    """Parse task state transition arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--status", required=True, help="Target task status")
    parser.add_argument("--reason", help="Required for wait, defer, and terminal statuses")
    return parser.parse_args()


def task_log_path(task_file: Path) -> Path:
    """Return the log path paired with a task-state path."""
    return task_file.with_name(f"{task_file.stem}.log.json")


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Write a UTF-8 JSON document with a trailing newline."""
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    """Apply one safe state transition to a task and its local audit log."""
    args = parse_args()
    try:
        task_state = load_json(args.task_file)
        schema = load_json(DEFAULT_SCHEMA_PATH)
        task_log = load_json(task_log_path(args.task_file))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Task load failed: {error}", file=sys.stderr)
        return 2

    errors = validate_task_state(task_state, schema)
    if errors:
        print("Task state is invalid:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 2
    if not isinstance(task_log, dict):
        print("Task log must be a JSON object.", file=sys.stderr)
        return 2

    current_status = task_state["status"]
    if args.status not in ALLOWED_TRANSITIONS.get(current_status, set()):
        print(f"Transition is not allowed: {current_status} -> {args.status}", file=sys.stderr)
        return 1
    if args.status in REASON_REQUIRED_STATUSES and not args.reason:
        print(f"--reason is required for {args.status}", file=sys.stderr)
        return 2

    now = utc_now()
    task_state["status"] = args.status
    task_state["updated_at"] = now
    errors = validate_task_state(task_state, schema)
    if errors:
        print("Updated task state is invalid:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 2

    transitions = task_log.setdefault("state_transitions", [])
    if not isinstance(transitions, list):
        print("Task log state_transitions must be an array.", file=sys.stderr)
        return 2
    transitions.append(
        {
            "from_status": current_status,
            "to_status": args.status,
            "occurred_at": now,
            "reason": args.reason,
        }
    )
    task_log["status"] = args.status
    if args.status in TERMINAL_STATUSES:
        task_log["finished_at"] = now
        task_log["stop_reason"] = args.reason

    write_json(args.task_file, task_state)
    write_json(task_log_path(args.task_file), task_log)
    print(f"TASK_STATUS={args.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
