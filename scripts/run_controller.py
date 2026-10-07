#!/usr/bin/env python3
"""Poll SSTD changes and [AX] SSTC Issues with persisted, policy-bound execution."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "CONTROLLER_ERROR=INVALID_ARGUMENTS; see --help\n")


def main(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="One intake/advance/approval tick")
    args = parser.parse_args(argv)
    try:
        from sst_ax_controller import configuration, run_once
        config = configuration(args.config)
        while True:
            result = run_once(config)
            for task_id, status in result["outcomes"].items():
                print("TASK=" + task_id + " STATUS=" + status, flush=True)
            if result["approval_error"]:
                print("CONTROLLER_APPROVAL=CONFIGURATION_OR_TRANSPORT_REQUIRED", file=sys.stderr)
            if args.once:
                return 0
            # Short waits keep termination responsive even with a long poll interval.
            for _ in range(config["poll_seconds"]):
                time.sleep(1)
    except KeyboardInterrupt:
        print("CONTROLLER=STOPPED")
        return 130
    except Exception:
        print("CONTROLLER_ERROR=CONFIGURATION_INPUT_OR_EXECUTION_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
