#!/usr/bin/env python3
"""Collect read-only Git evidence for an SST-AX task impact review."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from validate_task_state import DEFAULT_SCHEMA_PATH, load_json, validate_task_state

MAX_EVIDENCE_LENGTH = 30_000


def run_git(repository: Path, arguments: list[str]) -> str:
    """Run a read-only Git command and return its standard output.

    Args:
        repository: Local Git repository to inspect.
        arguments: Git arguments after the executable name.

    Returns:
        Command standard output.

    Raises:
        RuntimeError: If Git cannot collect the requested evidence.
    """
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise RuntimeError(detail)
    return result.stdout


def truncate(value: str) -> str:
    """Bound report size while retaining evidence that it was truncated."""
    if len(value) <= MAX_EVIDENCE_LENGTH:
        return value
    return f"{value[:MAX_EVIDENCE_LENGTH]}\n\n[truncated by SST-AX]\n"


def parse_args() -> argparse.Namespace:
    """Parse impact analysis arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-file", type=Path, required=True, help="Schema-valid task state")
    parser.add_argument(
        "--source-repository",
        type=Path,
        help="Local source repository; required for SSTD_CHANGE",
    )
    return parser.parse_args()


def main() -> int:
    """Write a deterministic, read-only impact evidence report."""
    args = parse_args()
    try:
        task_state = load_json(args.task_file)
        errors = validate_task_state(task_state, load_json(DEFAULT_SCHEMA_PATH))
    except (OSError, ValueError) as error:
        print(f"Task load failed: {error}", file=sys.stderr)
        return 2
    if errors:
        print("Task state is invalid:", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        return 2

    report_path = args.task_file.with_suffix(".impact.md")
    lines = [
        f"# Impact Evidence: {task_state['task_id']}",
        "",
        "This report collects deterministic evidence only. It does not decide risk,",
        "request approval, invoke Codex, or modify SSTD/SSTC source repositories.",
        "",
        f"- Source type: `{task_state['source_type']}`",
        f"- Source reference: `{task_state['source_reference']}`",
        f"- Current task risk: `{task_state['risk_level']}`",
    ]

    if task_state["source_type"] == "SSTD_CHANGE":
        if args.source_repository is None:
            print("--source-repository is required for SSTD_CHANGE.", file=sys.stderr)
            return 2
        repository = args.source_repository.resolve()
        if not (repository / ".git").exists():
            print(f"Not a Git repository: {repository}", file=sys.stderr)
            return 2
        reference = task_state["source_reference"]
        try:
            revision = run_git(repository, ["rev-parse", "--verify", f"{reference}^{{commit}}"])
            changed_files = run_git(repository, ["diff-tree", "--no-commit-id", "--name-only", "-r", reference])
            summary = run_git(repository, ["show", "--format=fuller", "--stat", "--no-ext-diff", reference, "--"])
        except RuntimeError as error:
            print(f"Git evidence collection failed: {error}", file=sys.stderr)
            return 1
        lines.extend(
            [
                "",
                "## Source revision",
                "",
                f"`{revision.strip()}`",
                "",
                "## Changed files",
                "",
                "```text",
                truncate(changed_files).rstrip(),
                "```",
                "",
                "## Git summary",
                "",
                "```text",
                truncate(summary).rstrip(),
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Next analysis input",
                "",
                "SSTC feature requests require their Issue body and target-repository instructions",
                "before a semantic impact assessment can be performed.",
            ]
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"IMPACT_REPORT={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
