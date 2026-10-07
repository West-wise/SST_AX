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
    parser.add_argument("--resume", action="store_true", help="Resume the verified same Task/session/worktree within budget")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--reconcile-interrupted", action="store_true",
                          help="Reconcile a RUNNING receipt only after its recorded process stopped; do not run Codex")
    recovery.add_argument("--abort-interrupted", action="store_true",
                          help="Terminate an uncertain RUNNING Task while preserving its worktree")
    parser.add_argument("--observed-active-seconds", type=float, help="Observed cumulative active seconds after a crash")
    parser.add_argument("--observed-write-steps", type=int, help="Observed cumulative write/shell steps after a crash")
    parser.add_argument("--legacy-active-seconds", type=float, help="Observed total active time for a legacy receipt without execution checkpoint")
    parser.add_argument("--legacy-write-steps", type=int, help="Observed legacy write/shell steps; never infer zero from missing events")
    args = parser.parse_args()
    from sstc_worker import SAFE_FAILURES, reconcile_interrupted, run
    try:
        if args.reconcile_interrupted or args.abort_interrupted:
            if args.resume or args.legacy_active_seconds is not None or args.legacy_write_steps is not None:
                raise ValueError("RECOVERY_ARGUMENTS_REFUSED")
            outcome = reconcile_interrupted(args.task_file, args.input_directory,
                                            active_seconds=args.observed_active_seconds,
                                            write_steps=args.observed_write_steps, abort=args.abort_interrupted,
                                            worktree=args.worktree, branch=args.branch)
        else:
            if args.observed_active_seconds is not None or args.observed_write_steps is not None:
                raise ValueError("RECOVERY_ARGUMENTS_REFUSED")
            outcome = run(args.task_file, args.sstc_repository, args.worktree, args.branch,
                          args.input_directory, args.codex, args.validation_mode, args.resume,
                          args.legacy_active_seconds, args.legacy_write_steps)
    except KeyboardInterrupt:
        print("SSTC_WORKER=INTERRUPTED", file=sys.stderr); return 130
    except Exception as error:
        codes = SAFE_FAILURES | {"RECOVERY_ARGUMENTS_REFUSED"}
        code = str(error) if isinstance(error, ValueError) and str(error) in codes else "CONTEXT_OR_EXECUTION_FAILED"
        print("SSTC_WORKER_ERROR=" + code, file=sys.stderr); return 2
    print("SSTC_WORKER=" + outcome)
    print("PUSH_AUTHORIZATION=NONE")
    print("PR_AUTHORIZATION=NONE")
    if outcome == "INTERRUPTED":
        return 130
    return 0 if outcome == "IMPLEMENTED" or args.reconcile_interrupted or args.abort_interrupted else 1

if __name__ == "__main__":
    raise SystemExit(main())
