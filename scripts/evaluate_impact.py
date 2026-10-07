#!/usr/bin/env python3
"""Compare an analysis with human-labelled semantic cases; never authorize work."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from impact_validation import SCHEMA_PATH, load_document, validate_shape


def evaluate(case: dict, result: dict) -> list[str]:
    schema = load_document(SCHEMA_PATH)
    if validate_shape(result, schema, schema, "result"):
        return ["RESULT_SCHEMA"]
    expected = case["expected"]
    failures = []
    if result["source_type"] != expected["source_type"]:
        failures.append("SOURCE_TYPE")
    if result["change_required"] != expected["change_required"]:
        failures.append("CHANGE_REQUIRED")
    for key, status in expected["impacts"].items():
        if result["impacts"][key]["status"] != status:
            failures.append("IMPACT_" + key.upper())
    if not set(expected["approval_reasons"]).issubset(result["approval_reasons"]):
        failures.append("APPROVAL_REASONS")
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--case-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        failures = evaluate(load_document(args.case_file), load_document(args.result_file))
        print("SEMANTIC_EVALUATION=" + ("MISMATCH" if failures else "MATCH"))
        for code in failures:
            print(code)
        print("AUTHORIZATION=NONE")
        return 1 if failures else 0
    except (OSError, ValueError, KeyError, TypeError):
        print("SEMANTIC_EVALUATION_ERROR: invalid case or result", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
