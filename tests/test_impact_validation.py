"""Contract, input-boundary, and read-only tests using synthetic impact bundles."""

from __future__ import annotations

import builtins
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"
FIXTURES_DIRECTORY = Path(__file__).parent / "fixtures" / "impact"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from impact_validation import (
    ContractError,
    InputError,
    MAX_BYTES,
    MAX_DEPTH,
    load_document,
    validate_impact,
)


FIXTURE_NAMES = tuple(
    f"{source}-{outcome}"
    for source in ("sstd", "sstc")
    for outcome in ("required", "not-required", "undetermined")
)
APPROVAL_REASONS = {
    "ui_ux": "UI_CHANGE",
    "protocol_contract": "PROTOCOL_CHANGE",
    "dependency": "DEPENDENCY_CHANGE",
    "android_permission": "ANDROID_PERMISSION_CHANGE",
    "destructive_action": "DESTRUCTIVE_ACTION",
    "sstd_change_required": "SSTD_CHANGE_REQUIRED",
}
MARKER = "SYNTHETIC_PRIVATE_MARKER_9371"


def bundle(name: str = "sstc-not-required") -> dict:
    return json.loads((FIXTURES_DIRECTORY / f"{name}.json").read_text(encoding="utf-8"))


def validate(documents: dict) -> list[str]:
    return validate_impact(documents["task"], documents["manifest"], documents["result"])


def defer(documents: dict) -> None:
    documents["result"]["change_required"] = "UNDETERMINED"
    documents["result"]["unresolved_questions"] = ["Can the missing context be supplied?"]


class ImpactContractTest(unittest.TestCase):
    def assert_rejected(self, documents: dict) -> list[str]:
        errors = validate(documents)
        self.assertIsInstance(errors, list)
        self.assertTrue(errors, "Invalid impact bundle was accepted")
        self.assertTrue(all(isinstance(error, str) for error in errors))
        return errors

    def test_six_synthetic_bundles_are_valid_and_do_not_mutate_inputs(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                documents = bundle(name)
                original = copy.deepcopy(documents)
                self.assertEqual(validate(documents), [])
                self.assertEqual(validate(documents), [])
                self.assertEqual(documents, original)

    def test_each_result_identity_must_match_the_fixed_inputs(self) -> None:
        for source in ("sstd", "sstc"):
            for field, replacement in (
                ("task_id", "sstc-feature-20260912-9999"),
                ("source_type", "SSTC_FEATURE" if source == "sstd" else "SSTD_CHANGE"),
                ("source_reference", "another-synthetic-request"),
                ("sstc_revision", "1" * 40),
                ("sstd_revision", "2" * 40),
                ("sstd_base_revision", "3" * 40),
                ("request_sha256", "4" * 64),
            ):
                with self.subTest(source=source, field=field):
                    documents = bundle(f"{source}-required")
                    target = documents["result"]
                    if field not in ("task_id", "source_type"):
                        target = target["input_context"]
                    target[field] = replacement
                    original = copy.deepcopy(documents)
                    self.assert_rejected(documents)
                    self.assertEqual(documents, original)

    def test_matching_manifest_and_result_cannot_override_task_identity(self) -> None:
        for field, value in (
            ("task_id", "sstc-feature-20260912-9999"),
            ("source_type", "SSTD_CHANGE"),
            ("source_reference", "another-synthetic-request"),
        ):
            with self.subTest(field=field):
                documents = bundle()
                for name in ("manifest", "result"):
                    target = documents[name]
                    if field == "source_reference":
                        target = target["input_context"]
                    target[field] = value
                self.assert_rejected(documents)

    def test_task_schema_remains_required(self) -> None:
        for field, value in (("attempt", True), ("attempt", 4), ("status", "APPROVED"),
                             ("created_at", "not-a-date"), ("branch", "main")):
            with self.subTest(field=field, value=value):
                documents = bundle()
                documents["task"][field] = value
                self.assert_rejected(documents)
        documents = bundle()
        del documents["task"]["updated_at"]
        self.assert_rejected(documents)

    def test_nested_schema_errors_are_rejected_without_echoing_values(self) -> None:
        cases = (
            ("result", (), "schema_version", "2.0"),
            ("manifest", (), "schema_version", "2.0"),
            ("result", (), "summary", " \t\n"),
            ("result", (), "change_required", MARKER),
            ("result", ("impacts", "ui_ux"), "status", MARKER),
            ("result", ("impacts", "ui_ux"), "reason", " \t\n"),
            ("result", ("impacts", "ui_ux"), "evidence_ids", "source"),
            ("result", ("evidence", 0), "reason", " \t\n"),
            ("manifest", ("evidence", 0), "required", 1),
            ("manifest", ("evidence", 0), "content_sha256", "z" * 64),
            ("manifest", ("input_context",), "sstc_revision", "c" * 39),
            ("result", (), "unresolved_questions", [" \t\n"]),
            ("result", (), "approval_reasons", ["UI_CHANGE", "UI_CHANGE"]),
        )
        for name, path, field, value in cases:
            with self.subTest(name=name, path=path, field=field):
                documents = bundle()
                target = documents[name]
                for component in path:
                    target = target[component]
                target[field] = value
                self.assertNotIn(MARKER, "\n".join(self.assert_rejected(documents)))
        for name, path, field in (
            ("manifest", ("input_context",), "sstc_revision"),
            ("manifest", ("evidence", 0), "missing"),
            ("result", ("impacts", "ui_ux"), "reason"),
            ("result", ("evidence", 0), "evidence_id"),
        ):
            with self.subTest(missing=field, name=name):
                documents = bundle()
                target = documents[name]
                for component in path:
                    target = target[component]
                del target[field]
                self.assert_rejected(documents)

    def test_unknown_field_names_are_not_disclosed_even_in_task_errors(self) -> None:
        for name, path in (("task", ()), ("manifest", ()), ("result", ()),
                           ("manifest", ("evidence", 0)),
                           ("result", ("impacts", "ui_ux"))):
            with self.subTest(name=name, path=path):
                documents = bundle()
                target = documents[name]
                for component in path:
                    target = target[component]
                target[MARKER + "\nFORGED_DIAGNOSTIC"] = MARKER
                errors = "\n".join(self.assert_rejected(documents))
                self.assertNotIn(MARKER, errors)
                self.assertNotIn("FORGED_DIAGNOSTIC", errors)

    def test_malformed_task_url_returns_safe_schema_error_without_mutating_inputs(self) -> None:
        documents = bundle()
        documents["task"]["draft_pr_url"] = f"https://[{MARKER}]/"
        original = copy.deepcopy(documents)
        self.assertEqual(validate(documents), ["TASK_SCHEMA: task"])
        self.assertEqual(documents, original)

    def test_oversized_arrays_produce_one_bounded_diagnostic_not_per_item_errors(self) -> None:
        documents = bundle("sstc-undetermined")
        documents["result"]["unresolved_questions"] = [f"Synthetic question {i}?" for i in range(256)]
        self.assertEqual(validate(documents), [])
        for name, path, field, location in (
            ("manifest", (), "evidence", "manifest.evidence"),
            ("result", (), "evidence", "result.evidence"),
            ("result", (), "approval_reasons", "result.approval_reasons"),
            ("result", (), "unresolved_questions", "result.unresolved_questions"),
            ("result", ("impacts", "ui_ux"), "evidence_ids", "result.impacts.ui_ux.evidence_ids"),
        ):
            with self.subTest(location=location):
                documents = bundle()
                target = documents[name]
                for component in path:
                    target = target[component]
                # Bad duplicate items would amplify diagnostics if traversed individually.
                target[field] = [{MARKER: MARKER}] * 257
                self.assertEqual(validate(documents), [f"ARRAY_SIZE: {location}"])

    def test_trailing_newline_cannot_bypass_identity_patterns_when_references_agree(self) -> None:
        for field in ("task_id", "evidence_id", "sstc_revision", "sstd_revision",
                      "sstd_base_revision", "request_sha256", "content_sha256"):
            with self.subTest(field=field):
                documents = bundle("sstd-required" if field.startswith("sstd_") else "sstc-required")
                manifest, result = documents["manifest"], documents["result"]
                # Keep related values aligned so rejection tests syntax, not mismatched inputs.
                if field == "task_id":
                    for name in ("task", "manifest", "result"):
                        documents[name][field] += "\n"
                elif field == "evidence_id":
                    manifest["evidence"][0][field] += "\n"
                    result["evidence"][0][field] += "\n"
                    for impact in result["impacts"].values():
                        impact["evidence_ids"] = [
                            reference + "\n" if reference == "source" else reference
                            for reference in impact["evidence_ids"]
                        ]
                elif field == "content_sha256":
                    manifest["evidence"][1][field] += "\n"
                else:
                    old_value = manifest["input_context"][field]
                    for document in (manifest, result):
                        document["input_context"][field] += "\n"
                    for item in manifest["evidence"]:
                        evidence_field = "request_sha256" if field == "request_sha256" else "revision"
                        if item[evidence_field] == old_value:
                            item[evidence_field] += "\n"
                            if field == "request_sha256":
                                item["content_sha256"] += "\n"
                errors = self.assert_rejected(documents)
                self.assertTrue(any(error.startswith("PATTERN:") for error in errors), errors)

    def test_definitive_results_require_declared_evidence_for_every_impact(self) -> None:
        for outcome in ("required", "not-required"):
            for failure in ("no_evidence", "unknown_evidence", "undeclared_impact", "empty_impact"):
                with self.subTest(outcome=outcome, failure=failure):
                    documents = bundle(f"sstc-{outcome}")
                    result = documents["result"]
                    if failure == "no_evidence":
                        result["evidence"] = []
                    elif failure == "unknown_evidence":
                        result["evidence"][0]["evidence_id"] = "not-in-manifest"
                    elif failure == "undeclared_impact":
                        result["evidence"] = result["evidence"][1:]
                    else:
                        result["impacts"]["dependency"]["evidence_ids"] = []
                    self.assert_rejected(documents)

    def test_duplicate_evidence_ids_and_impact_references_are_rejected(self) -> None:
        for name in ("manifest", "result", "impact"):
            with self.subTest(name=name):
                documents = bundle()
                items = (documents["result"]["impacts"]["dependency"]["evidence_ids"]
                         if name == "impact" else documents[name]["evidence"])
                items.append(copy.deepcopy(items[0]))
                self.assert_rejected(documents)

    def test_evidence_is_bound_to_source_revision_and_request_snapshot(self) -> None:
        for source, index, field, value in (
            ("sstd", 0, "revision", "1" * 40),
            ("sstd", 0, "request_sha256", "1" * 64),
            ("sstc", 1, "revision", "1" * 40),
            ("sstc", 0, "request_sha256", "1" * 64),
            ("sstc", 0, "revision", "1" * 40),
            ("sstc", 0, "path", "request.md"),
            ("sstc", 1, "source", "SSTD"),
        ):
            with self.subTest(source=source, index=index, field=field):
                documents = bundle(f"{source}-required")
                documents["manifest"]["evidence"][index][field] = value
                self.assert_rejected(documents)

    def test_initial_commit_and_deleted_file_base_revision_are_valid(self) -> None:
        for initial_commit in (True, False):
            with self.subTest(initial_commit=initial_commit):
                documents = bundle("sstd-required")
                if initial_commit:
                    for name in ("manifest", "result"):
                        documents[name]["input_context"]["sstd_base_revision"] = None
                else:
                    item = documents["manifest"]["evidence"][0]
                    item["path"] = "src/deleted_synthetic_field.cpp"
                    item["revision"] = documents["manifest"]["input_context"]["sstd_base_revision"]
                self.assertEqual(validate(documents), [])

    def test_request_content_hash_must_identify_the_supplied_snapshot(self) -> None:
        documents = bundle()
        documents["manifest"]["evidence"][0]["content_sha256"] = "1" * 64
        self.assert_rejected(documents)
        defer(documents)
        self.assert_rejected(documents)

    def test_missing_flag_and_null_content_hash_must_agree(self) -> None:
        for missing, content_hash in ((True, "e" * 64), (False, None)):
            with self.subTest(missing=missing):
                documents = bundle("sstc-undetermined")
                item = documents["manifest"]["evidence"][1]
                item["missing"] = missing
                item["content_sha256"] = content_hash
                self.assert_rejected(documents)

    def test_irrelevant_source_context_is_invalid_even_when_result_matches_manifest(self) -> None:
        for source, field, value in (("sstd", "request_sha256", "1" * 64),
                                     ("sstc", "sstd_revision", "1" * 40),
                                     ("sstc", "sstd_base_revision", "1" * 40)):
            with self.subTest(source=source, field=field):
                documents = bundle(f"{source}-undetermined")
                for name in ("manifest", "result"):
                    documents[name]["input_context"][field] = value
                self.assert_rejected(documents)

    def test_missing_or_truncated_required_input_requires_a_question_and_deferral(self) -> None:
        for flag in ("missing", "truncated"):
            for source in ("sstd", "sstc"):
                with self.subTest(flag=flag, source=source):
                    documents = bundle(f"{source}-not-required")
                    item = documents["manifest"]["evidence"][2]
                    item[flag] = True
                    if flag == "missing":
                        item["content_sha256"] = None
                    documents["result"]["evidence"] = documents["result"]["evidence"][:2]
                    for impact in documents["result"]["impacts"].values():
                        impact["evidence_ids"] = ["source", "client"]
                    self.assert_rejected(documents)
                    defer(documents)
                    self.assertEqual(validate(documents), [])
                    documents["result"]["unresolved_questions"] = []
                    self.assert_rejected(documents)

    def test_absent_mandatory_kinds_cannot_be_replaced_by_optional_evidence(self) -> None:
        for source in ("sstd", "sstc"):
            for index in range(3):
                for change in ("remove", "optional", "supporting"):
                    with self.subTest(source=source, index=index, change=change):
                        documents = bundle(f"{source}-not-required")
                        item = documents["manifest"]["evidence"][index]
                        if change == "remove":
                            removed_id = item["evidence_id"]
                            documents["manifest"]["evidence"].pop(index)
                            documents["result"]["evidence"].pop(index)
                            for impact in documents["result"]["impacts"].values():
                                impact["evidence_ids"].remove(removed_id)
                        elif change == "optional":
                            item["required"] = False
                        else:
                            item["kind"] = "supporting"
                            if item["source"] != "REQUEST" and item["path"] is None:
                                item["path"] = "src/synthetic_support.cpp"
                        self.assert_rejected(documents)
                        defer(documents)
                        self.assertEqual(validate(documents), [])

    def test_unpinned_required_revisions_force_deferral(self) -> None:
        for source, field, evidence_source in (
            ("sstd", "sstd_revision", "SSTD"),
            ("sstd", "sstc_revision", "SSTC"),
            ("sstc", "sstc_revision", "SSTC"),
            ("sstc", "request_sha256", "REQUEST"),
        ):
            with self.subTest(source=source, field=field):
                documents = bundle(f"{source}-not-required")
                for name in ("manifest", "result"):
                    documents[name]["input_context"][field] = None
                for item in documents["manifest"]["evidence"]:
                    if item["source"] == evidence_source:
                        item["request_sha256" if field == "request_sha256" else "revision"] = None
                        item["missing"] = True
                        item["content_sha256"] = None
                self.assert_rejected(documents)
                defer(documents)
                self.assertEqual(validate(documents), [])

    def test_unknown_impacts_and_open_questions_preclude_definitive_results(self) -> None:
        for outcome in ("required", "not-required"):
            for uncertainty in ("unknown", "question"):
                with self.subTest(outcome=outcome, uncertainty=uncertainty):
                    documents = bundle(f"sstc-{outcome}")
                    if uncertainty == "unknown":
                        documents["result"]["impacts"]["dependency"]["status"] = "UNKNOWN"
                    else:
                        documents["result"]["unresolved_questions"] = ["Which version is intended?"]
                    self.assert_rejected(documents)
                    defer(documents)
                    self.assertEqual(validate(documents), [])

    def test_not_required_disallows_present_impacts_risk_and_approval(self) -> None:
        for field, value in (("risk_level", "LOW"), ("risk_level", "HIGH"),
                             ("approval_reasons", ["UI_CHANGE"]), ("present", None)):
            with self.subTest(field=field, value=value):
                documents = bundle()
                if field == "present":
                    documents["result"]["impacts"]["ui_ux"]["status"] = "PRESENT"
                    documents["result"]["approval_reasons"] = ["UI_CHANGE"]
                else:
                    documents["result"][field] = value
                self.assert_rejected(documents)

    def test_each_present_impact_requires_its_corresponding_approval_reason(self) -> None:
        for category, reason in APPROVAL_REASONS.items():
            with self.subTest(category=category):
                documents = bundle()
                result = documents["result"]
                result["change_required"] = "REQUIRED"
                result["risk_level"] = "LOW"
                result["impacts"][category]["status"] = "PRESENT"
                self.assert_rejected(documents)
                result["approval_reasons"] = [reason]
                self.assertEqual(validate(documents), [])
                result["impacts"][category]["status"] = "ABSENT"
                self.assert_rejected(documents)

    def test_high_and_critical_risk_require_reasons_without_changing_task_authority(self) -> None:
        for risk, reason in (("HIGH", "HIGH_RISK"), ("CRITICAL", "CRITICAL_RISK")):
            with self.subTest(risk=risk):
                documents = bundle("sstc-required")
                original_task = copy.deepcopy(documents["task"])
                documents["result"]["risk_level"] = risk
                self.assert_rejected(documents)
                documents["result"]["approval_reasons"].append(reason)
                self.assertEqual(validate(documents), [])
                self.assertEqual(documents["task"], original_task)

    def test_internal_change_can_require_work_without_special_approval_categories(self) -> None:
        documents = bundle()
        documents["result"]["change_required"] = "REQUIRED"
        documents["result"]["risk_level"] = "LOW"
        documents["result"]["summary"] = "Synthetic internal decoder correction is required."
        self.assertEqual(validate(documents), [])

    def test_code_paths_cannot_escape_or_use_platform_specific_separators(self) -> None:
        for path in ("../outside.kt", "src/../../outside.kt", "/tmp/outside.kt",
                     "C:/outside.kt", "C:outside.kt", "src\\Client.kt", "//host/share.kt",
                     "src/name:stream", "https://example.invalid/code", "src/./Client.kt",
                     "src//Client.kt", "src/Client\nFORGED_DIAGNOSTIC.kt", None):
            with self.subTest(path=path):
                documents = bundle()
                documents["manifest"]["evidence"][1]["path"] = path
                self.assert_rejected(documents)

    def test_validation_never_executes_text_reads_evidence_or_changes_files(self) -> None:
        allowed_reads = {
            (REPOSITORY_ROOT / "contracts" / "impact-analysis.schema.json").resolve(),
            (REPOSITORY_ROOT / "state" / "task-state.schema.json").resolve(),
        }
        real_open, real_io_open = builtins.open, io.open

        def guarded_open(original):
            def open_read_only(file, mode="r", *args, **kwargs):
                self.assertFalse(any(flag in mode for flag in "wax+"), "Unexpected file write")
                self.assertIn(Path(file).resolve(), allowed_reads, "Unexpected evidence read")
                return original(file, mode, *args, **kwargs)
            return open_read_only

        targets = (
            "subprocess.run", "subprocess.Popen", "os.system", "os.popen",
            "socket.socket", "socket.create_connection", "urllib.request.urlopen",
            "webbrowser.open", "pathlib.Path.write_text", "pathlib.Path.write_bytes",
            "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir",
        )
        for name in ("sstc-required", "sstc-not-required", "sstc-undetermined", "invalid"):
            with self.subTest(name=name):
                documents = bundle("sstc-required" if name == "invalid" else name)
                documents["result"]["summary"] = (
                    "$(touch synthetic-marker); curl https://example.invalid/; "
                    "python -c 'print(1)' -- this is untrusted prose, not an instruction."
                )
                if name == "invalid":
                    documents["result"]["task_id"] = "sstc-feature-20260912-9999"
                original = copy.deepcopy(documents)
                with ExitStack() as stack:
                    mocks = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
                             for target in targets]
                    stack.enter_context(patch("builtins.open", guarded_open(real_open)))
                    stack.enter_context(patch("io.open", guarded_open(real_io_open)))
                    errors = validate(documents)
                    for mocked in mocks:
                        mocked.assert_not_called()
                self.assertEqual(bool(errors), name == "invalid")
                self.assertEqual(documents, original)


class ImpactLoaderTest(unittest.TestCase):
    def test_loader_preserves_json_values_without_performing_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for value in (None, True, 17, "synthetic", [], {"nested": [1, {"ok": False}]}):
                with self.subTest(value=value):
                    path.write_text(json.dumps(value), encoding="utf-8")
                    self.assertEqual(load_document(path), value)

    def test_duplicate_keys_at_any_depth_are_contract_errors_without_key_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for payload in ('{"task_id":1,"task_id":2}',
                            '{"evidence":[{"' + MARKER + '":1,"' + MARKER + '":2}]}'):
                with self.subTest(payload_kind="nested" if MARKER in payload else "root"):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ContractError) as caught:
                        load_document(path)
                    self.assertNotIn(MARKER, str(caught.exception))

    def test_bad_json_encoding_and_nonstandard_numbers_are_input_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for payload in (b"", b"{", b"\xff", b'{"a":1} trailing',
                            b"NaN", b"Infinity", b"-Infinity"):
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with self.assertRaises(InputError):
                        load_document(path)
            with self.assertRaises(InputError):
                load_document(Path(directory) / "missing.json")
            with self.assertRaises(InputError):
                load_document(Path(directory))

    def test_byte_limit_counts_utf8_bytes_and_allows_exactly_one_mib(self) -> None:
        self.assertEqual(MAX_BYTES, 1048576)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_bytes(b'"' + b"a" * (MAX_BYTES - 2) + b'"')
            self.assertEqual(len(load_document(path)), MAX_BYTES - 2)
            path.write_bytes(b'"' + b"a" * (MAX_BYTES - 1) + b'"')
            with self.assertRaises(InputError):
                load_document(path)
            path.write_text('"' + "\uac00" * (MAX_BYTES // 2) + '"', encoding="utf-8")
            with self.assertRaises(InputError):
                load_document(path)

    def test_integer_digit_limit_is_enforced_independently_of_python_runtime_guard(self) -> None:
        get_limit = getattr(sys, "get_int_max_str_digits", None)
        set_limit = getattr(sys, "set_int_max_str_digits", None)
        previous_limit = get_limit() if get_limit is not None else None
        try:
            if set_limit is not None:
                set_limit(0)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "input.json"
                for sign in ("", "-"):
                    with self.subTest(sign=sign):
                        path.write_text(sign + "9" * 4300, encoding="utf-8")
                        expected = (10 ** 4300 - 1) * (-1 if sign else 1)
                        self.assertEqual(load_document(path), expected)
                        for digits in (4301, 5000):
                            path.write_text('{"attempt":' + sign + "9" * digits + "}", encoding="utf-8")
                            with self.assertRaises(InputError) as caught:
                                load_document(path)
                            self.assertNotIn("9" * 100, str(caught.exception))
        finally:
            if set_limit is not None:
                set_limit(previous_limit)

    def test_nesting_limit_ignores_brackets_inside_strings(self) -> None:
        self.assertEqual(MAX_DEPTH, 32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text("[" * MAX_DEPTH + "]" * MAX_DEPTH, encoding="utf-8")
            self.assertIsInstance(load_document(path), list)
            for depth in (MAX_DEPTH + 1, 2000):
                path.write_text("[" * depth + "]" * depth, encoding="utf-8")
                with self.assertRaises(InputError):
                    load_document(path)
            value = {"quoted": '[{\\\"' * 100}
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(load_document(path), value)


class ImpactCliTest(unittest.TestCase):
    def run_cli(self, directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIRECTORY / "validate_impact_result.py"), *arguments],
            cwd=directory, capture_output=True, text=True, encoding="utf-8", check=False,
        )

    def write_bundle(self, directory: Path, documents: dict) -> list[str]:
        arguments = []
        for name in ("task", "manifest", "result"):
            path = directory / f"{name}.json"
            path.write_text(json.dumps(documents[name]), encoding="utf-8")
            arguments.extend((f"--{name}-file", str(path)))
        return arguments

    def snapshot(self, directory: Path) -> dict[str, str]:
        return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()}

    def assert_safe_failure(self, completed: subprocess.CompletedProcess[str], code: int) -> None:
        self.assertEqual(completed.returncode, code, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertTrue(completed.stderr)
        self.assertRegex(completed.stderr, r"\b[A-Z][A-Z_]+\b")
        self.assertNotIn(MARKER, completed.stderr)
        self.assertNotIn("FORGED_DIAGNOSTIC", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertNotIn("VALID_IMPACT=", completed.stderr)

    def test_success_failure_and_deferral_leave_inputs_and_surrounding_files_unchanged(self) -> None:
        for name in (*FIXTURE_NAMES, "invalid"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory_name:
                directory = Path(directory_name)
                documents = bundle("sstc-required" if name == "invalid" else name)
                if name == "invalid":
                    documents["result"]["task_id"] = "sstc-feature-20260912-9999"
                arguments = self.write_bundle(directory, documents)
                for relative in ("state/tasks/task.log.json", "state/checkpoints/task.json",
                                 "sstd/src/synthetic.cpp", "sstc/app/Synthetic.kt"):
                    path = directory / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("synthetic sentinel\n", encoding="utf-8")
                before = self.snapshot(directory)
                completed = self.run_cli(directory, *arguments)
                if name == "invalid":
                    self.assert_safe_failure(completed, 1)
                else:
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stderr, "")
                    self.assertEqual(completed.stdout,
                                     f"VALID_IMPACT={documents['result']['change_required']}\n"
                                     "AUTHORIZATION=NONE\n")
                self.assertEqual(self.snapshot(directory), before)

    def test_all_input_roles_reject_duplicate_keys_with_safe_contract_errors(self) -> None:
        for name in ("task", "manifest", "result"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory_name:
                directory = Path(directory_name)
                arguments = self.write_bundle(directory, bundle())
                (directory / f"{name}.json").write_text(
                    '{"nested":{"' + MARKER + '":1,"' + MARKER + '":2}}', encoding="utf-8"
                )
                self.assert_safe_failure(self.run_cli(directory, *arguments), 1)

    def test_schema_and_relation_errors_have_safe_field_locations(self) -> None:
        for name in ("task", "manifest", "result"):
            for failure in ("extra_key", "wrong_type", "identity"):
                with self.subTest(name=name, failure=failure), tempfile.TemporaryDirectory() as directory_name:
                    directory = Path(directory_name)
                    documents = bundle()
                    if failure == "extra_key":
                        documents[name][MARKER + "\nFORGED_DIAGNOSTIC"] = MARKER
                    elif failure == "wrong_type":
                        documents[name]["source_type"] = MARKER
                    else:
                        documents[name]["task_id"] = "sstc-feature-20260912-9999"
                    completed = self.run_cli(directory, *self.write_bundle(directory, documents))
                    self.assert_safe_failure(completed, 1)
                    self.assertRegex(completed.stderr, r"\$|\b(task|manifest|result)\b")
                    if failure == "identity":
                        self.assertIn(f"{name}.task_id", completed.stderr)

    def test_read_parse_and_resource_errors_exit_two_without_echoing_input(self) -> None:
        for failure in ("missing", "directory", "parse", "encoding", "size", "depth"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory_name:
                directory = Path(directory_name)
                arguments = self.write_bundle(directory, bundle())
                result_path = directory / "result.json"
                if failure == "missing":
                    arguments[-1] = str(directory / f"{MARKER}.json")
                elif failure == "directory":
                    arguments[-1] = str(directory)
                elif failure == "parse":
                    result_path.write_text('{"summary":"' + MARKER + '"', encoding="utf-8")
                elif failure == "encoding":
                    result_path.write_bytes(b"\xff" + MARKER.encode("ascii"))
                elif failure == "size":
                    result_path.write_bytes(b" " * (MAX_BYTES + 1))
                else:
                    result_path.write_text("[" * 40 + "]" * 40, encoding="utf-8")
                before = self.snapshot(directory)
                self.assert_safe_failure(self.run_cli(directory, *arguments), 2)
                self.assertEqual(self.snapshot(directory), before)

    def test_help_version_and_missing_arguments_need_no_input_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            for option in ("--help", "--version"):
                with self.subTest(option=option):
                    completed = self.run_cli(directory, option)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertTrue(completed.stdout.strip())
                    self.assertEqual(completed.stderr, "")
                    self.assertNotIn("VALID_IMPACT=", completed.stdout)
            self.assert_safe_failure(self.run_cli(directory), 2)
            self.assert_safe_failure(self.run_cli(directory, "--" + MARKER), 2)
            self.assertEqual(self.snapshot(directory), {})

    def test_hostile_url_large_integer_and_array_fail_safely_without_writes(self) -> None:
        for failure, code, diagnostic in (
            ("url", 1, "TASK_SCHEMA: task\n"),
            ("integer", 2, None),
            ("array", 1, "ARRAY_SIZE: result.evidence\n"),
            ("newline", 1, None),
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory_name:
                directory = Path(directory_name)
                documents = bundle()
                if failure == "url":
                    documents["task"]["draft_pr_url"] = f"https://[{MARKER}]/"
                elif failure == "array":
                    documents["result"]["evidence"] = [{MARKER: MARKER}] * 257
                elif failure == "newline":
                    for name in ("task", "manifest", "result"):
                        documents[name]["task_id"] += "\n"
                else:
                    documents["task"]["attempt"] = MARKER
                arguments = self.write_bundle(directory, documents)
                if failure == "integer":
                    task_path = directory / "task.json"
                    task_path.write_text(
                        json.dumps(documents["task"]).replace(json.dumps(MARKER), "9" * 4301),
                        encoding="utf-8",
                    )
                before = self.snapshot(directory)
                # On newer Python, disable its guard to exercise the portable loader limit.
                with patch.dict("os.environ", {"PYTHONINTMAXSTRDIGITS": "0"}):
                    completed = self.run_cli(directory, *arguments)
                self.assert_safe_failure(completed, code)
                if diagnostic is not None:
                    self.assertEqual(completed.stderr, diagnostic)
                self.assertNotIn("9" * 100, completed.stderr)
                self.assertEqual(self.snapshot(directory), before)


if __name__ == "__main__":
    unittest.main()
