#!/usr/bin/env python3
"""Run one read-only Codex impact turn. No implementation or Slack transmission."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import subprocess

sys.dont_write_bytecode = True


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "CODEX_IMPACT_ERROR: invalid arguments; see --help\n")


def main() -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--input-directory", type=Path, required=True)
    parser.add_argument("--codex", default="codex", help="Trusted Codex executable")
    parser.add_argument("--resume-approved", action="store_true",
                        help="Deprecated: resume approved implementation through run_sstc_worker.py")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--reconcile-interrupted", action="store_true",
                          help="Verify the recorded process has stopped; do not call Codex")
    recovery.add_argument("--abort-interrupted", action="store_true",
                          help="Close an uncertain RUNNING analysis as ANALYSIS_FAILED")
    parser.add_argument("--observed-active-seconds", type=float,
                        help="Actual cumulative active time including the interrupted turn")
    parser.add_argument("--observed-write-steps", type=int,
                        help="Actual cumulative write count; read-only analysis normally has zero")
    args = parser.parse_args()
    if args.resume_approved:
        print("CODEX_IMPACT_ERROR: use run_sstc_worker.py after approval; reanalysis requires a new Task",
              file=sys.stderr)
        return 2
    if (args.observed_active_seconds is not None or args.observed_write_steps is not None) and not args.reconcile_interrupted:
        parser.error("Observation requires reconciliation")
    if args.reconcile_interrupted and (args.observed_active_seconds is None or args.observed_write_steps is None):
        parser.error("Actual budget observations required")
    from codex_impact import run, reconcile_interrupted
    try:
        if args.reconcile_interrupted or args.abort_interrupted:
            outcome = reconcile_interrupted(args.task_file, args.input_directory,
                        abort=args.abort_interrupted, active_seconds=args.observed_active_seconds,
                        write_steps=args.observed_write_steps)
        else:
            outcome = run(args.task_file, args.input_directory, args.codex)
    except KeyboardInterrupt:
        print("CODEX_IMPACT=INTERRUPTED", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, subprocess.TimeoutExpired):
        print("CODEX_IMPACT_ERROR: input, state, approval, or executable check failed", file=sys.stderr)
        return 2
    print("CODEX_IMPACT=" + outcome)
    print("AUTHORIZATION=NONE")
    return 0 if outcome == "VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
