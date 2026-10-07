#!/usr/bin/env python3
"""Request/check Task-bound SSTC builds without changing Task or approval authority."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "GITHUB_VALIDATION_ERROR=INVALID_ARGUMENTS; see --help\n")


def main(argv=None) -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    request = commands.add_parser("request", allow_abbrev=False)
    request.add_argument("--task-file", type=Path, required=True)
    request.add_argument("--candidate-sha", required=True)
    check = commands.add_parser("check", allow_abbrev=False)
    check.add_argument("--task-file", type=Path, required=True)
    reconcile = commands.add_parser("reconcile", allow_abbrev=False)
    reconcile.add_argument("--task-file", type=Path, required=True)
    reconcile.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)
    from github_validation import ValidationError, check_validation, request_validation
    try:
        if args.command == "request":
            record = request_validation(args.task_file, args.candidate_sha)
        elif args.command == "reconcile":
            record = check_validation(args.task_file, reconcile_run_id=args.run_id)
        else:
            record = check_validation(args.task_file)
    except KeyboardInterrupt:
        print("GITHUB_VALIDATION=INTERRUPTED", file=sys.stderr)
        return 130
    except ValidationError as error:
        print("GITHUB_VALIDATION_ERROR=" + str(error), file=sys.stderr)
        return 2
    except Exception:
        print("GITHUB_VALIDATION_ERROR=CONFIGURATION_TASK_API_OR_JSON_CHECK_FAILED", file=sys.stderr)
        return 2
    print("GITHUB_VALIDATION=" + record["outcome"])
    print("RUN_ID=" + str(record["run_id"]))
    print("AUTHORIZATION=NONE")
    return 1 if record["outcome"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
