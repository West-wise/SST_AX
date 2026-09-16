"""Read-only impact contract checks, not semantic analysis or authorization.

The schema walker supports only the keywords in impact-analysis.schema.json.
Cross-field rules below are additionally required; this is not a general JSON
Schema implementation. Neither evidence paths nor URLs are dereferenced.
"""

from __future__ import annotations

import json
import re
from ipaddress import IPv6Address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from validate_task_state import DEFAULT_SCHEMA_PATH, validate_task_state

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "impact-analysis.schema.json"
MAX_BYTES = 1_048_576
MAX_DEPTH = 32
APPROVALS = {
    "ui_ux": "UI_CHANGE",
    "protocol_contract": "PROTOCOL_CHANGE",
    "dependency": "DEPENDENCY_CHANGE",
    "android_permission": "ANDROID_PERMISSION_CHANGE",
    "destructive_action": "DESTRUCTIVE_ACTION",
    "sstd_change_required": "SSTD_CHANGE_REQUIRED",
}


class InputError(ValueError):
    """Unreadable, malformed, or over-budget input; messages contain no raw input."""


class ContractError(ValueError):
    """Ambiguous JSON object rejected before schema validation."""


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("DUPLICATE_KEY: JSON object")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise InputError("INVALID_JSON: non-JSON numeric constant")


def bounded_integer(value: str) -> int:
    # Python 3.10 lacks the default integer-string limit of newer Python versions.
    if len(value.lstrip("-")) > 4300:
        raise InputError("INPUT_NUMBER: exceeds 4300 digits")
    return int(value)


def load_document(path: Path) -> Any:
    """Read at most 1 MiB and reject nesting before invoking the JSON parser."""
    try:
        if not path.is_file():
            raise InputError("INPUT_READ: expected a regular file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        return decode_document(raw)
    except (ContractError, InputError):
        raise
    except (OSError, ValueError, RecursionError) as error:
        raise InputError("INPUT_READ_OR_JSON: cannot decode input") from error


def decode_document(raw: bytes) -> Any:
    """Apply the same JSON limits to an in-memory Worker response before saving it."""
    try:
        if len(raw) > MAX_BYTES:
            raise InputError("INPUT_SIZE: exceeds 1 MiB")
        text = raw.decode("utf-8")
        depth = 0
        quoted = escaped = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > MAX_DEPTH:
                    raise InputError("INPUT_DEPTH: exceeds 32 containers")
            elif character in "]}":
                depth -= 1
        return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant,
                          parse_int=bounded_integer)
    except (ContractError, InputError):
        raise
    except (OSError, ValueError, RecursionError) as error:
        raise InputError("INPUT_READ_OR_JSON: cannot decode input") from error


def validate_shape(value: Any, schema: dict[str, Any], root: dict[str, Any],
                   location: str) -> list[str]:
    """Apply the local schema subset without echoing untrusted keys or values."""
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].removeprefix("#/$defs/")]
    expected = schema.get("type", [])
    expected = [expected] if isinstance(expected, str) else expected
    actual = {str: "string", dict: "object", list: "array", bool: "boolean",
              type(None): "null"}.get(type(value))
    if actual not in expected:
        return [f"TYPE: {location}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"ENUM: {location}")
    if isinstance(value, dict):
        properties = schema["properties"]
        for name in schema["required"]:
            if name not in value:
                errors.append(f"REQUIRED: {location}.{name}")
        if value.keys() - properties.keys():
            errors.append(f"EXTRA_PROPERTY: {location}")
        for name in properties.keys() & value.keys():
            errors.extend(validate_shape(value[name], properties[name], root, f"{location}.{name}"))
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", MAX_BYTES):
            return [f"ARRAY_SIZE: {location}"]
        if schema.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
            errors.append(f"DUPLICATE_ITEM: {location}")
        for index, item in enumerate(value):
            errors.extend(validate_shape(item, schema["items"], root, f"{location}[{index}]"))
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"STRING_SIZE: {location}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"PATTERN: {location}")
    return sorted(errors)


def safe_relative_path(path: str) -> bool:
    """Portable slash-separated evidence paths; never used for filesystem I/O."""
    return not (
        any(ord(character) < 32 for character in path)
        or "\\" in path or ":" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
    )


def validate_impact(task: Any, manifest: Any, result: Any) -> list[str]:
    """Validate structure and declared consistency; never advance task state."""
    schema = load_document(SCHEMA_PATH)
    task_schema = load_document(DEFAULT_SCHEMA_PATH)
    try:
        task_errors = validate_task_state(task, task_schema)
        # Older urllib accepts arbitrary bracketed hosts; newer versions raise.
        # Make this boundary deterministic without changing the legacy Task CLI.
        if isinstance(task, dict) and isinstance(task.get("draft_pr_url"), str):
            parsed = urlsplit(task["draft_pr_url"])
            if "[" in parsed.netloc:
                host = parsed.hostname or ""
                if not re.fullmatch(r"v[0-9a-fA-F]+\..+", host):
                    IPv6Address(host)
    except (ValueError, TypeError, OverflowError):
        task_errors = ["Invalid Task value"]
    errors = ["TASK_SCHEMA: task"] if task_errors else []
    errors.extend(validate_shape(manifest, schema["$defs"]["manifest"], schema, "manifest"))
    errors.extend(validate_shape(result, schema, schema, "result"))
    if errors:
        return sorted(errors)
    for name in ("task_id", "source_type"):
        for label, document in (("manifest", manifest), ("result", result)):
            if document[name] != task[name]:
                errors.append(f"INPUT_MISMATCH: {label}.{name} != task.{name}")
    context = manifest["input_context"]
    if context != result["input_context"] or context["source_reference"] != task["source_reference"]:
        errors.append("INPUT_MISMATCH: input_context")
    is_sstd = task["source_type"] == "SSTD_CHANGE"
    prefix = "sstd-sync-" if is_sstd else "sstc-feature-"
    if not task["task_id"].startswith(prefix):
        errors.append("INPUT_MISMATCH: task_id prefix")
    if is_sstd and context["request_sha256"] is not None:
        errors.append("SOURCE_CONTEXT: request_sha256")
    if not is_sstd and any(context[name] is not None for name in ("sstd_revision", "sstd_base_revision")):
        errors.append("SOURCE_CONTEXT: sstd revisions")
    incomplete = context["sstc_revision"] is None or context[
        "sstd_revision" if is_sstd else "request_sha256"
    ] is None
    evidence: dict[str, dict[str, Any]] = {}
    mandatory = {"source_diff" if is_sstd else "request", "sstc_context", "target_instructions"}
    available_kinds: set[str] = set()
    for index, item in enumerate(manifest["evidence"]):
        loc = f"manifest.evidence[{index}]"
        if item["evidence_id"] in evidence:
            errors.append(f"DUPLICATE_EVIDENCE: {loc}")
        evidence[item["evidence_id"]] = item
        if item["required"] and (item["missing"] or item["truncated"]):
            incomplete = True
        if item["required"] and not item["missing"] and not item["truncated"]:
            available_kinds.add(item["kind"])
        if item["missing"] != (item["content_sha256"] is None):
            errors.append(f"EVIDENCE_CONTENT: {loc}")
        if item["path"] is not None and not safe_relative_path(item["path"]):
            errors.append(f"EVIDENCE_PATH: {loc}.path")
        expected_source = {"source_diff": "SSTD", "request": "REQUEST",
                           "sstc_context": "SSTC", "target_instructions": "SSTC"}.get(item["kind"])
        if expected_source and item["source"] != expected_source:
            errors.append(f"EVIDENCE_SOURCE: {loc}")
        if item["source"] == "REQUEST":
            if (is_sstd or item["revision"] is not None or item["path"] is not None
                    or item["request_sha256"] != context["request_sha256"]):
                errors.append(f"EVIDENCE_INPUT: {loc}")
            if not item["missing"] and item["content_sha256"] != context["request_sha256"]:
                errors.append(f"EVIDENCE_HASH: {loc}")
        else:
            revisions = {context["sstc_revision"]} if item["source"] == "SSTC" else {
                context["sstd_revision"], context["sstd_base_revision"]}
            if (item["request_sha256"] is not None or item["revision"] not in revisions
                    or (not item["missing"] and item["revision"] is None)):
                errors.append(f"EVIDENCE_INPUT: {loc}")
            if item["kind"] != "source_diff" and item["path"] is None:
                errors.append(f"EVIDENCE_PATH: {loc}.path")
    incomplete = incomplete or not mandatory.issubset(available_kinds)
    declared: set[str] = set()
    for index, item in enumerate(result["evidence"]):
        evidence_id = item["evidence_id"]
        if evidence_id in declared:
            errors.append(f"DUPLICATE_EVIDENCE: result.evidence[{index}]")
        declared.add(evidence_id)
        if evidence_id not in evidence:
            errors.append(f"EVIDENCE_REFERENCE: result.evidence[{index}]")
        elif evidence[evidence_id]["missing"] or evidence[evidence_id]["truncated"]:
            incomplete = True
    definite = result["change_required"] != "UNDETERMINED"
    if definite and not declared:
        errors.append("EVIDENCE_REQUIRED: result.evidence")
    required_reasons: set[str] = set()
    for name, reason in APPROVALS.items():
        impact = result["impacts"][name]
        if not set(impact["evidence_ids"]).issubset(declared):
            errors.append(f"EVIDENCE_REFERENCE: result.impacts.{name}")
        if definite and not impact["evidence_ids"]:
            errors.append(f"EVIDENCE_REQUIRED: result.impacts.{name}")
        if impact["status"] == "UNKNOWN":
            incomplete = True
        if impact["status"] == "PRESENT":
            required_reasons.add(reason)
        elif impact["status"] == "ABSENT" and reason in result["approval_reasons"]:
            errors.append(f"APPROVAL_CONTRADICTION: result.impacts.{name}")
    if result["risk_level"] in ("HIGH", "CRITICAL"):
        required_reasons.add(result["risk_level"] + "_RISK")
    if not required_reasons.issubset(result["approval_reasons"]):
        errors.append("APPROVAL_MISSING: result.approval_reasons")
    for level in ("HIGH", "CRITICAL"):
        if level + "_RISK" in result["approval_reasons"] and result["risk_level"] != level:
            errors.append("APPROVAL_CONTRADICTION: result.risk_level")
    if (incomplete or result["unresolved_questions"]) and definite:
        errors.append("UNDETERMINED_REQUIRED: result.change_required")
    if not definite and not result["unresolved_questions"]:
        errors.append("QUESTION_REQUIRED: result.unresolved_questions")
    if result["change_required"] == "NOT_REQUIRED" and (
        result["risk_level"] != "NONE" or result["approval_reasons"]
        or any(item["status"] != "ABSENT" for item in result["impacts"].values())
    ):
        errors.append("NO_CHANGE_CONTRADICTION: result.change_required")
    return sorted(set(errors))
