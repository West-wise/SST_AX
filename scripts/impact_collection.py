"""Bounded Git-object input collection, not semantic analysis or authorization."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any

from impact_validation import SCHEMA_PATH, load_document, safe_relative_path, validate_shape
from validate_task_state import DEFAULT_SCHEMA_PATH, validate_task_state

MAX_EVIDENCE_BYTES = 65_536
MAX_TOTAL_BYTES = 1_048_576
MAX_EVIDENCE = 128
MAX_GIT_BYTES = 2_097_152
GIT_TIMEOUT = 15
SHA40 = re.compile(r"[0-9a-f]{40}")
SECRET = re.compile(
    rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    rb"(?:xox[baprs]|xapp)-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|"
    rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,})\b|"
    rb"(?i:authorization\s*:\s*bearer\s+\S+|"
    rb"(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)"
    rb"\s*[=:]\s*[\"']?[A-Za-z0-9_+/=-]{8,})"
)


class CollectionError(ValueError):
    """Rejected input; never include untrusted data in the message."""


def check_content(raw: bytes) -> None:
    """Reject recognizable secrets before any persistence, not a full scanner."""
    if SECRET.search(raw):
        raise CollectionError("SECRET_CONTENT")


def check_path(path: str) -> str:
    """Use PR6 portable path rules and reject obvious secret-bearing names."""
    if (not safe_relative_path(path) or not path.strip() or len(path) > 4096
            or any(char in path for char in "*?[]")):
        raise CollectionError("UNSAFE_PATH")
    for part in path.lower().split("/"):
        compact = re.sub(r"[-_.]", "", part)
        if (part == ".git" or part.startswith(".env") or
                any(word in compact for word in ("privatekey", "keystore", "credential")) or
                part in ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc") or
                part.endswith((".pem", ".key", ".p12", ".pfx", ".jks"))):
            raise CollectionError("SECRET_PATH")
    check_content(path.encode("utf-8"))
    return path


def selected_paths(paths: list[str]) -> list[str]:
    if len(paths) > MAX_EVIDENCE:
        raise CollectionError("PATH_COUNT")
    return sorted({check_path(path) for path in paths})


def git_environment() -> dict[str, str]:
    """Discard all inherited Git controls, traces, credentials and config injection."""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "", "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_PAGER": "", "GIT_TRACE": "0", "LC_ALL": "C",
    })
    return env


class Git:
    """Only explicit built-in Git object commands; bounded memory and runtime."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository.resolve(strict=True)
        self.git_dir: Path | None = None
        self.executable = shutil.which("git")
        if not self.executable or not self.repository.is_dir():
            raise CollectionError("REPOSITORY")
        self.git_dir = Path(self.run(["rev-parse", "--absolute-git-dir"]).decode("utf-8").strip()).resolve(strict=True)
        common = self.run(["rev-parse", "--git-common-dir"]).decode("utf-8").strip()
        self.common_dir = (self.repository / common).resolve(strict=True)
        if self.run(["rev-parse", "--show-object-format"]).strip() != b"sha1":
            raise CollectionError("OBJECT_FORMAT")

    def run(self, arguments: list[str]) -> bytes:
        command = [self.executable, "--no-pager", "--literal-pathspecs",
                   "--no-replace-objects"]
        for option in ("core.fsmonitor=false", "core.hooksPath=" + os.devnull,
                       "core.attributesFile=" + os.devnull, "core.pager=",
                       "protocol.allow=never", "submodule.recurse=false",
                       "gc.auto=0", "maintenance.auto=false", "diff.renames=false",
                       "diff.submodule=short",
                       "core.quotePath=true", "diff.ignoreSubmodules=none",
                       "log.showSignature=false", "core.useReplaceRefs=false"):
            command.extend(["-c", option])
        if self.git_dir is not None:
            command.extend(["--git-dir=" + str(self.git_dir), "-c", "core.bare=true"])
        command.extend(arguments)
        output = bytearray()
        overflow = threading.Event()
        with subprocess.Popen(command, cwd=self.repository, env=git_environment(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            def drain() -> None:
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        break
                    if len(output) + len(chunk) > MAX_GIT_BYTES:
                        overflow.set()
                        if process.poll() is None:
                            process.kill()
                        break
                    output.extend(chunk)

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                process.wait(timeout=GIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise CollectionError("GIT_TIMEOUT")
            except KeyboardInterrupt:
                process.kill()
                process.wait()
                raise
            finally:
                reader.join(timeout=GIT_TIMEOUT)
            if reader.is_alive() or overflow.is_set() or process.returncode:
                raise CollectionError("GIT_READ")
        return bytes(output)

    def resolve(self, reference: str) -> str:
        if (not reference or reference.startswith("-") or len(reference) > 4096 or
                any(ord(char) < 32 for char in reference)):
            raise CollectionError("REVISION")
        raw = self.run(["rev-parse", "--verify", "--end-of-options", reference + "^{commit}"])
        revision = raw.decode("ascii").strip()
        if not SHA40.fullmatch(revision):
            raise CollectionError("REVISION")
        return revision

    def file(self, revision: str, path: str, read: bool = True) -> bytes | None:
        """Walk each tree component to reject symlink/submodule ancestors too."""
        parts = path.split("/")
        for index in range(len(parts)):
            prefix = "/".join(parts[:index + 1])
            record = self.run(["ls-tree", "-z", revision, "--", prefix])
            if not record:
                return None
            entries = record.rstrip(b"\0").split(b"\0")
            if len(entries) != 1:
                raise CollectionError("TREE_PATH")
            header, actual = entries[0].split(b"\t", 1)
            mode, kind, object_id = header.split()
            if actual.decode("utf-8") != prefix or not SHA40.fullmatch(object_id.decode("ascii")):
                raise CollectionError("TREE_PATH")
            last = index == len(parts) - 1
            if (not last and (mode != b"040000" or kind != b"tree")) or (
                    last and (mode not in (b"100644", b"100755") or kind != b"blob")):
                raise CollectionError("UNSAFE_GIT_MODE")
        if not read:
            return b""
        return self.run(["cat-file", "blob", object_id.decode("ascii")])


class Bundle:
    def __init__(self, task: dict[str, Any], revision: str) -> None:
        self.manifest: dict[str, Any] = {
            "schema_version": "1.0", "task_id": task["task_id"],
            "source_type": task["source_type"],
            "input_context": {
                "source_reference": task["source_reference"], "sstc_revision": revision,
                "sstd_base_revision": None, "sstd_revision": None, "request_sha256": None,
            }, "evidence": [],
        }
        self.bodies: dict[str, bytes] = {}
        self.total = 0

    def add(self, kind: str, source: str, revision: str | None,
            path: str | None, raw: bytes | None) -> None:
        if len(self.manifest["evidence"]) >= MAX_EVIDENCE:
            raise CollectionError("EVIDENCE_COUNT")
        evidence_id = f"e{len(self.manifest['evidence']) + 1:04d}"
        truncated = False
        digest = None
        if raw is not None:
            check_content(raw)
            text = raw.decode("utf-8")
            if "\0" in text:
                raise CollectionError("BINARY_CONTENT")
            limit = min(MAX_EVIDENCE_BYTES, MAX_TOTAL_BYTES - self.total)
            retained = raw[:limit].decode("utf-8", errors="ignore").encode("utf-8")
            truncated = retained != raw
            if source == "REQUEST" and truncated:
                raise CollectionError("REQUEST_SIZE")
            digest = hashlib.sha256(retained).hexdigest()
            self.bodies[evidence_id] = retained
            self.total += len(retained)
        self.manifest["evidence"].append({
            "evidence_id": evidence_id, "source": source, "revision": revision,
            "request_sha256": self.manifest["input_context"]["request_sha256"] if source == "REQUEST" else None,
            "path": path, "kind": kind, "content_sha256": digest,
            "required": True, "missing": raw is None, "truncated": truncated,
        })

    def complete(self) -> bool:
        return not any(item["missing"] or item["truncated"] for item in self.manifest["evidence"])


def source_diff(bundle: Bundle, source: Git, base_ref: str, paths: list[str]) -> None:
    target = source.resolve(bundle.manifest["input_context"]["source_reference"])
    base = None if base_ref == "ROOT" else source.resolve(base_ref)
    if base is None:
        commit = source.run(["cat-file", "commit", target])
        if any(line.startswith(b"parent ") for line in commit.split(b"\n\n", 1)[0].splitlines()):
            raise CollectionError("ROOT_REQUIRES_ROOT_COMMIT")
    bundle.manifest["input_context"].update(sstd_revision=target, sstd_base_revision=base)
    common = ["--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
              "--ignore-submodules=none"]
    command = (["diff", *common, base, target] if base is not None else
               ["diff-tree", "--root", "--no-commit-id", "-r", *common, target])
    names = source.run([*command, "--name-only", "-z", "--"])
    changed = {check_path(name.decode("utf-8")) for name in names.split(b"\0") if name}
    missing_selected = False
    for path in paths:
        present_target = source.file(target, path, read=False)
        present_base = source.file(base, path, read=False) if base is not None else None
        if present_target is None and present_base is None:
            missing_selected = True
        # Scan both original blobs: a diff header can split a recognizable secret.
        for rev, present in ((target, present_target), (base, present_base)):
            if rev is not None and present is not None:
                raw = source.file(rev, path)
                assert raw is not None
                check_content(raw)
    raw = source.run([*command, "--patch", "--text", "--full-index",
                      "--src-prefix=a/", "--dst-prefix=b/", "--", *paths])
    bundle.add("source_diff", "SSTD", target, None, raw)
    if changed - set(paths) or missing_selected:
        bundle.add("source_diff", "SSTD", target, None, None)


def output_path(path: Path, repositories: list[Git]) -> Path:
    absolute = Path(os.path.abspath(path))
    for component in [absolute, *absolute.parents]:
        if component.is_symlink() or (component.exists() and
                getattr(component.lstat(), "st_file_attributes", 0) & 0x400):
            raise CollectionError("OUTPUT_LINK")
    if absolute.exists() or not absolute.parent.is_dir():
        raise CollectionError("OUTPUT_EXISTS_OR_PARENT")
    output = absolute.resolve()
    for repo in repositories:
        for protected in (repo.repository, repo.git_dir, repo.common_dir):
            assert protected is not None
            if (output == protected or output in protected.parents or protected in output.parents):
                raise CollectionError("OUTPUT_OVERLAP")
    return output


def publish(bundle: Bundle, output: Path) -> None:
    """Publish without overwriting, including a competing empty directory."""
    if os.name != "nt" and not sys.platform.startswith("linux"):
        raise CollectionError("NO_ATOMIC_NOREPLACE")
    stage = Path(tempfile.mkdtemp(prefix=".impact-inputs-", dir=output.parent)).resolve()
    try:
        (stage / "evidence").mkdir()
        for evidence_id, raw in bundle.bodies.items():
            (stage / "evidence" / (evidence_id + ".txt")).write_bytes(raw)
        raw_manifest = (json.dumps(bundle.manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(raw_manifest) > MAX_TOTAL_BYTES:
            raise CollectionError("MANIFEST_SIZE")
        check_content(raw_manifest)
        (stage / "manifest.json").write_bytes(raw_manifest)
        if os.name == "nt":
            os.rename(stage, output)
        elif sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            rename = getattr(libc, "renameat2", None)
            if rename is None:
                raise CollectionError("NO_ATOMIC_NOREPLACE")
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(stage), -100, os.fsencode(output), 1):
                raise CollectionError("PUBLISH_FAILED")
        else:
            raise CollectionError("NO_ATOMIC_NOREPLACE")
    finally:
        # Only our exact mkdtemp directory; never recurse through external links.
        if stage.exists():
            if (stage.parent != output.parent or not stage.name.startswith(".impact-inputs-")
                    or stage.is_symlink()):
                raise CollectionError("CLEANUP_SCOPE")
            evidence_dir = stage / "evidence"
            if evidence_dir.is_symlink():
                raise CollectionError("CLEANUP_SCOPE")
            if evidence_dir.is_dir():
                for item in evidence_dir.iterdir():
                    item.unlink()
                evidence_dir.rmdir()
            (stage / "manifest.json").unlink(missing_ok=True)
            stage.rmdir()


def collect(args: argparse.Namespace) -> bool:
    """Collect and validate a bundle while leaving source and Task files unchanged."""
    task = load_document(args.task_file)
    if validate_task_state(task, load_document(DEFAULT_SCHEMA_PATH)):
        raise CollectionError("TASK_SCHEMA")
    is_sstd = task["source_type"] == "SSTD_CHANGE"
    prefix = "sstd-sync-" if is_sstd else "sstc-feature-"
    if not task["task_id"].startswith(prefix):
        raise CollectionError("TASK_PREFIX")
    check_content(task["source_reference"].encode("utf-8"))
    if is_sstd:
        if not (args.source_repository and args.sstd_base and args.sstd_path) or args.request_file:
            raise CollectionError("SOURCE_ARGUMENTS")
    elif not args.request_file or any((args.source_repository, args.sstd_base, args.sstd_path,
                                      getattr(args, "sstd_context", None))):
        raise CollectionError("REQUEST_ARGUMENTS")
    contexts = selected_paths(args.sstc_context)
    sstd_paths = selected_paths(args.sstd_path) if is_sstd else []
    sstc = Git(args.sstc_repository)
    source = Git(args.source_repository) if is_sstd else None
    output = output_path(args.output_directory, [sstc] + ([source] if source else []))
    revision = sstc.resolve(args.sstc_revision)
    bundle = Bundle(task, revision)
    if source:
        source_diff(bundle, source, args.sstd_base, sstd_paths)
        for path in selected_paths(getattr(args, "sstd_context", None) or []):
            target = bundle.manifest["input_context"]["sstd_revision"]
            bundle.add("supporting", "SSTD", target, path, source.file(target, path))
    else:
        check_path(args.request_file.name)
        if args.request_file.is_symlink():
            raise CollectionError("REQUEST_FILE")
        raw = None
        if args.request_file.exists():
            if not stat.S_ISREG(args.request_file.stat().st_mode):
                raise CollectionError("REQUEST_FILE")
            with args.request_file.open("rb") as stream:
                raw = stream.read(MAX_EVIDENCE_BYTES + 1)
            if len(raw) > MAX_EVIDENCE_BYTES:
                raise CollectionError("REQUEST_SIZE")
            check_content(raw)
            bundle.manifest["input_context"]["request_sha256"] = hashlib.sha256(raw).hexdigest()
        bundle.add("request", "REQUEST", None, None, raw)
    instructions = {"AGENTS.md"}
    for path in contexts:
        bundle.add("sstc_context", "SSTC", revision, path, sstc.file(revision, path))
        for parent in PurePosixPath(path).parents:
            instructions.add(str(parent / "AGENTS.md"))
    for path in sorted(instructions, key=lambda value: (value.count("/"), value)):
        raw = sstc.file(revision, path)
        if raw is not None or path == "AGENTS.md":
            bundle.add("target_instructions", "SSTC", revision, path, raw)
    schema = load_document(SCHEMA_PATH)
    if validate_shape(bundle.manifest, schema["$defs"]["manifest"], schema, "manifest"):
        raise CollectionError("MANIFEST_SCHEMA")
    # Construction binds every record to Task and pinned revisions. Recheck hashes
    # against the exact bytes to be written, without fabricating an AI result.
    for item in bundle.manifest["evidence"]:
        body = bundle.bodies.get(item["evidence_id"])
        if item["missing"] != (body is None):
            raise CollectionError("EVIDENCE_MISSING")
        if body is not None and hashlib.sha256(body).hexdigest() != item["content_sha256"]:
            raise CollectionError("EVIDENCE_HASH")
        if item["source"] == "REQUEST" and item["content_sha256"] != item["request_sha256"]:
            raise CollectionError("REQUEST_HASH")
    # Recheck destination after collection, immediately before creating any files.
    output_path(output, [sstc] + ([source] if source else []))
    publish(bundle, output)
    return bundle.complete()
