"""Polling intake and resumable, one-step Controller for SSTD and SSTC requests."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time

from codex_impact import bundle, canonical
from controller_inputs import REPOSITORIES, ensure_revision, select_context
from create_task import build_task_log, build_task_state, next_task_id, utc_now
from github_validation import GitHub, SHA, require
from impact_collection import check_content, collect
from impact_validation import load_document
from task_storage import atomic_json, checkpoint_path, companion, recover_pair, save_pair, task_lock, validate_pair

TERMINAL = {"READY_FOR_REVIEW", "COMPLETED", "REJECTED", "ANALYSIS_FAILED", "IMPLEMENTATION_FAILED",
            "BUILD_FAILED", "TEST_FAILED", "SECURITY_REVIEW_FAILED", "PROTOCOL_APPROVAL_REQUIRED"}


def configuration(path: Path) -> dict:
    config = load_document(path)
    allowed = {"sstd_repository", "sstc_repository", "state_directory", "worktree_directory",
               "poll_seconds", "bootstrap_sstd_base"}
    require(isinstance(config, dict) and set(config) <= allowed and
            allowed <= set(config), "CONTROLLER_CONFIGURATION_REQUIRED")
    check_content(canonical(config))
    require(type(config["poll_seconds"]) is int and 15 <= config["poll_seconds"] <= 3600,
            "POLL_INTERVAL_REFUSED")
    for key in ("sstd_repository", "sstc_repository", "state_directory", "worktree_directory"):
        value = config[key]
        require(isinstance(value, str) and Path(value).is_absolute(), "ABSOLUTE_CONTROLLER_PATH_REQUIRED")
        config[key] = Path(value)
    bootstrap = config.get("bootstrap_sstd_base")
    require(bootstrap == "ROOT" or
            isinstance(bootstrap, str) and SHA.fullmatch(bootstrap), "BOOTSTRAP_REVISION_REFUSED")
    return config


def move(path: Path, target: str, reason: str) -> None:
    from update_task_state import update
    args = argparse.Namespace(task_file=path, status=target, reason=reason, recover=False,
                              record_reset_at=None, deferred_until=None)
    with task_lock(path):
        require(update(args) == 0, "CONTROLLER_TRANSITION_FAILED")


def materialize(event: dict, tasks: Path) -> Path:
    """The journal's reserved Task ID is persisted before this transaction."""
    path = tasks / (event["task_id"] + ".json")
    with task_lock(path):
        if companion(path, "pending").exists():
            recover_pair(path)
        if path.exists():
            state, log = load_document(path), load_document(companion(path, "log"))
            validate_pair(path, state, log)
            require(state["source_type"] == event["source_type"] and
                    state["source_reference"] == event["source_reference"], "INTAKE_TASK_ID_COLLISION")
        else:
            args = argparse.Namespace(source_type=event["source_type"],
                                      source_reference=event["source_reference"], risk_level="MEDIUM",
                                      approval_reason=None, checkpoint_path=None, branch=None)
            now = utc_now()
            state = build_task_state(args, event["task_id"], now)
            save_pair(path, state, build_task_log(state, now))
        if event["source_type"] == "SSTC_FEATURE" and not event.get("input_error"):
            request = path.with_name(path.stem + ".request.md")
            raw = event["request"].encode("utf-8")
            require(hashlib.sha256(raw).hexdigest() == event["request_sha256"], "REQUEST_SNAPSHOT_CHANGED")
            if request.exists():
                require(request.read_bytes() == raw, "REQUEST_SNAPSHOT_CHANGED")
            else:
                with request.open("xb") as stream:
                    stream.write(raw)
    return path


def discover(journal: dict, config: dict, client) -> list[dict]:
    """Read immutable identities; a main commit and its Release share one key."""
    server = "repos/" + REPOSITORIES["SSTD_CHANGE"]
    main = client.api(server + "/commits/main")
    target = main.get("sha")
    require(isinstance(target, str) and SHA.fullmatch(target), "SERVER_HEAD_REQUIRED")
    previous = journal.get("sstd_cursor")
    parents = main.get("parents", [])
    base = previous or config["bootstrap_sstd_base"]
    require(base == "ROOT" or isinstance(base, str) and SHA.fullmatch(base), "SERVER_BASE_REQUIRED")
    events = []
    if target != previous and target != base:
        events.append({"key": "sstd:" + target, "source_type": "SSTD_CHANGE",
                       "source_reference": target, "source_revision": target, "base_revision": base})
    releases = client.api(server + "/releases?per_page=100")
    require(isinstance(releases, list) and len(releases) < 100, "RELEASE_POLL_LIMIT_REQUIRES_REVIEW")
    release_ids = []
    for release in releases:
        release_id = release.get("id")
        require(type(release_id) is int and release_id > 0, "RELEASE_ID_REQUIRED")
        if release.get("draft") or release.get("prerelease"):
            continue
        release_ids.append(release_id)
        # Bootstrap via the explicitly selected main range. Historical Releases
        # establish a cursor; they must not create backward contract work.
        if "release_ids" not in journal or release_id in journal["release_ids"]:
            continue
        from urllib.parse import quote
        tag = release.get("tag_name")
        require(isinstance(tag, str) and 0 < len(tag) <= 200 and
                not any(ord(char) < 32 for char in tag), "RELEASE_TAG_REFUSED")
        commit = client.api(server + "/commits/" + quote(tag, safe=""))
        revision = commit.get("sha")
        require(isinstance(revision, str) and SHA.fullmatch(revision), "RELEASE_COMMIT_REQUIRED")
        if revision == target:
            continue
        comparison = client.api(server + "/compare/" + revision + "..." + target + "?per_page=1")
        require(isinstance(comparison, dict) and comparison.get("status") in
                {"ahead", "behind", "diverged", "identical"} and
                comparison.get("base_commit", {}).get("sha") == revision and
                isinstance(comparison.get("merge_base_commit", {}).get("sha"), str) and
                SHA.fullmatch(comparison["merge_base_commit"]["sha"]), "RELEASE_ANCESTRY_REQUIRES_REVIEW")
        if comparison["status"] in {"ahead", "identical"}:
            require(comparison["merge_base_commit"]["sha"] == revision, "RELEASE_ANCESTRY_REQUIRES_REVIEW")
            # The observed main range already covers this contract; never adapt
            # the client backward to an intermediate commit released later.
            continue
        parents = commit.get("parents", [])
        events.append({"key": "sstd:" + revision, "source_type": "SSTD_CHANGE",
                       "source_reference": revision, "source_revision": revision,
                       "base_revision": parents[0]["sha"] if parents else "ROOT"})
    journal["release_ids"] = sorted(set(journal.get("release_ids", [])) | set(release_ids))
    issues = client.api("repos/" + REPOSITORIES["SSTC_FEATURE"] + "/issues?state=open&per_page=100")
    require(isinstance(issues, list) and len(issues) < 100, "ISSUE_POLL_LIMIT_REQUIRES_REVIEW")
    for index, issue in enumerate(issues):
        if "pull_request" in issue or not isinstance(issue.get("title"), str) or not issue["title"].startswith("[AX]"):
            continue
        number, body = issue.get("number"), issue.get("body")
        if type(number) is not int or number <= 0:
            journal.setdefault("rejected_events", {})["issue-index-" + str(index)] = "ISSUE_ID_REFUSED"
            continue
        raw, error = b"", None
        try:
            require(isinstance(body, str) and body.strip(), "ISSUE_REQUEST_REQUIRED")
            raw = (issue["title"] + "\n\n" + body).encode("utf-8")
            require(len(raw) <= 65_536, "ISSUE_REQUEST_SIZE_LIMIT")
            check_content(raw)
        except (ValueError, TypeError, UnicodeError):
            error = "ISSUE_INPUT_MISSING_OVERSIZED_OR_SECRET_REJECTED"
        checksum = hashlib.sha256(raw).hexdigest()
        if error:
            events.append({"key": f"issue:{number}:rejected:{checksum}", "source_type": "SSTC_FEATURE",
                           "source_reference": f"https://github.com/{REPOSITORIES['SSTC_FEATURE']}/issues/{number}",
                           "input_error": error})
            continue
        events.append({"key": f"issue:{number}:{checksum}", "source_type": "SSTC_FEATURE",
                       "source_reference": f"https://github.com/{REPOSITORIES['SSTC_FEATURE']}/issues/{number}",
                       "request": raw.decode("utf-8"), "request_sha256": checksum})
    journal["sstd_cursor"] = target
    return events


def prepare_inputs(path: Path, event: dict, config: dict, client) -> Path:
    inputs = path.with_name(path.stem + ".inputs")
    if inputs.exists():
        manifest, _ = bundle(load_document(path), inputs)
        require(manifest["input_context"]["sstc_revision"] == event["sstc_revision"], "INTAKE_CLIENT_REVISION_CHANGED")
        require(not any(item["required"] and (item["missing"] or item["truncated"])
                        for item in manifest["evidence"]), "INPUT_CONTEXT_INCOMPLETE")
        return inputs
    client_revision = event["sstc_revision"]
    ensure_revision(config["sstc_repository"], client_revision, "SSTC_FEATURE")
    server = event["source_type"] == "SSTD_CHANGE"
    if server:
        ensure_revision(config["sstd_repository"], event["source_revision"], "SSTD_CHANGE")
        if event["base_revision"] != "ROOT":
            ensure_revision(config["sstd_repository"], event["base_revision"], "SSTD_CHANGE")
    selection = select_context(config["sstc_repository"], client_revision,
                               config["sstd_repository"] if server else None,
                               event.get("source_revision"), event.get("base_revision"))
    event["context_coverage"] = selection["coverage"]
    args = argparse.Namespace(task_file=path, sstc_repository=config["sstc_repository"],
                              sstc_revision=client_revision, sstc_context=selection["sstc_context"],
                              output_directory=inputs, source_repository=config["sstd_repository"] if server else None,
                              sstd_base=event.get("base_revision"), sstd_path=selection["sstd_path"] if server else None,
                              sstd_context=selection["sstd_context"],
                              request_file=path.with_name(path.stem + ".request.md") if not server else None)
    started = utc_now()
    complete = collect(args)
    with task_lock(path):
        state, log = load_document(path), load_document(companion(path, "log"))
        log["commands"].append({"name": "controller-input-collector", "command": "impact_collection.collect",
                                "exit_code": 0 if complete else 1, "started_at": started,
                                "finished_at": utc_now(), "artifact_paths": [str(inputs / "manifest.json")]})
        save_pair(path, state, log)
    require(complete, "INPUT_CONTEXT_INCOMPLETE")
    return inputs


def drive(path: Path, event: dict, config: dict, client) -> str:
    """Advance one state. Side effects have their own persisted, exact receipts."""
    state = load_document(path)
    status = state["status"]
    if status == "DEFERRED_RATE_LIMIT":
        deadline = state.get("deferred_until")
        if not deadline or datetime.fromisoformat(deadline.replace("Z", "+00:00")) > datetime.now(timezone.utc):
            return status
        checkpoint = load_document(checkpoint_path(path))
        resume_status = checkpoint["resume_status"]
        # The Worker validates the original policy/Slack authority before this
        # transition; the generic state CLI cannot authorize approved HIGH work.
        if resume_status != "IMPLEMENTING":
            move(path, resume_status, "Observed usage reset elapsed")
        status = resume_status
    if status in TERMINAL or status == "WAITING_APPROVAL":
        return status
    if status == "RECEIVED":
        move(path, "ANALYZING", "Controller intake received fixed source")
        status = "ANALYZING"
    if status == "ANALYZING":
        if event.get("input_error"):
            move(path, "ANALYSIS_FAILED", event["input_error"])
            return "ANALYSIS_FAILED"
        inputs = prepare_inputs(path, event, config, client)
        log = load_document(companion(path, "log"))
        if log.get("codex_analysis", {}).get("outcome") != "VALID":
            from codex_impact import run
            outcome = run(path, inputs, "codex")
            if outcome != "VALID":
                return load_document(path)["status"]
        from execution_policy import decide
        return decide(path, inputs)["status"]
    if status == "IMPLEMENTING":
        inputs = path.with_name(path.stem + ".inputs")
        log = load_document(companion(path, "log"))
        receipt = log.get("codex_worker")
        if receipt is None or receipt.get("outcome") in {"RATE_LIMIT", "CODEX_FAILED", "TIMEOUT", "INTERRUPTED"}:
            from sstc_worker import run
            worktree = Path(receipt["worktree"]) if receipt else config["worktree_directory"] / path.stem
            branch = receipt["branch"] if receipt else "ax/sstc-sync/" + path.stem
            outcome = run(path, config["sstc_repository"], worktree, branch, inputs,
                          validation_mode="github", resume=receipt is not None)
            if outcome != "IMPLEMENTED":
                return load_document(path)["status"]
        else:
            require(receipt.get("outcome") == "IMPLEMENTED", "WORKER_FAILURE_REQUIRES_REVIEW")
        from sstc_pipeline import publish
        publish(path, inputs, client)
        return load_document(path)["status"]
    if status == "VALIDATING":
        from sstc_pipeline import check
        check(path, client)
        return load_document(path)["status"]
    raise ValueError("CONTROLLER_STATUS_REFUSED")


def run_once(config: dict, client=None, approval=None) -> dict:
    client = client or GitHub()
    state_directory = config["state_directory"]
    tasks = state_directory / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    config["worktree_directory"].mkdir(parents=True, exist_ok=True)
    path = state_directory / "controller.json"
    with task_lock(path):
        journal = load_document(path) if path.exists() else {"schema_version": "1.0", "events": {}, "sstd_cursor": None}
        require(journal.get("schema_version") == "1.0" and isinstance(journal.get("events"), dict), "CONTROLLER_JOURNAL_REFUSED")
        # Repair every reserved identity before assigning any further daily ID.
        for event in journal["events"].values():
            materialize(event, tasks)
        new_events = discover(journal, config, client)
        client_commit = client.api("repos/" + REPOSITORIES["SSTC_FEATURE"] + "/commits/main").get("sha")
        require(isinstance(client_commit, str) and SHA.fullmatch(client_commit), "CLIENT_HEAD_REQUIRED")
        for event in new_events:
            key = event["key"]
            if key in journal["events"]:
                continue
            event.update(task_id=next_task_id(event["source_type"], tasks), sstc_revision=client_commit,
                         failures=0, active_seconds=0, last_error=None)
            journal["events"][key] = event
            atomic_json(path, journal)
            materialize(event, tasks)
        def poll_approvals(pending):
            nonlocal approval
            try:
                if approval is None:
                    import os
                    from slack_runner import configuration as slack_configuration, listen_many
                    approval = lambda paths: listen_many(paths, slack_configuration(os.environ), timeout_seconds=5)
                require(approval(pending) == 0, "SLACK_PENDING_TRANSPORT_FAILED")
            except Exception:
                journal["approval_error"] = "SLACK_CONFIGURATION_OR_TRANSPORT_REQUIRED"
            else:
                journal.pop("approval_error", None)

        existing_waiting = [tasks / (event["task_id"] + ".json") for event in journal["events"].values()
                            if load_document(tasks / (event["task_id"] + ".json"))["status"] == "WAITING_APPROVAL"]
        # Service existing decisions before starting a potentially long Codex turn.
        if existing_waiting:
            poll_approvals(existing_waiting)
        waiting, outcomes = [], {}
        for key, event in journal["events"].items():
            task = tasks / (event["task_id"] + ".json")
            started = time.monotonic()
            if event["failures"] >= 3:
                outcomes[event["task_id"]] = "ESCALATION_REQUIRED"
                continue
            try:
                status = drive(task, event, config, client)
                if status == "WAITING_APPROVAL":
                    waiting.append(task)
                outcomes[event["task_id"]] = status
                event["last_error"] = None
            except Exception:
                event["failures"] += 1
                event["last_error"] = "CONTROLLER_INPUT_EXECUTION_OR_CONTEXT_FAILED"
                status = load_document(task)["status"]
                if status == "ANALYZING":
                    move(task, "ANALYSIS_FAILED", event["last_error"])
                    status = "ANALYSIS_FAILED"
                outcomes[event["task_id"]] = status
            event["active_seconds"] += time.monotonic() - started
            atomic_json(path, journal)
        if any(task not in existing_waiting for task in waiting):
            poll_approvals(waiting)
        atomic_json(path, journal)
        return {"outcomes": outcomes, "pending_approvals": len(waiting),
                "approval_error": journal.get("approval_error")}
