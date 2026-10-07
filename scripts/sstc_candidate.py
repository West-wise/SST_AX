"""Bounded Android source snapshots; credentials never enter Git objects or API bodies."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

from codex_impact import regular, worker_environment
from impact_collection import check_content, check_path, MAX_EVIDENCE_BYTES, MAX_TOTAL_BYTES

SHA = re.compile(r"[0-9a-f]{40}")


def git(path: Path, args: list[str], *, raw: bytes | None = None, index: str | None = None) -> bytes:
    env = worker_environment()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_PROTOCOL_FROM_USER="0")
    if index is not None:
        env["GIT_INDEX_FILE"] = index
    if args and args[0] in {"diff", "show", "log"}:
        args = [args[0], "--no-ext-diff", "--no-textconv", *args[1:]]
    result = subprocess.run(["git", "--no-replace-objects", "-c", "core.fsmonitor=false", *args], cwd=path,
                            env=env, input=raw, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("CANDIDATE_GIT_FAILED")
    return result.stdout


def candidate_snapshot(worktree: Path, revision: str, branch: str) -> list[dict]:
    if not SHA.fullmatch(revision) or not branch.startswith("ax/sstc-sync/"):
        raise ValueError("CANDIDATE_CONTEXT_REQUIRED")
    if (git(worktree, ["rev-parse", "HEAD"]).decode().strip() != revision or
            git(worktree, ["symbolic-ref", "--short", "HEAD"]).decode().strip() != branch or
            Path(git(worktree, ["rev-parse", "--show-toplevel"]).decode().strip()).resolve() != worktree.resolve()):
        raise ValueError("CANDIDATE_HEAD_OR_BRANCH_CHANGED")
    paths = set(git(worktree, ["diff", "--name-only", "--no-renames", "-z", revision, "--"]).decode("utf-8").split("\0"))
    paths.update(git(worktree, ["ls-files", "--others", "--exclude-standard", "-z"]).decode("utf-8").split("\0"))
    paths.discard("")
    if not 1 <= len(paths) <= 20:
        raise ValueError("CANDIDATE_CHANGE_COUNT")
    files, total = [], 0
    for name in sorted(paths):
        check_path(name)
        if (not re.fullmatch(r"app/src/[A-Za-z0-9_./-]+\.(kt|java|xml)", name) or
                name.endswith("/AndroidManifest.xml") or any(part.startswith(".") for part in name.split("/"))):
            raise ValueError("CANDIDATE_SCOPE_REQUIRES_REVIEW")
        mode = git(worktree, ["ls-tree", revision, "--", name]).decode().split(" ", 1)[0]
        if mode not in {"", "100644"}:
            raise ValueError("CANDIDATE_FILE_MODE_REFUSED")
        path = worktree / name
        if path.exists() or path.is_symlink():
            regular(path)
            with path.open("rb") as stream:
                content = stream.read(MAX_EVIDENCE_BYTES + 1)
            total += len(content)
            if len(content) > MAX_EVIDENCE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("CANDIDATE_SIZE_LIMIT")
            check_content(content)
            content.decode("utf-8")
            if b"\0" in content:
                raise ValueError("CANDIDATE_BINARY_REFUSED")
            digest = hashlib.sha256(content).hexdigest()
        else:
            # Check deleted paths' parents too, before treating absence as deletion.
            for parent in path.parents:
                if parent.is_symlink():
                    raise ValueError("CANDIDATE_LINK_REFUSED")
            digest = None
        files.append({"path": name, "mode": "100644", "sha256": digest})
    return files


def tree_payload(worktree: Path, revision: str, branch: str, files: list[dict]) -> tuple[str, list[dict]]:
    if candidate_snapshot(worktree, revision, branch) != files:
        raise ValueError("CANDIDATE_CHANGED")
    entries = []
    # A private index leaves the worktree's staged/unstaged edits intact. hash-object
    # bypasses repository filters; only already-scanned bytes become Git objects.
    with tempfile.TemporaryDirectory(prefix="sst-ax-index-") as temporary:
        index = str(Path(temporary) / "index")
        git(worktree, ["read-tree", revision], index=index)
        for item in files:
            name = item["path"]
            entry = {"path": name, "mode": "100644", "type": "blob"}
            if item["sha256"] is None:
                git(worktree, ["update-index", "--force-remove", "--", name], index=index)
                entry["sha"] = None
            else:
                regular(worktree / name)
                with (worktree / name).open("rb") as stream:
                    raw = stream.read(MAX_EVIDENCE_BYTES + 1)
                if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    raise ValueError("CANDIDATE_CHANGED")
                check_content(raw)
                blob = git(worktree, ["hash-object", "-w", "--stdin"], raw=raw).decode().strip()
                git(worktree, ["update-index", "--add", "--cacheinfo", "100644", blob, name], index=index)
                entry["content"] = raw.decode("utf-8")
            entries.append(entry)
        tree = git(worktree, ["write-tree"], index=index).decode().strip()
    if candidate_snapshot(worktree, revision, branch) != files:
        raise ValueError("CANDIDATE_CHANGED")
    return tree, entries
