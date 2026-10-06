#!/usr/bin/env python3
"""Publish an approved SSTC candidate and produce a Draft PR after remote validation."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "SSTC_PIPELINE_ERROR=INVALID_ARGUMENTS; see --help\n")


def main(argv=None) -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    publish = commands.add_parser("publish", allow_abbrev=False)
    publish.add_argument("--task-file", type=Path, required=True)
    publish.add_argument("--input-directory", type=Path, required=True)
    check = commands.add_parser("check", allow_abbrev=False)
    check.add_argument("--task-file", type=Path, required=True)
    args = parser.parse_args(argv)
    from sstc_pipeline import check as poll, publish as start
    try:
        record = (start(args.task_file, args.input_directory) if args.command == "publish"
                  else poll(args.task_file))
    except KeyboardInterrupt:
        print("SSTC_PIPELINE=INTERRUPTED", file=sys.stderr)
        return 130
    except Exception:
        print("SSTC_PIPELINE_ERROR=APPROVAL_CONTEXT_PUBLICATION_OR_VALIDATION_CHECK_FAILED", file=sys.stderr)
        return 2
    print("SSTC_PIPELINE=" + record["outcome"])
    print("CANDIDATE_SHA=" + str(record["candidate_sha"]))
    if record.get("run_id"):
        print("RUN_ID=" + str(record["run_id"]))
    if record.get("draft_pr_url"):
        print("DRAFT_PR=" + record["draft_pr_url"])
    return 1 if record["outcome"] == "BUILD_FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
