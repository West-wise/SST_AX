#!/usr/bin/env python3
"""Collect pinned impact inputs; never authorize or update Task state.

Exit 0: collected; 1: incomplete bundle; 2: rejected; 130: interrupted.
Output: manifest.json and evidence/e0001.txt etc., in stable sorted order.
Limits: 64 KiB/evidence, 1 MiB retained total, 128 records.
Manual context selection does not prove semantic sufficiency.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
VERSION = "1.0.0"


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "COLLECTION_ERROR: invalid arguments; see --help\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--sstc-repository", type=Path, required=True)
    parser.add_argument("--sstc-revision", required=True)
    parser.add_argument("--sstc-context", action="append", required=True, help="Exact file; repeatable")
    parser.add_argument("--output-directory", type=Path, required=True, help="New directory; parent must exist")
    parser.add_argument("--source-repository", type=Path, help="SSTD_CHANGE only")
    parser.add_argument("--sstd-base", help="SSTD_CHANGE: explicit commit/ref or ROOT")
    parser.add_argument("--sstd-path", action="append", help="SSTD_CHANGE: exact file; repeatable")
    parser.add_argument("--sstd-context", action="append", help="SSTD_CHANGE: pinned supporting source; repeatable")
    parser.add_argument("--request-file", type=Path, help="SSTC_FEATURE: UTF-8 snapshot <=64 KiB")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from impact_collection import collect
    try:
        complete = collect(args)
    except KeyboardInterrupt:
        print("COLLECTION_INTERRUPTED", file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, OverflowError, RuntimeError):
        print("COLLECTION_ERROR: input, security, Git, budget, or output check failed", file=sys.stderr)
        return 2
    print("COLLECTION=" + ("COMPLETE" if complete else "INCOMPLETE"))
    print("SELECTION_LIMITATION=manual context selection is not proof of semantic sufficiency")
    print("AUTHORIZATION=NONE")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
