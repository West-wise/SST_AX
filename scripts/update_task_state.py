#!/usr/bin/env python3
"""Apply one allowed SST-AX task-state transition and record it in the task log."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from validate_task_state import DEFAULT_SCHEMA_PATH, load_json, validate_task_state
from task_storage import (
    companion, load_checkpoint, recover_pair,
    save_pair, task_lock, validate_pair,
)

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
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", help="Target task status")
    action.add_argument("--recover", action="store_true", help="Repair an interrupted save only")
    action.add_argument("--record-reset-at", help="Record an observed usage reset time on a deferred task")
    parser.add_argument("--reason", help="Required for wait, defer, and terminal statuses")
    parser.add_argument("--deferred-until", help="Observed reset time, ISO-8601 with timezone")
    return parser.parse_args()


def task_log_path(task_file: Path) -> Path:
    """Return the log path paired with a task-state path."""
    return task_file.with_name(f"{task_file.stem}.log.json")


def update(args: argparse.Namespace) -> int:
    """Apply a transition while holding the task lock."""
    if args.recover:
        recover_pair(args.task_file)
        print("TASK_RECOVERED")
        return 0
    from impact_collection import check_content
    for value in (args.reason, args.deferred_until, getattr(args, "record_reset_at", None)):
        if isinstance(value, str):
            check_content(value.encode("utf-8"))
    if companion(args.task_file, "pending").exists():
        raise ValueError("Interrupted save: run --recover first")
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
    validate_pair(args.task_file, task_state, task_log)

    if getattr(args, "record_reset_at", None):
        if task_state["status"] != "DEFERRED_RATE_LIMIT" or args.deferred_until:
            raise ValueError("Reset recording requires a deferred task")
        checkpoint = load_checkpoint(args.task_file, task_state, task_log)
        deadline = datetime.fromisoformat(args.record_reset_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError("Reset time must include timezone")
        task_state.update(deferred_until=args.record_reset_at, updated_at=utc_now())
        task_log.setdefault("reset_observations", []).append({
            "reset_at": args.record_reset_at, "recorded_at": task_state["updated_at"]})
        save_pair(args.task_file, task_state, task_log, checkpoint_status=checkpoint["resume_status"])
        print("RESET_TIME_RECORDED")
        return 0

    current_status = task_state["status"]
    if args.status not in ALLOWED_TRANSITIONS.get(current_status, set()):
        print(f"Transition is not allowed: {current_status} -> {args.status}", file=sys.stderr)
        return 1
    if args.status in REASON_REQUIRED_STATUSES and not args.reason:
        print(f"--reason is required for {args.status}", file=sys.stderr)
        return 2

    checkpoint = None
    if current_status in {"WAITING_APPROVAL", "DEFERRED_RATE_LIMIT"}:
        checkpoint = load_checkpoint(args.task_file, task_state, task_log)
    if current_status == "DEFERRED_RATE_LIMIT" and args.status != "REJECTED":
        if args.status != checkpoint["resume_status"]:
            raise ValueError("Resume must return to the checkpoint stage")
        deadline = task_state.get("deferred_until")
        if not deadline or datetime.fromisoformat(deadline.replace("Z", "+00:00")) > datetime.now(timezone.utc):
            raise ValueError("Rate-limit reset time is missing or has not elapsed")
    if args.status == "IMPLEMENTING":
        if task_state["risk_level"] == "CRITICAL":
            raise ValueError("CRITICAL tasks cannot be implemented automatically")
        if (task_state["risk_level"] == "HIGH" or task_state.get("approval_reason")
                or current_status == "WAITING_APPROVAL"):
            raise ValueError("Verified Slack approval required; use the authenticated Slack gateway")
    if args.status == "READY_FOR_REVIEW":
        raise ValueError("Trusted validation evidence and Draft PR receipt required; use the SSTC pipeline producer")

    resume_status = current_status
    if current_status == "DEFERRED_RATE_LIMIT":
        resume_status = checkpoint["resume_status"]
    if args.status in {"WAITING_APPROVAL", "DEFERRED_RATE_LIMIT"}:
        task_state["checkpoint_path"] = f"state/checkpoints/{task_state['task_id']}.json"
        if args.status == "WAITING_APPROVAL":
            task_state["approval_reason"] = args.reason
    if args.deferred_until and args.status != "DEFERRED_RATE_LIMIT":
        raise ValueError("--deferred-until is only valid when deferring")
    if args.status == "DEFERRED_RATE_LIMIT":
        if not args.deferred_until:
            raise ValueError("--deferred-until requires an observed reset time")
        deadline = datetime.fromisoformat(args.deferred_until.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError("Reset time must include timezone")
        task_state["deferred_until"] = args.deferred_until

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

    save_pair(args.task_file, task_state, task_log,
              checkpoint_status=resume_status if args.status in {"WAITING_APPROVAL", "DEFERRED_RATE_LIMIT"} else None)
    print(f"TASK_STATUS={args.status}")
    return 0


def main() -> int:
    """Serialize writes and fail closed when state or evidence is inconsistent."""
    args = parse_args()
    try:
        with task_lock(args.task_file):
            return update(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"Task update refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
