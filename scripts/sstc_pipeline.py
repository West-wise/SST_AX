"""Controller publication and Draft PR production, bound to verified Slack approval."""
from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import quote

from codex_impact import regular
from github_validation import (GitHub, REPOSITORY, SHA, check_validation, digest,
                               positive, request_validation, require, snapshot, validate_receipt)
from impact_collection import check_content
from impact_validation import load_document
from sstc_candidate import candidate_snapshot, git, tree_payload
from sstc_worker import BRANCH, codex_command
from task_storage import atomic_json, companion, save_pair, task_lock, validate_pair
from update_task_state import utc_now
from execution_policy import BudgetClient, execution_context

PREFIX = "repos/" + REPOSITORY


def record_path(path: Path) -> Path:
    return companion(path, "pipeline")


def pair(path: Path) -> tuple[dict, dict]:
    require(not companion(path, "pending").exists(), "RECOVER_PAIR_FIRST")
    state, log = load_document(path), load_document(companion(path, "log"))
    validate_pair(path, state, log)
    return state, log


def transition(state: dict, log: dict, target: str, reason: str) -> None:
    old, now = state["status"], utc_now()
    state.update(status=target, updated_at=now)
    log["status"] = target
    log["state_transitions"].append({"from_status": old, "to_status": target,
                                     "occurred_at": now, "reason": reason})


def context(path: Path, record: dict) -> tuple[dict, dict, dict]:
    state, log = pair(path)
    require(record["task_id"] == path.stem and record["repository"] == REPOSITORY and
            snapshot(path) == record["task_snapshot"], "PIPELINE_CONTEXT_CHANGED")
    ctx = execution_context(path, Path(record["input_directory"]))
    manifest, session = ctx["manifest"], ctx["session_id"]
    require(__import__("codex_impact").digest(ctx["proof"]) == record["authority_sha256"], "EXECUTION_AUTHORITY_CHANGED")
    require(session == record["session_id"], "PIPELINE_SESSION_CHANGED")
    worktree = Path(record["worktree"])
    require(candidate_snapshot(worktree, record["source_revision"], record["branch"]) ==
            record["candidate_files"], "CANDIDATE_CHANGED")
    return state, log, manifest


def post(path: Path, record: dict, client, endpoint: str, payload: dict, outcome: str):
    record["outcome"] = outcome
    entry = {"command": ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
             "exit_code": None, "started_at": utc_now(), "finished_at": None}
    record.setdefault("commands", []).append(entry)
    # Journal before POST: a lost response must never cause a blind repeat.
    atomic_json(record_path(path), record)
    response = client.api(endpoint, payload)
    entry.update(exit_code=0, finished_at=utc_now())
    atomic_json(record_path(path), record)
    return response


def verify_commit(client, record: dict) -> None:
    sha = record["candidate_sha"]
    require(isinstance(sha, str) and SHA.fullmatch(sha) is not None, "CANDIDATE_SHA_REQUIRED")
    commit = client.api(PREFIX + "/git/commits/" + sha)
    require(commit.get("sha") == sha and commit.get("tree", {}).get("sha") == record["tree_sha"] and
            [item.get("sha") for item in commit.get("parents", [])] == [record["source_revision"]],
            "CANDIDATE_COMMIT_MISMATCH")


def verify_ref(client, record: dict) -> None:
    ref = client.api(PREFIX + "/git/ref/heads/" + quote(record["branch"], safe="/"))
    require(ref.get("ref") == "refs/heads/" + record["branch"] and
            ref.get("object", {}).get("type") == "commit" and
            ref["object"].get("sha") == record["candidate_sha"], "REMOTE_BRANCH_CHANGED")


def mark_published(path: Path, record: dict, state: dict, log: dict) -> None:
    """Journal the exact publication transition before changing the Task pair."""
    if "publication_pair" not in record:
        transition(state, log, "VALIDATING", "Controller published approval-bound candidate")
        log["sstc_candidate"] = {key: record[key] for key in
            ("repository", "source_revision", "candidate_sha", "tree_sha", "branch", "candidate_files")}
        log["commands"].append({"name": "sstc-pipeline-publish",
            "command": ["run_sstc_pipeline.py", "publish", "--task-file", str(path)],
            "exit_code": 0, "started_at": utc_now(), "finished_at": utc_now(),
            "artifact_paths": [record_path(path).name]})
        record["publication_pair"] = {"state": state, "log": log}
        record["publication_snapshot"] = {"task_id": path.stem, "state": digest(state),
            "log": digest(log), "checkpoint": record["task_snapshot"]["checkpoint"]}
        atomic_json(record_path(path), record)
    else:
        intended = record["publication_pair"]
        validate_pair(path, intended["state"], intended["log"])
        expected_state, expected_log = copy.deepcopy(state), copy.deepcopy(log)
        now = intended["state"]["updated_at"]
        expected_state.update(status="VALIDATING", updated_at=now)
        expected_log["status"] = "VALIDATING"
        expected_log["state_transitions"].append({"from_status": "IMPLEMENTING", "to_status": "VALIDATING",
            "occurred_at": now, "reason": "Controller published approval-bound candidate"})
        expected_log["sstc_candidate"] = {key: record[key] for key in
            ("repository", "source_revision", "candidate_sha", "tree_sha", "branch", "candidate_files")}
        entry = intended["log"]["commands"][-1]
        expected_log["commands"].append({"name": "sstc-pipeline-publish",
            "command": ["run_sstc_pipeline.py", "publish", "--task-file", str(path)],
            "exit_code": 0, "started_at": entry.get("started_at"), "finished_at": entry.get("finished_at"),
            "artifact_paths": [record_path(path).name]})
        require(intended == {"state": expected_state, "log": expected_log}, "PUBLICATION_TRANSITION_REFUSED")
    intended = record["publication_pair"]
    validate_pair(path, intended["state"], intended["log"])
    require(intended["state"]["status"] == "VALIDATING", "PUBLICATION_TRANSITION_REFUSED")
    save_pair(path, intended["state"], intended["log"])
    require(snapshot(path) == record["publication_snapshot"], "PUBLICATION_TRANSITION_REFUSED")
    record.update(outcome="DISPATCH_UNCERTAIN", task_snapshot=snapshot(path))
    atomic_json(record_path(path), record)


def publish(task_file: Path, input_directory: Path, client=None) -> dict:
    path = task_file.absolute()
    client = BudgetClient(client or GitHub(), path)
    with task_lock(path):
        state, log = pair(path)
        require(state["status"] == "IMPLEMENTING", "IMPLEMENTING_REQUIRED")
        require(not record_path(path).exists() and not companion(path, "github-validation").exists(),
                "PIPELINE_EXISTS_NO_AUTOMATIC_REPUBLISH")
        worker = log.get("codex_worker", {})
        require(worker.get("outcome") == "IMPLEMENTED" and worker.get("validation_mode") in {"github", "local"},
                "IMPLEMENTATION_REQUIRED")
        if worker["validation_mode"] == "local":
            receipt = worker.get("validation", {})
            require(isinstance(receipt, dict) and
                    receipt.get("command") == "./gradlew testDebugUnitTest assembleDebug" and
                    type(receipt.get("exit_code")) is int and receipt["exit_code"] == 0 and
                    worker.get("validation_plan", {}).get("revision") == worker.get("source_revision") and
                    worker["validation_plan"].get("jdk") == "17" and
                    worker["validation_plan"].get("commands") ==
                    ["chmod +x gradlew", "./gradlew testDebugUnitTest assembleDebug"],
                    "LOCAL_VALIDATION_RECEIPT_REQUIRED")
        ctx = execution_context(path, input_directory.absolute())
        entry = log["commands"][-1]
        manifest, session = ctx["manifest"], ctx["session_id"]
        require(entry.get("name") == "codex-sstc-worker" and type(entry.get("exit_code")) is int and
                entry["exit_code"] == 0 and entry.get("finished_at") and
                entry.get("started_at") == worker.get("started_at") and
                entry.get("command") == codex_command(entry["command"][0], session) and
                worker.get("session_id") == session, "WORKER_RECEIPT_REFUSED")
        revision, branch = worker["source_revision"], worker["branch"]
        require(BRANCH.fullmatch(branch) is not None and revision == manifest["input_context"]["sstc_revision"],
                "WORKER_CONTEXT_REFUSED")
        worktree, files = Path(worker["worktree"]), worker["candidate_files"]
        require(candidate_snapshot(worktree, revision, branch) == files, "CANDIDATE_CHANGED")
        expected_tree, entries = tree_payload(worktree, revision, branch, files)
        source = client.api(PREFIX + "/git/commits/" + revision)
        base_tree = git(worktree, ["rev-parse", revision + "^{tree}"]).decode().strip()
        require(source.get("sha") == revision and source.get("tree", {}).get("sha") == base_tree,
                "SOURCE_COMMIT_MISMATCH")
        refs = client.api(PREFIX + "/git/matching-refs/heads/" + quote(branch, safe="/"))
        require(isinstance(refs, list) and not any(item.get("ref") == "refs/heads/" + branch for item in refs),
                "REMOTE_BRANCH_ALREADY_EXISTS")
        record = {"schema_version": "1.0", "repository": REPOSITORY, "task_id": state["task_id"],
                  "input_directory": str(input_directory.absolute()), "session_id": session,
                  "worktree": str(worktree), "branch": branch, "source_revision": revision,
                  "candidate_files": files, "tree_sha": expected_tree, "candidate_sha": None,
                  "authority_sha256": __import__("codex_impact").digest(ctx["proof"]),
                  "analysis_record": copy.deepcopy(ctx["record"]),
                  "task_snapshot": snapshot(path), "outcome": "PUBLISH_UNCERTAIN"}
        tree = post(path, record, client, PREFIX + "/git/trees",
                    {"base_tree": base_tree, "tree": entries}, "PUBLISH_UNCERTAIN")
        require(tree.get("sha") == expected_tree, "PUBLISHED_TREE_MISMATCH")
        commit = post(path, record, client, PREFIX + "/git/commits",
                      {"message": "SSTC: " + path.stem + " 승인된 변경", "tree": expected_tree,
                       "parents": [revision]}, "PUBLISH_UNCERTAIN")
        record["candidate_sha"] = commit.get("sha")
        verify_commit(client, record)
        post(path, record, client, PREFIX + "/git/refs",
             {"ref": "refs/heads/" + branch, "sha": record["candidate_sha"]}, "PUBLISH_UNCERTAIN")
        verify_ref(client, record)
        context(path, record)
        mark_published(path, record, state, log)
    result = request_validation(path, record["candidate_sha"], client)
    with task_lock(path):
        context(path, record)
        require(result.get("task_snapshot") == record["task_snapshot"] and
                result.get("candidate_sha") == record["candidate_sha"], "VALIDATION_REQUEST_MISMATCH")
        record.update(outcome="PENDING", run_id=result["run_id"])
        atomic_json(record_path(path), record)
    return record


def reconcile(task_file: Path, client=None) -> dict:
    """Verify an existing candidate ref; never repeat a publication or dispatch POST."""
    path = task_file.absolute()
    client = BudgetClient(client or GitHub(), path)
    with task_lock(path):
        record = load_document(record_path(path))
        require(record.get("outcome") == "PUBLISH_UNCERTAIN", "PUBLICATION_RECONCILIATION_REQUIRED")
        if "publication_pair" in record:
            intended = record["publication_pair"]
            require(record.get("publication_snapshot") == {"task_id": path.stem,
                "state": digest(intended["state"]), "log": digest(intended["log"]),
                "checkpoint": record["task_snapshot"]["checkpoint"]}, "PUBLICATION_TRANSITION_REFUSED")
        # A process may have saved the Task pair and died before updating the journal.
        if "publication_snapshot" in record and snapshot(path) == record["publication_snapshot"]:
            record["task_snapshot"] = record["publication_snapshot"]
        state, log, _ = context(path, record)
        require(state["status"] in {"IMPLEMENTING", "VALIDATING"}, "PUBLICATION_RECONCILIATION_REQUIRED")
        verify_commit(client, record)
        verify_ref(client, record)
        if state["status"] == "IMPLEMENTING":
            mark_published(path, record, state, log)
        else:
            record.update(outcome="DISPATCH_UNCERTAIN", task_snapshot=snapshot(path))
            atomic_json(record_path(path), record)
        return record


def abort(task_file: Path) -> dict:
    """End an unresolvable publication safely while retaining every receipt and ref."""
    path = task_file.absolute()
    with task_lock(path):
        record = load_document(record_path(path))
        state, log = pair(path)
        require(record.get("task_id") == path.stem and record.get("repository") == REPOSITORY and
                record.get("outcome") in {"PUBLISH_UNCERTAIN", "DISPATCH_UNCERTAIN", "PR_UNCERTAIN", "FINALIZING"},
                "UNCERTAIN_PIPELINE_REQUIRED")
        require(state["status"] in {"IMPLEMENTING", "VALIDATING"}, "UNCERTAIN_PIPELINE_REQUIRED")
        target = "IMPLEMENTATION_FAILED" if state["status"] == "IMPLEMENTING" else "BUILD_FAILED"
        transition(state, log, target, "Operator aborted unresolved remote request")
        log.update(stop_reason="PIPELINE_ABORTED", finished_at=state["updated_at"])
        record.update(outcome="FINALIZING", final_outcome=target, final_pair={"state": state, "log": log},
            final_snapshot={"task_id": path.stem, "state": digest(state), "log": digest(log),
                "checkpoint": snapshot(path)["checkpoint"]})
        atomic_json(record_path(path), record)
        save_pair(path, state, log)
        record["outcome"] = target
        atomic_json(record_path(path), record)
        return record


def pr_payload(path: Path, record: dict) -> dict:
    approved = record["analysis_record"]
    result = load_document(path.parent / approved["result_file"])
    regular(path.parent / approved["result_file"])
    summary = result["summary"]
    check_content(summary.encode("utf-8"))
    body = ("승인된 Task의 SSTC 변경을 구현하고 GitHub Actions 검증을 통과했습니다.\n\n"
            f"- Task: `{path.stem}`\n- 분석 요약: {summary}\n"
            f"- 분석 기준 커밋: `{record['source_revision']}`\n"
            f"- 검증 대상 커밋: `{record['candidate_sha']}`\n"
            f"- 검증 실행: https://github.com/{REPOSITORY}/actions/runs/{record['run_id']}\n"
            "- JDK 17 / `chmod +x gradlew` / `./gradlew testDebugUnitTest assembleDebug`\n"
            "- Task·후보 SHA·workflow SHA·run ID/attempt·artifact digest·결과 JSON 대조 완료\n\n"
            "사람의 코드 검토가 필요합니다. 화면 변경은 실제 기기의 세로·가로 방향에서 확인해야 합니다.\n"
            "checkpoint와 Slack 승인 기록을 권한 판단의 기준으로 사용했습니다.\n")
    return {"title": f"[SST-AX] {path.stem} 승인된 SSTC 변경", "body": body,
            "head": record["branch"], "base": "main", "draft": True, "maintainer_can_modify": False}


def verify_pr(response: dict, record: dict, payload: dict) -> None:
    number = response.get("number")
    require(positive(number) and response.get("state") == "open" and response.get("draft") is True and
            response.get("merged_at") is None and
            response.get("html_url") == f"https://github.com/{REPOSITORY}/pull/{number}" and
            response.get("head", {}).get("sha") == record["candidate_sha"] and
            response["head"].get("ref") == record["branch"] and
            response["head"].get("repo", {}).get("full_name") == REPOSITORY and
            response.get("base", {}).get("ref") == "main" and
            response["base"].get("repo", {}).get("full_name") == REPOSITORY and
            response.get("title") == payload["title"] and response.get("body") == payload["body"],
            "DRAFT_PR_RECEIPT_MISMATCH")


def check(task_file: Path, client=None) -> dict:
    path = task_file.absolute()
    client = BudgetClient(client or GitHub(), path)
    with task_lock(path):
        record = load_document(record_path(path))
        if record["outcome"] in {"READY_FOR_REVIEW", "BUILD_FAILED", "IMPLEMENTATION_FAILED"}:
            require(snapshot(path) == record["final_snapshot"], "PIPELINE_CONTEXT_CHANGED")
            return record
        if record["outcome"] == "FINALIZING" and snapshot(path) == record["final_snapshot"]:
            record["outcome"] = record["final_outcome"]
            atomic_json(record_path(path), record)
            return record
        context(path, record)
        require(record["outcome"] in {"PENDING", "DISPATCH_UNCERTAIN", "PR_UNCERTAIN", "FINALIZING"},
                "PUBLICATION_UNCERTAIN_REQUIRES_RECONCILIATION")
        validation = load_document(companion(path, "github-validation"))
        require(validation.get("candidate_sha") == record["candidate_sha"] and
                validation.get("task_snapshot") == record["task_snapshot"] and positive(validation.get("run_id")),
                "VALIDATION_REQUEST_MISMATCH")
        require(record.get("run_id", validation["run_id"]) == validation["run_id"], "VALIDATION_RUN_CHANGED")
        record["run_id"] = validation["run_id"]
        if record["outcome"] == "DISPATCH_UNCERTAIN":
            # The Sensor may have saved the acknowledgement before this producer
            # was interrupted. Resume that exact run without another dispatch.
            record["outcome"] = "PENDING"
        atomic_json(record_path(path), record)
    result = check_validation(path, client)
    with task_lock(path):
        # Another checker may have journaled a lost PR response while the Sensor
        # held/released this lock. Always honor the persisted phase, not our old copy.
        record = load_document(record_path(path))
        if record["outcome"] in {"READY_FOR_REVIEW", "BUILD_FAILED"}:
            require(snapshot(path) == record["final_snapshot"], "PIPELINE_CONTEXT_CHANGED")
            return record
        state, log, _ = context(path, record)
        require(state["status"] == "VALIDATING", "VALIDATING_REQUIRED")
        require(result.get("candidate_sha") == record["candidate_sha"] and
                result.get("run_id") == record["run_id"] and
                result.get("task_snapshot") == record["task_snapshot"], "VALIDATION_RESULT_MISMATCH")
        if result["outcome"] == "PENDING":
            return record
        if result["outcome"] == "FAILED":
            transition(state, log, "BUILD_FAILED", "GitHub Actions validation failed")
            log.update(stop_reason="BUILD_FAILED", finished_at=utc_now())
            record.update(outcome="FINALIZING", final_outcome="BUILD_FAILED",
                          final_snapshot={"task_id": path.stem, "state": digest(state), "log": digest(log),
                                          "checkpoint": record["task_snapshot"]["checkpoint"]})
            atomic_json(record_path(path), record)
            save_pair(path, state, log)
            record["outcome"] = "BUILD_FAILED"
            atomic_json(record_path(path), record)
            return record
        require(result["outcome"] == "VALIDATED", "VALIDATION_REQUIRED")
        validate_receipt(result.get("receipt"), result)
        verify_commit(client, record)
        verify_ref(client, record)
        payload = pr_payload(path, record)
        if record["outcome"] in {"PR_UNCERTAIN", "FINALIZING"}:
            pulls = client.api(PREFIX + "/pulls?state=all&base=main&head=" +
                               quote(REPOSITORY.split("/")[0] + ":" + record["branch"], safe="") + "&per_page=100")
            require(isinstance(pulls, list) and len(pulls) == 1, "PR_UNCERTAIN_REQUIRES_RECONCILIATION")
            response = client.api(PREFIX + "/pulls/" + str(pulls[0]["number"]))
        else:
            response = post(path, record, client, PREFIX + "/pulls", payload, "PR_UNCERTAIN")
        verify_pr(response, record, payload)
        # Recheck all local and remote bindings immediately before the producer transition.
        context(path, record)
        verify_ref(client, record)
        state["draft_pr_url"] = response["html_url"]
        transition(state, log, "READY_FOR_REVIEW", "Verified remote validation and Draft PR receipts")
        log.update(finished_at=utc_now(), stop_reason="HUMAN_REVIEW_REQUIRED")
        log["draft_pr"] = {"number": response["number"], "url": response["html_url"],
                           "candidate_sha": record["candidate_sha"], "run_id": record["run_id"], "draft": True}
        log["commands"].append({"name": "sstc-pipeline-check", "command": ["run_sstc_pipeline.py", "check"],
                                "exit_code": 0, "started_at": utc_now(), "finished_at": utc_now(),
                                "artifact_paths": [companion(path, "github-validation").name, record_path(path).name]})
        record.update(outcome="FINALIZING", final_outcome="READY_FOR_REVIEW", draft_pr_url=response["html_url"],
                      final_snapshot={"task_id": path.stem, "state": digest(state), "log": digest(log),
                                      "checkpoint": record["task_snapshot"]["checkpoint"]})
        atomic_json(record_path(path), record)
        save_pair(path, state, log)
        record["outcome"] = "READY_FOR_REVIEW"
        atomic_json(record_path(path), record)
        return record
