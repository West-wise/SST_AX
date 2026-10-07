#!/usr/bin/env python3
"""Collect read-only Git evidence for an SST-AX task impact review."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from impact_collection import Git, check_content
from impact_validation import load_document
from validate_task_state import DEFAULT_SCHEMA_PATH, validate_task_state

MAX_EVIDENCE_LENGTH = 30_000


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "IMPACT_REPORT_ERROR=INVALID_ARGUMENTS; see --help\n")


def run_git(repository: Git, arguments: list[str]) -> str:
    """Run a read-only Git command and return its standard output.

    Args:
        repository: Local Git repository to inspect.
        arguments: Git arguments after the executable name.

    Returns:
        Command standard output.

    Raises:
        ValueError: If bounded Git evidence is unavailable or contains a recognizable secret.
    """
    raw = repository.run(arguments)
    check_content(raw)
    return raw.decode("utf-8")


def truncate(value: str) -> str:
    """Bound report size while retaining evidence that it was truncated."""
    if len(value) <= MAX_EVIDENCE_LENGTH:
        return value
    return f"{value[:MAX_EVIDENCE_LENGTH]}\n\n[truncated by SST-AX]\n"


def parse_args() -> argparse.Namespace:
    """Parse impact analysis arguments."""
    parser = SafeParser(description=__doc__, allow_abbrev=False)
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
        task_state = load_document(args.task_file)
        errors = validate_task_state(task_state, load_document(DEFAULT_SCHEMA_PATH))
    except (OSError, ValueError):
        print("IMPACT_REPORT_ERROR=TASK_READ", file=sys.stderr)
        return 2
    if errors:
        print("IMPACT_REPORT_ERROR=TASK_SCHEMA", file=sys.stderr)
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
            print("IMPACT_REPORT_ERROR=SOURCE_REPOSITORY_REQUIRED", file=sys.stderr)
            return 2
        reference = task_state["source_reference"]
        try:
            repository = Git(args.source_repository)
            revision = repository.resolve(reference)
            changed_files = run_git(repository, ["diff-tree", "--no-commit-id", "--name-only", "-r", revision])
            summary = run_git(repository, ["show", "--format=fuller", "--stat", "--no-ext-diff", revision, "--"])
        except (OSError, ValueError):
            print("IMPACT_REPORT_ERROR=GIT_EVIDENCE", file=sys.stderr)
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

    try:
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        check_content(raw)
        report_path.write_bytes(raw)
    except (OSError, ValueError):
        print("IMPACT_REPORT_ERROR=REPORT_REJECTED_OR_UNWRITABLE", file=sys.stderr)
        return 1
    print(f"IMPACT_REPORT={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
