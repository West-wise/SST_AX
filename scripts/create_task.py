#!/usr/bin/env python3
"""Create a schema-valid local task state and task log for SST-AX."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_task_state import DEFAULT_SCHEMA_PATH, load_json, validate_task_state

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK_DIRECTORY = REPOSITORY_ROOT / "state" / "tasks"
TASK_ID_PATTERN = re.compile(r"^(sstd-sync|sstc-feature)-(\d{8})-(\d{4})\.json$")


def utc_now() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repository_revision() -> str:
    """Return the current SST-AX revision, if Git metadata is available."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def next_task_id(source_type: str, task_directory: Path) -> str:
    """Generate the next daily task ID without overwriting existing state."""
    prefix = "sstd-sync" if source_type == "SSTD_CHANGE" else "sstc-feature"
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    highest = 0
    if task_directory.exists():
        for task_file in task_directory.glob("*.json"):
            match = TASK_ID_PATTERN.match(task_file.name)
            if match and match.group(1) == prefix and match.group(2) == date:
                highest = max(highest, int(match.group(3)))
    return f"{prefix}-{date}-{highest + 1:04d}"


def parse_args() -> argparse.Namespace:
    """Parse task creation arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--source-type", choices=("SSTD_CHANGE", "SSTC_FEATURE"), required=True)
    parser.add_argument("--source-reference", required=True, help="SSTD commit/ref or SSTC Issue URL")
    parser.add_argument(
        "--risk-level",
        choices=("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"),
        default="MEDIUM",
    )
    parser.add_argument("--task-id", help="Explicit task ID; otherwise generated from UTC date")
    parser.add_argument("--branch", help="Reserved SSTC work branch, starting with ax/sstc-sync/")
    parser.add_argument("--approval-reason", help="Reason to request human approval")
    parser.add_argument("--checkpoint-path", help="Checkpoint path under state/checkpoints/")
    parser.add_argument("--task-directory", type=Path, default=DEFAULT_TASK_DIRECTORY)
    return parser.parse_args()


def build_task_state(args: argparse.Namespace, task_id: str, now: str) -> dict[str, Any]:
    """Build the initial state document using only schema-defined fields."""
    task_state: dict[str, Any] = {
        "task_id": task_id,
        "source_type": args.source_type,
        "source_reference": args.source_reference.strip(),
        "status": "RECEIVED",
        "risk_level": args.risk_level,
        "created_at": now,
        "updated_at": now,
        "attempt": 0,
        "approval_reason": args.approval_reason,
        "checkpoint_path": args.checkpoint_path,
        "deferred_until": None,
        "draft_pr_url": None,
        "unresolved_issues": [],
    }
    if args.branch:
        task_state["branch"] = args.branch
    return task_state


def build_task_log(task_state: dict[str, Any], now: str) -> dict[str, Any]:
    """Create an initial task log from the repository template."""
    template_path = REPOSITORY_ROOT / "templates" / "task-log.json"
    task_log = load_json(template_path)
    revision = repository_revision()
    task_log.update(
        {
            "task_id": task_state["task_id"],
            "source_type": task_state["source_type"],
            "source_reference": task_state["source_reference"],
            "status": task_state["status"],
            "started_at": now,
            "policy_version": revision,
            "guide_version": revision,
            "commands": [],
            "state_transitions": [
                {
                    "from_status": None,
                    "to_status": task_state["status"],
                    "occurred_at": now,
                    "reason": "task created",
                }
            ],
            "unresolved_issues": [],
        }
    )
    return task_log


def main() -> int:
    """Create a task state and matching task log without external calls."""
    args = parse_args()
    args.task_directory = args.task_directory.resolve()
    task_id = args.task_id or next_task_id(args.source_type, args.task_directory)
    expected_prefix = "sstd-sync-" if args.source_type == "SSTD_CHANGE" else "sstc-feature-"
    if not task_id.startswith(expected_prefix):
        print("Task ID prefix does not match source type.", file=sys.stderr)
        return 2

    now = utc_now()
    task_state = build_task_state(args, task_id, now)
    try:
        schema = load_json(DEFAULT_SCHEMA_PATH)
    except (OSError, json.JSONDecodeError) as error:
        print(f"Schema load failed: {error}", file=sys.stderr)
        return 2
    errors = validate_task_state(task_state, schema)
    if errors:
        print("Task state is invalid:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 2

    task_path = args.task_directory / f"{task_id}.json"
    log_path = args.task_directory / f"{task_id}.log.json"
    if task_path.exists() or log_path.exists():
        print(f"Task already exists: {task_id}", file=sys.stderr)
        return 1

    args.task_directory.mkdir(parents=True, exist_ok=True)
    task_path.write_text(json.dumps(task_state, indent=2) + "\n", encoding="utf-8")
    log_path.write_text(json.dumps(build_task_log(task_state, now), indent=2) + "\n", encoding="utf-8")
    print(f"TASK_ID={task_id}")
    print(f"TASK_STATE={task_path}")
    print(f"TASK_LOG={log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
