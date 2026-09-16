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
                        help="Verify Slack/checkpoint binding and continue the exact session read-only")
    args = parser.parse_args()
    from codex_impact import run
    try:
        outcome = run(args.task_file, args.input_directory, args.codex, args.resume_approved)
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
