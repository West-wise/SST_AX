#!/usr/bin/env python3
"""Validate SST-AX task-state files against the repository schema subset."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = REPOSITORY_ROOT / "state" / "task-state.schema.json"


def load_json(path: Path) -> Any:
    """Load a UTF-8 JSON file.

    Args:
        path: JSON file to load.

    Returns:
        Parsed JSON value.
    """
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def is_date_time(value: str) -> bool:
    """Return whether a value is an ISO-8601 date-time string."""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def matches_type(value: Any, expected: str) -> bool:
    """Check the JSON type names used by the task-state schema."""
    return {
        "string": isinstance(value, str),
        "null": value is None,
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(expected, False)


def validate_value(value: Any, schema: dict[str, Any], location: str) -> list[str]:
    """Validate the JSON Schema features used by SST-AX task state.

    This intentionally supports only the keywords present in the repository
    schema; it is not a general JSON Schema implementation.
    """
    errors: list[str] = []
    expected_types = schema.get("type")
    if isinstance(expected_types, str):
        expected_types = [expected_types]
    if expected_types and not any(matches_type(value, item) for item in expected_types):
        return [f"{location}: expected {', '.join(expected_types)}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{location}: must be one of {schema['enum']}")
    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, value) is None:
            errors.append(f"{location}: does not match required pattern")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{location}: must not be empty")
        if schema.get("format") == "date-time" and not is_date_time(value):
            errors.append(f"{location}: must be an ISO-8601 date-time")
        if schema.get("format") == "uri":
            try:
                parsed = urlparse(value)
            except ValueError:
                errors.append(f"{location}: must be an absolute URI")
            else:
                if not parsed.scheme:
                    errors.append(f"{location}: must be an absolute URI")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value):
            errors.append(f"{location}: is below the minimum")
        if value > schema.get("maximum", value):
            errors.append(f"{location}: is above the maximum")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate_value(item, schema["items"], f"{location}[{index}]"))
    return errors


def validate_task_state(state: Any, schema: dict[str, Any]) -> list[str]:
    """Validate one task-state document and return human-readable errors."""
    if not isinstance(state, dict):
        return ["$: task state must be a JSON object"]

    errors: list[str] = []
    properties = schema["properties"]
    for name in schema["required"]:
        if name not in state:
            errors.append(f"$.{name}: required property is missing")
    if schema.get("additionalProperties") is False:
        for name in state:
            if name not in properties:
                errors.append("$: additional properties are not allowed")
    for name, value in state.items():
        if name in properties:
            errors.extend(validate_value(value, properties[name], f"$.{name}"))
    return errors


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_files", nargs="+", type=Path, help="Task-state JSON file(s)")
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help="Task-state schema path",
    )
    return parser.parse_args()


def main() -> int:
    """Validate requested task-state files."""
    args = parse_args()
    try:
        schema = load_json(args.schema)
    except (OSError, json.JSONDecodeError) as error:
        print(f"Schema load failed: {error}", file=sys.stderr)
        return 2

    valid = True
    for task_file in args.task_files:
        try:
            errors = validate_task_state(load_json(task_file), schema)
        except (OSError, json.JSONDecodeError) as error:
            print(f"{task_file}: load failed: {error}", file=sys.stderr)
            valid = False
            continue
        if errors:
            valid = False
            for error in errors:
                print(f"{task_file}: {error}", file=sys.stderr)
        else:
            print(f"VALID {task_file}")
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
