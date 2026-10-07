"""Offline collector contract tests with disposable SHA-1 Git repositories."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

from impact_validation import SCHEMA_PATH, load_document, validate_impact, validate_shape


MARKER = "SYNTHETIC_PRIVATE_MARKER_9371"
PRIVATE_BODY = (
    "-----BEGIN PRIVATE KEY-----\n" + MARKER + "\n-----END PRIVATE KEY-----\n"
).encode("ascii")


class ImpactCollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Do not inherit account configuration, hooks, signing, or Git overrides.
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        self.environment.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_ATTR_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0", PYTHONDONTWRITEBYTECODE="1",
        )
        self.sstc = self.new_repository("sstc")
        self.sstd = self.new_repository("sstd")
        self.client_files = {
            "AGENTS.md": b"Root client instructions.\r\n",
            "app/AGENTS.md": b"Application instructions.\n",
            "app/src/AGENTS.md": b"Source instructions.\n",
            "app/src/Client.kt": b"val version = 1\r\n",
            "app/src/Other.kt": b"val other = 2\n",
            "unrelated/AGENTS.md": b"Not in selected ancestry.\n",
        }
        for path, body in self.client_files.items():
            self.write(self.sstc, path, body)
        self.sstc_revision = self.commit(self.sstc)
        self.write(self.sstd, "protocol.txt", b"version=1\n")
        self.sstd_base = self.commit(self.sstd)
        self.write(self.sstd, "protocol.txt", b"version=2\n")
        self.sstd_revision = self.commit(self.sstd)
        self.task_file = self.root / "task.json"
        self.request_file = self.root / "request.txt"
        self.request_file.write_bytes("Synthetic request: \uac80\uc99d.\r\n".encode("utf-8"))
        self.output = self.root / "collected"
        self.set_task()

    def git(self, repository: Path, *arguments: str, body: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", *arguments], cwd=repository, env=self.environment,
            input=body, capture_output=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def new_repository(self, name: str) -> Path:
        repository = self.root / name
        repository.mkdir()
        self.git(repository, "init", "--object-format=sha1", "--template=")
        for key, value in (
            ("user.name", "Synthetic collector test"),
            ("user.email", "collector@example.invalid"),
            ("core.autocrlf", "false"), ("commit.gpgsign", "false"),
            ("gc.auto", "0"),
        ):
            self.git(repository, "config", key, value)
        return repository

    def write(self, repository: Path, path: str, body: bytes) -> None:
        destination = repository / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)

    def commit(self, repository: Path) -> str:
        self.git(repository, "add", "--all")
        self.git(repository, "commit", "-m", "Synthetic fixture")
        return self.git(repository, "rev-parse", "HEAD").decode("ascii").strip()

    def set_task(self, source: str = "SSTC_FEATURE", reference: str | None = None) -> None:
        prefix = "sstd-sync" if source == "SSTD_CHANGE" else "sstc-feature"
        self.task = {
            "task_id": prefix + "-20260914-0001",
            "source_type": source,
            "source_reference": reference or (
                self.sstd_revision if source == "SSTD_CHANGE" else "local:synthetic-request"
            ),
            "status": "ANALYZING", "risk_level": "MEDIUM",
            "created_at": "2026-09-14T00:00:00Z",
            "updated_at": "2026-09-14T00:00:00Z", "attempt": 0,
            "approval_reason": "Preserve existing review requirement.",
        }
        self.task_file.write_text(json.dumps(self.task), encoding="utf-8")

    def arguments(self, contexts: tuple[str, ...] = ("app/src/Client.kt",),
                  paths: tuple[str, ...] = ("protocol.txt",),
                  base: str | None = None, output: Path | None = None) -> list[str]:
        arguments = [
            "--task-file", str(self.task_file),
            "--sstc-repository", str(self.sstc),
            "--sstc-revision", self.sstc_revision,
            "--output-directory", str(output if output is not None else self.output),
        ]
        for path in contexts:
            arguments += ["--sstc-context", path]
        if self.task["source_type"] == "SSTD_CHANGE":
            arguments += [
                "--source-repository", str(self.sstd),
                "--sstd-base", base if base is not None else self.sstd_base,
            ]
            for path in paths:
                arguments += ["--sstd-path", path]
        else:
            arguments += ["--request-file", str(self.request_file)]
        return arguments

    def snapshot(self, root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).digest()
            for path in root.rglob("*") if path.is_file()
        }

    def collect(self, arguments: list[str] | None = None) -> subprocess.CompletedProcess[bytes]:
        before = [self.snapshot(repository) for repository in (self.sstc, self.sstd)]
        task_before = self.task_file.read_bytes()
        request_before = self.request_file.read_bytes() if self.request_file.exists() else None
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIRECTORY / "collect_impact_inputs.py"),
             *(arguments if arguments is not None else self.arguments())],
            cwd=self.root, env=self.environment, capture_output=True, check=False, timeout=30,
        )
        self.assertEqual(self.task_file.read_bytes(), task_before)
        self.assertEqual(
            self.request_file.read_bytes() if self.request_file.exists() else None,
            request_before,
        )
        self.assertEqual(
            [self.snapshot(repository) for repository in (self.sstc, self.sstd)], before,
            "Collector changed source worktree, index, refs, or object files",
        )
        return result

    def result_for(self, manifest: dict, definite: bool = False) -> dict:
        ids = [item["evidence_id"] for item in manifest["evidence"]]
        return {
            "schema_version": "1.0", "task_id": self.task["task_id"],
            "source_type": self.task["source_type"],
            "input_context": copy.deepcopy(manifest["input_context"]),
            "change_required": "REQUIRED" if definite else "UNDETERMINED",
            "summary": "Synthetic inputs require human interpretation.",
            "risk_level": "LOW",
            "evidence": [{"evidence_id": key, "reason": "Synthetic input."} for key in ids],
            "impacts": {
                key: {"status": "ABSENT" if definite else "UNKNOWN",
                      "reason": "Synthetic test assertion.", "evidence_ids": ids}
                for key in ("ui_ux", "protocol_contract", "dependency",
                            "android_permission", "destructive_action", "sstd_change_required")
            },
            "approval_reasons": [],
            "unresolved_questions": [] if definite else ["What behavior should change?"],
        }

    def manifest(self, result: subprocess.CompletedProcess[bytes],
                 output: Path | None = None) -> dict:
        self.assertIn(result.returncode, (0, 1), result.stderr.decode("utf-8", "replace"))
        directory = output if output is not None else self.output
        manifest = load_document(directory / "manifest.json")
        incomplete = any(item["required"] and (item["missing"] or item["truncated"])
                         for item in manifest["evidence"])
        self.assertEqual(result.returncode, 1 if incomplete else 0)
        schema = load_document(SCHEMA_PATH)
        self.assertEqual(validate_shape(manifest, schema["$defs"]["manifest"], schema, "manifest"), [])
        self.assertEqual(validate_impact(self.task, manifest, self.result_for(manifest)), [])
        ids = [item["evidence_id"] for item in manifest["evidence"]]
        self.assertEqual(len(ids), len(set(ids)))
        expected_files = {"manifest.json"}
        for item in manifest["evidence"]:
            relative = "evidence/" + item["evidence_id"] + ".txt"
            body_path = directory / relative
            if item["missing"]:
                self.assertIsNone(item["content_sha256"])
                self.assertFalse(body_path.exists(), "Missing evidence must have no body")
            else:
                expected_files.add(relative)
                self.assertEqual(
                    hashlib.sha256(body_path.read_bytes()).hexdigest(), item["content_sha256"]
                )
        self.assertEqual(
            {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()},
            expected_files,
        )
        return manifest

    def body(self, item: dict, output: Path | None = None) -> bytes:
        directory = output if output is not None else self.output
        return (directory / "evidence" / (item["evidence_id"] + ".txt")).read_bytes()

    def source_body(self, manifest: dict) -> bytes:
        return b"\n".join(self.body(item) for item in manifest["evidence"]
                          if item["kind"] == "source_diff" and not item["missing"])

    def assert_failed(self, result: subprocess.CompletedProcess[bytes],
                      output: Path | None = None) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(MARKER.encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertFalse((output if output is not None else self.output).exists())

    def assert_incomplete(self, manifest: dict) -> None:
        self.assertTrue(any(item["required"] and (item["missing"] or item["truncated"])
                            for item in manifest["evidence"]))
        self.assertIn("UNDETERMINED_REQUIRED: result.change_required",
                      validate_impact(self.task, manifest, self.result_for(manifest, definite=True)))

    def test_feature_snapshots_exact_bytes_and_scoped_ancestor_instructions(self) -> None:
        manifest = self.manifest(self.collect(self.arguments(
            contexts=("app/src/Client.kt", "app/src/Other.kt"),
        )))
        context = manifest["input_context"]
        self.assertEqual(context["sstc_revision"], self.sstc_revision)
        self.assertIsNone(context["sstd_revision"])
        self.assertIsNone(context["sstd_base_revision"])
        self.assertEqual(context["request_sha256"],
                         hashlib.sha256(self.request_file.read_bytes()).hexdigest())
        request = [item for item in manifest["evidence"] if item["kind"] == "request"]
        self.assertEqual(len(request), 1)
        self.assertEqual(self.body(request[0]), self.request_file.read_bytes())
        self.assertTrue(request[0]["required"])
        instructions = [item for item in manifest["evidence"]
                        if item["kind"] == "target_instructions" and not item["missing"]]
        self.assertEqual({item["path"] for item in instructions},
                         {"AGENTS.md", "app/AGENTS.md", "app/src/AGENTS.md"})
        self.assertEqual(len(instructions), 3)
        contexts = [item for item in manifest["evidence"] if item["kind"] == "sstc_context"]
        self.assertEqual({item["path"] for item in contexts},
                         {"app/src/Client.kt", "app/src/Other.kt"})
        for item in instructions + contexts:
            self.assertTrue(item["required"])
            self.assertFalse(item["missing"])
            self.assertFalse(item["truncated"])
            self.assertEqual(item["revision"], self.sstc_revision)
            self.assertEqual(self.body(item), self.client_files[item["path"]])
        self.assertEqual(validate_impact(self.task, manifest, self.result_for(manifest, True)), [])

    def test_sstd_uses_explicit_revisions_and_repeated_selected_paths(self) -> None:
        self.write(self.sstd, "extra.txt", b"extra contract\n")
        self.sstd_revision = self.commit(self.sstd)
        self.set_task("SSTD_CHANGE")
        manifest = self.manifest(self.collect(self.arguments(paths=("protocol.txt", "extra.txt"))))
        self.assertEqual(manifest["input_context"]["sstd_base_revision"], self.sstd_base)
        self.assertEqual(manifest["input_context"]["sstd_revision"], self.sstd_revision)
        self.assertIsNone(manifest["input_context"]["request_sha256"])
        body = self.source_body(manifest)
        for expected in (b"-version=1", b"+version=2", b"+extra contract"):
            self.assertIn(expected, body)
        self.assertFalse(any(item["source"] == "REQUEST" for item in manifest["evidence"]))
        self.assertEqual(validate_impact(self.task, manifest, self.result_for(manifest, True)), [])

    def test_fixed_commits_ignore_dirty_index_worktree_and_moved_refs(self) -> None:
        for repository in (self.sstc, self.sstd):
            self.git(repository, "tag", "selected")
        initial = self.manifest(self.collect())
        initial_files = self.snapshot(self.output)
        for repository, path in ((self.sstc, "app/src/Client.kt"), (self.sstd, "protocol.txt")):
            self.write(repository, path, b"moved ref content\n")
            self.commit(repository)
            self.git(repository, "tag", "-f", "selected")
            self.write(repository, path, b"staged dirty content\n")
            self.git(repository, "add", path)
            self.write(repository, path, b"unstaged dirty content\n")
        repeated_output = self.root / "repeated"
        repeated = self.manifest(self.collect(self.arguments(output=repeated_output)), repeated_output)
        self.assertEqual(initial, repeated)
        self.assertEqual(initial_files, self.snapshot(repeated_output))
        self.set_task("SSTD_CHANGE")
        source_output = self.root / "source"
        manifest = self.manifest(self.collect(self.arguments(output=source_output)), source_output)
        source = b"\n".join(self.body(item, source_output) for item in manifest["evidence"]
                            if item["kind"] == "source_diff" and not item["missing"])
        self.assertEqual(manifest["input_context"]["sstd_revision"], self.sstd_revision)
        self.assertIn(b"+version=2", source)
        for unexpected in (b"moved ref content", b"staged dirty content", b"unstaged dirty content"):
            self.assertNotIn(unexpected, source)

    def test_deleted_file_diff_is_bound_to_base_revision(self) -> None:
        (self.sstd / "protocol.txt").unlink()
        self.sstd_revision = self.commit(self.sstd)
        self.set_task("SSTD_CHANGE")
        manifest = self.manifest(self.collect())
        self.assertIn(b"-version=1", self.source_body(manifest))
        self.assertIn(b"deleted file mode", self.source_body(manifest))
        self.assertEqual(manifest["input_context"]["sstd_base_revision"], self.sstd_base)
        self.assertEqual(manifest["input_context"]["sstd_revision"], self.sstd_revision)
        deleted = [item for item in manifest["evidence"] if item["source"] == "SSTD"
                   and item["path"] == "protocol.txt"]
        # Aggregate source_diff may use path=null and the target revision.
        # Only optional path-specific deleted evidence must refer to the base.
        for item in deleted:
            self.assertEqual(item["revision"], self.sstd_base)
            self.assertFalse(item["missing"])

    def test_root_diff_and_explicit_base_and_scope_are_required(self) -> None:
        self.set_task("SSTD_CHANGE", self.sstd_base)
        manifest = self.manifest(self.collect(self.arguments(base="ROOT")))
        self.assertIsNone(manifest["input_context"]["sstd_base_revision"])
        self.assertIn(b"+version=1", self.source_body(manifest))
        self.set_task("SSTD_CHANGE")
        for name in ("nonroot", "no-base", "no-path"):
            with self.subTest(case=name):
                output = self.root / name
                arguments = self.arguments(base="ROOT", output=output)
                if name != "nonroot":
                    flag = "--sstd-base" if name == "no-base" else "--sstd-path"
                    index = arguments.index(flag)
                    del arguments[index:index + 2]
                self.assert_failed(self.collect(arguments), output)

    def test_merge_diff_uses_explicit_second_parent_not_first_parent(self) -> None:
        self.git(self.sstd, "checkout", "-b", "side", self.sstd_base)
        self.write(self.sstd, "side.txt", b"side parent content\n")
        side = self.commit(self.sstd)
        self.git(self.sstd, "checkout", "-b", "merge-target", self.sstd_revision)
        self.git(self.sstd, "merge", "--no-ff", "-m", "Synthetic merge", "side")
        self.sstd_revision = self.git(self.sstd, "rev-parse", "HEAD").decode().strip()
        self.set_task("SSTD_CHANGE")
        manifest = self.manifest(self.collect(self.arguments(base=side)))
        self.assertEqual(manifest["input_context"]["sstd_base_revision"], side)
        self.assertIn(b"-version=1", self.source_body(manifest))
        self.assertIn(b"+version=2", self.source_body(manifest))
        self.assertNotIn(b"+side parent content", self.source_body(manifest))
        self.assertEqual(validate_impact(self.task, manifest, self.result_for(manifest, True)), [])

    def test_unselected_sstd_changes_are_explicitly_incomplete(self) -> None:
        self.write(self.sstd, "outside.txt", b"unselected contract change\n")
        self.sstd_revision = self.commit(self.sstd)
        self.set_task("SSTD_CHANGE")
        manifest = self.manifest(self.collect())
        self.assertTrue(any(item["kind"] == "source_diff" and item["required"]
                            and (item["missing"] or item["truncated"])
                            for item in manifest["evidence"]))
        self.assertNotIn(b"+unselected contract change", self.source_body(manifest))
        self.assert_incomplete(manifest)

    def test_missing_context_instructions_and_request_have_no_body(self) -> None:
        for kind in ("sstc_context", "target_instructions", "request"):
            with self.subTest(kind=kind):
                output = self.root / kind
                contexts = ("app/src/Missing.kt",) if kind == "sstc_context" else ("app/src/Client.kt",)
                if kind == "target_instructions":
                    (self.sstc / "AGENTS.md").unlink()
                    self.sstc_revision = self.commit(self.sstc)
                if kind == "request":
                    self.request_file.unlink()
                manifest = self.manifest(self.collect(self.arguments(
                    contexts=contexts, output=output,
                )), output)
                self.assertTrue(any(item["kind"] == kind and item["required"] and item["missing"]
                                    for item in manifest["evidence"]))
                self.assert_incomplete(manifest)
                if kind == "request":
                    self.assertIsNone(manifest["input_context"]["request_sha256"])

    def test_oversized_context_cannot_appear_complete_or_have_wrong_hash(self) -> None:
        large_body = b"synthetic context line\n" * 4_000
        self.write(self.sstc, "app/src/Large.kt", large_body)
        self.sstc_revision = self.commit(self.sstc)
        manifest = self.manifest(self.collect(self.arguments(contexts=("app/src/Large.kt",))))
        items = [item for item in manifest["evidence"] if item["kind"] == "sstc_context"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item["truncated"])
        if not item["missing"]:
            saved = self.body(item)
            self.assertLess(len(saved), len(large_body))
            self.assertTrue(large_body.startswith(saved), "Truncation must preserve original bytes")
        self.assert_incomplete(manifest)

    def test_invalid_paths_and_git_pathspecs_are_rejected_for_both_sources(self) -> None:
        invalid = (
            "../" + MARKER, "/absolute/" + MARKER, "C:/absolute/" + MARKER,
            "app\\src\\Client.kt", "./protocol.txt", ".git/config",
            "app//src/Client.kt", "app/*", ":(glob)**", "app/src/Client.kt:stream",
        )
        for source in ("SSTC_FEATURE", "SSTD_CHANGE"):
            self.set_task(source)
            for path in invalid:
                with self.subTest(source=source, path=path):
                    arguments = self.arguments(
                        contexts=(path,) if source == "SSTC_FEATURE" else ("app/src/Client.kt",),
                        paths=(path,) if source == "SSTD_CHANGE" else ("protocol.txt",),
                    )
                    self.assert_failed(self.collect(arguments))

    def test_git_symlink_ancestors_gitlinks_and_trees_are_not_regular_evidence(self) -> None:
        for source, repository in (("SSTC_FEATURE", self.sstc), ("SSTD_CHANGE", self.sstd)):
            blob = self.git(repository, "hash-object", "-w", "--stdin",
                            body=MARKER.encode()).decode().strip()
            head = self.git(repository, "rev-parse", "HEAD").decode().strip()
            for mode, object_id, path in (
                ("120000", blob, "link"), ("160000", head, "submodule"),
            ):
                self.git(repository, "update-index", "--add", "--cacheinfo",
                         mode + "," + object_id + "," + path)
            self.git(repository, "commit", "-m", "Synthetic unsupported Git entries")
            revision = self.git(repository, "rev-parse", "HEAD").decode().strip()
            if source == "SSTC_FEATURE":
                self.sstc_revision = revision
            else:
                self.sstd_revision = revision
            self.set_task(source)
            for path in ("link", "link/child.txt", "submodule", "submodule/child.txt", "."):
                with self.subTest(source=source, path=path):
                    self.assert_failed(self.collect(self.arguments(
                        contexts=(path,) if source == "SSTC_FEATURE" else ("app/src/Client.kt",),
                        paths=(path,) if source == "SSTD_CHANGE" else ("protocol.txt",),
                    )))
        self.set_task()
        self.git(self.sstc, "update-index", "--chmod=+x", "app/src/Client.kt")
        self.git(self.sstc, "commit", "-m", "Synthetic executable regular file")
        self.sstc_revision = self.git(self.sstc, "rev-parse", "HEAD").decode().strip()
        manifest = self.manifest(self.collect())
        client = next(item for item in manifest["evidence"] if item["kind"] == "sstc_context")
        self.assertEqual(self.body(client), self.client_files["app/src/Client.kt"])

    def test_secret_filenames_are_rejected_without_creating_artifacts(self) -> None:
        names = (".env", "id_rsa", "release.keystore")
        for source, repository in (("SSTC_FEATURE", self.sstc), ("SSTD_CHANGE", self.sstd)):
            for name in names:
                self.write(repository, name, (MARKER + "\n").encode())
            revision = self.commit(repository)
            if source == "SSTC_FEATURE":
                self.sstc_revision = revision
            else:
                self.sstd_revision = revision
            self.set_task(source)
            for name in names:
                with self.subTest(source=source, filename=name):
                    self.assert_failed(self.collect(self.arguments(
                        contexts=(name,) if source == "SSTC_FEATURE" else ("app/src/Client.kt",),
                        paths=(name,) if source == "SSTD_CHANGE" else ("protocol.txt",),
                    )))
        self.set_task()
        dangerous_request = self.root / ".env"
        dangerous_request.write_bytes(b"Synthetic harmless content\n")
        arguments = self.arguments()
        arguments[arguments.index("--request-file") + 1] = str(dangerous_request)
        self.assert_failed(self.collect(arguments))

    def test_secret_content_in_requests_context_instructions_and_deleted_diff_is_atomic(self) -> None:
        self.request_file.write_bytes(PRIVATE_BODY)
        self.assert_failed(self.collect())
        self.request_file.write_bytes(b"Synthetic safe request\n")
        for path in ("app/src/Client.kt", "AGENTS.md"):
            with self.subTest(path=path):
                self.write(self.sstc, path, PRIVATE_BODY)
                self.sstc_revision = self.commit(self.sstc)
                self.assert_failed(self.collect())
                self.write(self.sstc, path, self.client_files[path])
                self.sstc_revision = self.commit(self.sstc)
        self.write(self.sstd, "protocol.txt", PRIVATE_BODY)
        self.sstd_base = self.commit(self.sstd)
        (self.sstd / "protocol.txt").unlink()
        self.sstd_revision = self.commit(self.sstd)
        self.set_task("SSTD_CHANGE")
        self.assert_failed(self.collect())

    def test_slack_app_tokens_are_rejected_before_request_persistence(self) -> None:
        token = b"xapp-1-A123456-T123456-abcdefghijklmnopqrstuvwxyz012345"
        for raw in (token, b"SLACK_APP_TOKEN=" + token):
            with self.subTest(assignment=raw.startswith(b"SLACK_APP_TOKEN=")):
                self.request_file.write_bytes(raw)
                result = self.collect()
                self.assert_failed(result)
                self.assertNotIn(token, result.stdout + result.stderr)
        from impact_collection import check_content
        check_content(b"Document the xapp prefix; xapp-short is a synthetic label.")

    def test_existing_outputs_and_outputs_inside_source_repositories_are_never_overwritten(self) -> None:
        for form in ("file", "empty-directory", "populated-directory"):
            with self.subTest(form=form):
                output = self.root / form
                if form == "file":
                    output.write_bytes(b"existing output\n")
                else:
                    output.mkdir()
                    if form == "populated-directory":
                        (output / "manifest.json").write_bytes(b"existing manifest\n")
                before = output.read_bytes() if output.is_file() else self.snapshot(output)
                result = self.collect(self.arguments(output=output))
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(result.stdout, b"")
                self.assertEqual(output.read_bytes() if output.is_file() else self.snapshot(output), before)
        self.set_task("SSTD_CHANGE")
        for repository in (self.sstd, self.sstc):
            for relative in ("new-output", ".git/new-output"):
                with self.subTest(repository=repository.name, relative=relative):
                    output = repository / relative
                    self.assert_failed(self.collect(self.arguments(output=output)), output)

    def test_invalid_task_revision_and_source_arguments_fail_with_rawsafe_stderr(self) -> None:
        for case in ("task-json", "unknown-task-key", "revision", "foreign-source-args", "no-request"):
            with self.subTest(case=case):
                self.set_task()
                arguments = self.arguments()
                if case == "task-json":
                    self.task_file.write_bytes(('{"' + MARKER + '": invalid}').encode())
                elif case == "unknown-task-key":
                    self.task[MARKER] = MARKER
                    self.task_file.write_text(json.dumps(self.task), encoding="utf-8")
                elif case == "revision":
                    arguments[arguments.index("--sstc-revision") + 1] = MARKER
                elif case == "foreign-source-args":
                    arguments += ["--source-repository", str(self.sstd),
                                  "--sstd-base", self.sstd_base, "--sstd-path", "protocol.txt"]
                else:
                    index = arguments.index("--request-file")
                    del arguments[index:index + 2]
                self.assert_failed(self.collect(arguments))
        self.set_task("SSTD_CHANGE")
        self.assert_failed(self.collect(self.arguments() + ["--request-file", str(self.request_file)]))

    def test_both_sources_produce_manifests_accepted_by_existing_validation_cli(self) -> None:
        for source in ("SSTC_FEATURE", "SSTD_CHANGE"):
            with self.subTest(source=source):
                self.set_task(source)
                output = self.root / source
                manifest = self.manifest(self.collect(self.arguments(output=output)), output)
                result_file = self.root / "analysis.json"
                result_file.write_text(json.dumps(self.result_for(manifest)), encoding="utf-8")
                before = self.snapshot(output)
                validated = subprocess.run(
                    [sys.executable, "-B", str(SCRIPTS_DIRECTORY / "validate_impact_result.py"),
                     "--task-file", str(self.task_file),
                     "--manifest-file", str(output / "manifest.json"),
                     "--result-file", str(result_file)],
                    cwd=self.root, env=self.environment, capture_output=True, check=False, timeout=30,
                )
                self.assertEqual(validated.returncode, 0, validated.stderr.decode("utf-8", "replace"))
                self.assertIn(b"VALID_IMPACT=UNDETERMINED", validated.stdout)
                self.assertIn(b"AUTHORIZATION=NONE", validated.stdout)
                self.assertEqual(self.snapshot(output), before)


if __name__ == "__main__":
    unittest.main()
