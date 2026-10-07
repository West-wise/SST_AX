"""Pinned Git context selection for both Controller input routes."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
import subprocess

from impact_collection import Git, MAX_EVIDENCE, check_path, git_environment
from github_validation import SHA, require

REPOSITORIES = {"SSTD_CHANGE": "West-wise/Server_State_Telemetry_Demon",
                "SSTC_FEATURE": "West-wise/Server_State_Telemetry_Client"}


def ensure_revision(repository: Path, revision: str, source: str) -> None:
    require(SHA.fullmatch(revision) is not None, "PINNED_REVISION_REQUIRED")
    git = Git(repository)
    try:
        require(git.resolve(revision) == revision, "REVISION_MISMATCH")
        return
    except ValueError:
        pass
    # Only acquire the already authenticated API's immutable commit. No checkout,
    # source-worktree edit, branch update or tag creation is performed.
    result = subprocess.run(["git", "-C", str(repository), "config", "--get", "remote.origin.url"],
                            capture_output=True, env=git_environment(), timeout=15)
    remote = result.stdout.decode("utf-8").strip()
    name = REPOSITORIES[source]
    require(remote in {f"https://github.com/{name}", f"https://github.com/{name}.git",
                       f"git@github.com:{name}.git"}, "SOURCE_REMOTE_REFUSED")
    environment = git_environment()
    environment["GIT_ALLOW_PROTOCOL"] = "https:ssh"
    environment.pop("GIT_CONFIG_GLOBAL", None)
    fetched = subprocess.run(["git", "-C", str(repository), "-c", "core.hooksPath=",
                              "fetch", "--no-tags", "origin", revision],
                             capture_output=True, env=environment, timeout=60, check=False)
    require(fetched.returncode == 0 and git.resolve(revision) == revision, "SOURCE_FETCH_FAILED")


def files(git: Git, revision: str) -> list[str]:
    return [check_path(raw.decode("utf-8")) for raw in
            git.run(["ls-tree", "-r", "--name-only", "-z", revision]).split(b"\0") if raw]


def select_context(sstc_repository: Path, sstc_revision: str,
                   sstd_repository: Path | None = None, sstd_revision: str | None = None,
                   sstd_base: str | None = None) -> dict:
    """Include all Android source consumers, refusing partial oversized selection."""
    client = Git(sstc_repository)
    require(client.resolve(sstc_revision) == sstc_revision, "CLIENT_REVISION_MISMATCH")
    all_client = files(client, sstc_revision)
    contexts = sorted(path for path in all_client if
                      re.fullmatch(r"app/src/.+\.(kt|java|xml)", path) or
                      re.fullmatch(r"(?:app/)?build\.gradle(?:\.kts)?", path) or
                      path.startswith(".github/workflows/"))
    require(any(path.startswith("app/src/") for path in contexts), "ANDROID_CONTEXT_REQUIRED")
    require("AGENTS.md" in all_client and any(path.startswith(".github/workflows/")
                                            for path in contexts), "TARGET_RULES_AND_CI_REQUIRED")
    instructions = {"AGENTS.md"}
    for path in contexts:
        instructions.update(str(parent / "AGENTS.md") for parent in PurePosixPath(path).parents)
    instructions.intersection_update(all_client)
    paths, supporting = [], []
    if sstd_repository is not None:
        server = Git(sstd_repository)
        require(server.resolve(sstd_revision) == sstd_revision, "SERVER_REVISION_MISMATCH")
        if sstd_base != "ROOT":
            server.run(["merge-base", "--is-ancestor", sstd_base, sstd_revision])
        args = (["diff", sstd_base, sstd_revision] if sstd_base != "ROOT" else
                ["diff-tree", "--root", "--no-commit-id", "-r", sstd_revision])
        raw = server.run([*args, "--no-renames", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "--"])
        paths = sorted(check_path(name.decode("utf-8")) for name in raw.split(b"\0") if name)
        require(paths, "SERVER_CHANGE_REQUIRED")
        supporting = sorted(path for path in files(server, sstd_revision) if path not in paths and
                            (path == "AGENTS.md" or
                             re.search(r"(?i)(protocol|serializ|message|metric|unit|packet|schema|model|"
                                       r"systemreader|systeminfo|collector|telemetry|socket|cpu|memory|disk)", path) and
                             re.search(r"\.(h|hpp|cpp|cc|json|md)$", path)))
    require(len(paths) <= MAX_EVIDENCE and len(contexts) + len(instructions) + len(supporting) + 1 <= MAX_EVIDENCE,
            "CONTEXT_BUDGET_REQUIRES_REVIEW")
    return {"sstc_context": contexts, "sstd_path": paths, "sstd_context": supporting,
            "coverage": "ALL_PINNED_ANDROID_SOURCE_CONSUMERS"}
