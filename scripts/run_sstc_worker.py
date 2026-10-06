#!/usr/bin/env python3
"""Run an approved SSTC Codex worker in a new branch/worktree."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True

class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "SSTC_WORKER_ERROR: invalid arguments; see --help\n")

def main() -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--sstc-repository", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--input-directory", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--validation-mode", choices=("local", "github"), default="local",
                        help="local runs Gradle; github leaves build/test to the Controller")
    args = parser.parse_args()
    from sstc_worker import run
    try:
        outcome = run(args.task_file, args.sstc_repository, args.worktree, args.branch,
                      args.input_directory, args.codex, args.validation_mode)
    except KeyboardInterrupt:
        print("SSTC_WORKER=INTERRUPTED", file=sys.stderr); return 130
    except Exception:
        print("SSTC_WORKER_ERROR: approval, repository, worktree, Codex, or validation check failed", file=sys.stderr); return 2
    print("SSTC_WORKER=" + outcome)
    print("PUSH_AUTHORIZATION=NONE")
    print("PR_AUTHORIZATION=NONE")
    return 0 if outcome in {"VALIDATED", "IMPLEMENTED"} else 1

if __name__ == "__main__":
    raise SystemExit(main())
