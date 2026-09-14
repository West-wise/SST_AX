#!/usr/bin/env python3
"""Validate local impact results without granting approval or changing state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The validator must not create local-module bytecode artifacts during CLI use.
sys.dont_write_bytecode = True

from impact_validation import ContractError, InputError, load_document, validate_impact


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "CLI_USAGE: check --help and required file arguments\n")


def main() -> int:
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--version", action="version", version="impact-contract 1.0")
    parser.add_argument("--task-file", type=Path, required=True, help="Existing Task state JSON")
    parser.add_argument("--manifest-file", type=Path, required=True, help="Trusted input manifest JSON")
    parser.add_argument("--result-file", type=Path, required=True, help="Untrusted analysis result JSON")
    args = parser.parse_args()
    try:
        task = load_document(args.task_file)
        manifest = load_document(args.manifest_file)
        result = load_document(args.result_file)
        errors = validate_impact(task, manifest, result)
    except ContractError as error:
        print(error, file=sys.stderr)
        return 1
    except InputError as error:
        print(error, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("INTERRUPTED: validation cancelled", file=sys.stderr)
        return 130
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"VALID_IMPACT={result['change_required']}")
    print("AUTHORIZATION=NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
