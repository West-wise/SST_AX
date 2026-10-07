"""Task-bound GitHub Actions Sensors; never grant implementation or PR authority."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

from impact_validation import decode_document, load_document
from task_storage import atomic_json, checkpoint_path, companion, task_lock, validate_pair

REPOSITORY = "West-wise/Server_State_Telemetry_Client"
WORKFLOW = ".github/workflows/sstc-validation.yml"
SHA = re.compile(r"[0-9a-f]{40}")
COMMANDS = ["chmod +x gradlew", "./gradlew testDebugUnitTest assembleDebug"]
MAX_ARCHIVE = 65_536


class ValidationError(ValueError):
    """Only fixed error codes cross the CLI boundary."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValidationError(code)


def positive(value) -> bool:
    return type(value) is int and value > 0


def digest(document) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class GitHub:
    """Controller-only gh credentials; stdout/stderr and tokens are never logged."""

    def api(self, endpoint: str, payload: dict | None = None, *, binary=False, timeout=30):
        command = ["gh", "api", "--hostname", "github.com", "-H",
                   "X-GitHub-Api-Version:2026-03-10", "--method",
                   "POST" if payload is not None else "GET", endpoint]
        if payload is not None:
            command.extend(["--input", "-"])
        env = dict(os.environ)
        env.pop("GH_DEBUG", None)
        env["GH_PROMPT_DISABLED"] = "1"
        result = subprocess.run(command, input=json.dumps(payload).encode() if payload is not None else None,
                                capture_output=True, env=env, timeout=timeout, check=False)
        require(result.returncode == 0, "GITHUB_API_FAILED")
        require(len(result.stdout) <= (MAX_ARCHIVE if binary else 1_048_576), "GITHUB_RESPONSE_LIMIT")
        return result.stdout if binary else decode_document(result.stdout)


def snapshot(path: Path) -> dict:
    require(not companion(path, "pending").exists(), "RECOVER_PAIR_FIRST")
    state, log = load_document(path), load_document(companion(path, "log"))
    validate_pair(path, state, log)
    checkpoint = checkpoint_path(path)
    require(not state.get("checkpoint_path") or checkpoint.is_file(), "CHECKPOINT_REQUIRED")
    return {"task_id": state["task_id"], "state": digest(state), "log": digest(log),
            "checkpoint": digest(load_document(checkpoint)) if checkpoint.exists() else None}


def record_path(path: Path) -> Path:
    return companion(path, "github-validation")


def request_validation(task_file: Path, candidate_sha: str, client=None) -> dict:
    require(isinstance(candidate_sha, str) and SHA.fullmatch(candidate_sha) is not None, "CANDIDATE_SHA_REQUIRED")
    path = task_file.absolute()
    client = client or GitHub()
    prefix = f"repos/{REPOSITORY}"
    with task_lock(path):
        frozen = snapshot(path)
        output = record_path(path)
        require(not output.exists(), "REQUEST_EXISTS_NO_AUTOMATIC_REDISPATCH")
        source = client.api(prefix + "/commits/main")
        workflow = client.api(prefix + "/actions/workflows/sstc-validation.yml")
        candidate = client.api(prefix + "/commits/" + candidate_sha)
        require(isinstance(source.get("sha"), str) and SHA.fullmatch(source["sha"]) is not None, "WORKFLOW_SHA_REQUIRED")
        require(workflow.get("path") == WORKFLOW and workflow.get("state") == "active" and positive(workflow.get("id")), "WORKFLOW_REFUSED")
        require(candidate.get("sha") == candidate_sha, "CANDIDATE_COMMIT_MISMATCH")
        record = {"schema_version": "1.0", "repository": REPOSITORY, "workflow_id": workflow["id"],
                  "workflow_sha": source["sha"], "task_id": frozen["task_id"],
                  "candidate_sha": candidate_sha, "task_snapshot": frozen,
                  "run_id": None, "run_attempt": 1, "outcome": "DISPATCH_UNCERTAIN", "authorization": "NONE"}
        # Persist before POST. A timeout may already have started a run; never repeat it.
        atomic_json(output, record)
        dispatched = client.api(prefix + "/actions/workflows/sstc-validation.yml/dispatches",
                                {"ref": "main", "inputs": {"task_id": frozen["task_id"], "candidate_sha": candidate_sha}})
        require(positive(dispatched.get("workflow_run_id")), "DISPATCH_RUN_ID_REQUIRED")
        record.update(run_id=dispatched["workflow_run_id"], outcome="PENDING")
        atomic_json(output, record)
        return record


def validate_receipt(receipt, record: dict) -> None:
    expected = {"schema_version": "1.0", "task_id": record["task_id"], "repository": REPOSITORY,
                "candidate_sha": record["candidate_sha"],
                "workflow_ref": f"{REPOSITORY}/{WORKFLOW}@refs/heads/main",
                "workflow_sha": record["workflow_sha"], "run_id": record["run_id"],
                "run_attempt": record["run_attempt"], "build_result": "success",
                "validation": {"jdk": "17", "commands": COMMANDS}, "authorization": "NONE"}
    require(isinstance(receipt, dict) and positive(receipt.get("run_id")) and
            positive(receipt.get("run_attempt")) and receipt == expected, "RECEIPT_MISMATCH")


def read_archive(raw: bytes):
    require(len(raw) <= MAX_ARCHIVE, "ARTIFACT_SIZE_LIMIT")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == ["validation.json"], "ARTIFACT_CONTENT_REFUSED")
        entry = archive.getinfo("validation.json")
        require(entry.file_size <= 16_384 and not entry.is_dir(), "RECEIPT_SIZE_LIMIT")
        return decode_document(archive.read(entry))


def check_validation(task_file: Path, client=None) -> dict:
    path = task_file.absolute()
    client = client or GitHub()
    prefix = f"repos/{REPOSITORY}"
    with task_lock(path):
        record = load_document(record_path(path))
        require(record.get("repository") == REPOSITORY and record.get("authorization") == "NONE" and
                record.get("schema_version") == "1.0", "REQUEST_REFUSED")
        require(snapshot(path) == record.get("task_snapshot") and record.get("task_id") == path.stem, "TASK_SNAPSHOT_CHANGED")
        require(positive(record.get("run_id")) and record.get("run_attempt") == 1 and
                type(record["run_attempt"]) is int and positive(record.get("workflow_id")), "REQUEST_RUN_REQUIRED")
        require(all(isinstance(record.get(k), str) and SHA.fullmatch(record[k]) is not None
                    for k in ("candidate_sha", "workflow_sha")), "REQUEST_SHA_REQUIRED")
        run_id = record["run_id"]
        run = client.api(f"{prefix}/actions/runs/{run_id}")
        require(run.get("id") == run_id and run.get("workflow_id") == record["workflow_id"] and
                run.get("repository", {}).get("full_name") == REPOSITORY and
                run.get("event") == "workflow_dispatch" and run.get("head_branch") == "main" and
                run.get("head_sha") == record["workflow_sha"] and run.get("path") == WORKFLOW and
                type(run.get("run_attempt")) is int and run["run_attempt"] == record["run_attempt"], "RUN_IDENTITY_MISMATCH")
        if run.get("status") != "completed":
            require(run.get("status") in {"queued", "in_progress", "waiting", "requested", "pending"}, "RUN_STATUS_REFUSED")
            record["outcome"] = "PENDING"
        elif run.get("conclusion") != "success":
            record["outcome"] = "FAILED"
        else:
            jobs = client.api(f"{prefix}/actions/runs/{run_id}/attempts/1/jobs?per_page=100")
            entries = jobs.get("jobs", [])
            require(jobs.get("total_count") == 3 and len(entries) == 3 and
                    {job.get("name") for job in entries} == {"prepare", "build", "receipt"} and
                    all(job.get("status") == "completed" and job.get("conclusion") == "success" and
                        job.get("run_id") == run_id for job in entries), "JOB_VALIDATION_FAILED")
            name = f"sstc-validation-{run_id}-1"
            artifacts = client.api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100&name={name}")
            entries = artifacts.get("artifacts", [])
            require(artifacts.get("total_count") == 1 and len(entries) == 1, "ARTIFACT_AMBIGUOUS_OR_MISSING")
            item = entries[0]
            require(item.get("name") == name and item.get("expired") is False and positive(item.get("id")) and
                    positive(item.get("size_in_bytes")) and item["size_in_bytes"] <= MAX_ARCHIVE and
                    item.get("workflow_run", {}).get("id") == run_id and
                    item["workflow_run"].get("head_sha") == record["workflow_sha"], "ARTIFACT_IDENTITY_MISMATCH")
            raw = client.api(f"{prefix}/actions/artifacts/{item['id']}/zip", binary=True)
            require(item.get("digest") == "sha256:" + hashlib.sha256(raw).hexdigest(), "ARTIFACT_DIGEST_MISMATCH")
            receipt = read_archive(raw)
            validate_receipt(receipt, record)
            record.update(outcome="VALIDATED", artifact_id=item["id"], receipt=receipt)
        atomic_json(record_path(path), record)
        return record
