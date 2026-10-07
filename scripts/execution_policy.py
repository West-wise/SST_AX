"""Trusted analysis decisions and immutable authority, separate from execution progress."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path
import time
import threading
import math

from codex_impact import approved_session, bundle, digest, regular, SESSION_ID
from github_validation import GitHub
from impact_validation import load_document, validate_impact
from task_storage import atomic_json, checkpoint_path, companion, save_pair, task_lock, validate_pair
from update_task_state import utc_now

POLICY = Path(__file__).resolve().parents[1] / "policies/agent-execution-policy.md"
MAX_ACTIVE_SECONDS, MAX_ATTEMPTS, MAX_WRITE_STEPS = 1800, 3, 20
RISK = {name: i for i, name in enumerate(("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"))}
IDENTITY = ("task_id", "source_type", "source_reference", "risk_level", "approval_reason")


def pair(path: Path) -> tuple[dict, dict]:
    if companion(path, "pending").exists():
        raise ValueError("RECOVER_PAIR_FIRST")
    state, log = load_document(path), load_document(companion(path, "log"))
    validate_pair(path, state, log)
    return state, log


def transition(state: dict, log: dict, target: str, reason: str) -> None:
    old = state["status"]
    state.update(status=target, updated_at=utc_now())
    log["status"] = target
    log["state_transitions"].append({"from_status": old, "to_status": target,
                                     "occurred_at": state["updated_at"], "reason": reason})
    if target in {"COMPLETED", "ANALYSIS_FAILED", "IMPLEMENTATION_FAILED", "PROTOCOL_APPROVAL_REQUIRED"}:
        log.update(finished_at=utc_now(), stop_reason=reason)


def validated_analysis(task_file: Path, input_directory: Path | None = None) -> dict:
    """Recompute both input bodies and retained result; JSON validity is never authority."""
    path = task_file.absolute()
    state, log = pair(path)
    record = log.get("codex_analysis", {})
    if record.get("outcome") != "VALID":
        raise ValueError("VALID_ANALYSIS_REQUIRED")
    result_file = path.parent / record["result_file"]
    if result_file.parent != path.parent or result_file.name != record["result_file"]:
        raise ValueError("RESULT_PATH")
    regular(result_file)
    result = load_document(result_file)
    if digest(result) != record.get("result_sha256"):
        raise ValueError("RESULT_CHANGED")
    directories = ([input_directory.absolute()] if input_directory else
                   [Path(record["inputs_directory"])] if record.get("inputs_directory") else
                   sorted(path.parent.glob(path.stem + ".inputs*")))
    matches = []
    for directory in directories:
        try:
            manifest, bodies = bundle(state, directory)
            if digest(manifest) == record.get("manifest_sha256"):
                matches.append((directory, manifest, bodies))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(matches) != 1:
        raise ValueError("EXACT_INPUT_BUNDLE_REQUIRED")
    directory, manifest, bodies = matches[0]
    if validate_impact(state, manifest, result):
        raise ValueError("INVALID_ANALYSIS")
    session = record.get("session_id")
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        raise ValueError("VALID_SESSION_REQUIRED")
    return {"state": state, "log": log, "record": record, "manifest": manifest,
            "bodies": bodies, "result": result, "session_id": session,
            "input_directory": directory}


def reasons(state: dict, result: dict) -> list[str]:
    flags = set(result["approval_reasons"])
    if state.get("approval_reason"):
        flags.add(state["approval_reason"])
    if state["risk_level"] in {"HIGH", "CRITICAL"}:
        flags.add(state["risk_level"] + "_RISK")
    return sorted(flags)


def authority_path(path: Path) -> Path:
    return companion(path, "execution-authority")


def make_authority(path: Path, analysis: dict, authorization: str, original: dict | None = None,
                   *, persist=True) -> dict:
    state, log = analysis["state"], analysis["log"]
    proof = {"schema_version": "1.0", "task_id": path.stem,
             "authorization": authorization, "session_id": analysis["session_id"],
             "manifest_sha256": digest(analysis["manifest"]), "result_sha256": digest(analysis["result"]),
             "source_revision": analysis["manifest"]["input_context"]["sstc_revision"],
             "policy_sha256": digest(POLICY.read_text(encoding="utf-8")),
             "analysis_record": copy.deepcopy(analysis["record"]),
             "identity": {key: state.get(key) for key in IDENTITY},
             "original_pair": original or {"state": copy.deepcopy(state), "log": copy.deepcopy(log)}}
    if authorization == "SLACK":
        proof["approval_checkpoint"] = load_document(checkpoint_path(path))
        proof["approval_request"] = load_document(companion(path, "slack-request"))
    if persist:
        atomic_json(authority_path(path), proof)
    return proof


def decide(task_file: Path, input_directory: Path) -> dict:
    path = task_file.absolute()
    with task_lock(path):
        analysis = validated_analysis(path, input_directory)
        state, log, result = analysis["state"], analysis["log"], analysis["result"]
        journal = companion(path, "execution-decision")
        if journal.exists():
            pending = load_document(journal)
            before, after, proof = pending["before"], pending["after"], pending["proof"]
            validate_pair(path, before["state"], before["log"])
            validate_pair(path, after["state"], after["log"])
            decision = {"decision": "IMPLEMENTING", "reason": "VERIFIED_POLICY_AUTOMATIC",
                        "result_sha256": digest(result), "manifest_sha256": digest(analysis["manifest"])}
            expected = copy.deepcopy(before)
            expected["state"].update(status="IMPLEMENTING", updated_at=after["state"]["updated_at"],
                                      risk_level=max((before["state"]["risk_level"], result["risk_level"]), key=RISK.get))
            expected["log"].update(status="IMPLEMENTING", policy_decision=decision)
            expected["log"]["state_transitions"].append({"from_status": "ANALYZING", "to_status": "IMPLEMENTING",
                "occurred_at": after["state"]["updated_at"], "reason": decision["reason"]})
            expected_proof = make_authority(path, {**analysis, "state": after["state"], "log": after["log"]},
                                             "POLICY", persist=False)
            if (before["state"]["status"] != "ANALYZING" or after != expected or
                    {"state": state, "log": log} not in (before, after) or
                    proof["original_pair"] != after or proof["authorization"] != "POLICY" or
                    proof["analysis_record"] != analysis["record"] or reasons(after["state"], result) or
                    after["state"]["risk_level"] in {"HIGH", "CRITICAL"} or result["change_required"] != "REQUIRED" or
                    proof["manifest_sha256"] != digest(analysis["manifest"]) or proof["result_sha256"] != digest(result) or
                    proof["policy_sha256"] != digest(POLICY.read_text(encoding="utf-8")) or proof != expected_proof):
                raise ValueError("POLICY_DECISION_JOURNAL_CHANGED")
            if authority_path(path).exists() and load_document(authority_path(path)) != proof:
                raise ValueError("EXECUTION_AUTHORITY_CHANGED")
            atomic_json(authority_path(path), proof)
            save_pair(path, after["state"], after["log"])
            journal.unlink()
            execution_context(path, input_directory)
            return {"decision": "IMPLEMENTING", "status": "IMPLEMENTING", "reason": decision["reason"],
                    "session_id": analysis["session_id"]}
        if state["status"] != "ANALYZING":
            raise ValueError("ANALYZING_REQUIRED")
        if authority_path(path).exists():
            raise ValueError("EXECUTION_AUTHORITY_ALREADY_ISSUED")
        before = {"state": copy.deepcopy(state), "log": copy.deepcopy(log)}
        state["risk_level"] = max((state["risk_level"], result["risk_level"]), key=RISK.get)
        flags = reasons(state, result)
        if result["change_required"] == "NOT_REQUIRED":
            target, reason = "COMPLETED", "VERIFIED_NO_CLIENT_IMPACT"
        elif result["change_required"] == "UNDETERMINED":
            target, reason = "ANALYSIS_FAILED", "UNDETERMINED_REQUIRES_REVIEW"
        elif state["risk_level"] == "CRITICAL":
            target, reason = "ANALYSIS_FAILED", "CRITICAL_PROPOSAL_ONLY"
        elif state["source_type"] == "SSTC_FEATURE" and any(
                result["impacts"][key]["status"] == "PRESENT"
                for key in ("protocol_contract", "sstd_change_required")):
            target, reason = "PROTOCOL_APPROVAL_REQUIRED", "SSTD_CONTRACT_DECISION_REQUIRED"
        elif any(result["impacts"][key]["status"] == "PRESENT" for key in ("dependency", "android_permission")):
            target, reason = "ANALYSIS_FAILED", "UNSUPPORTED_VALIDATION_SCOPE"
        elif flags:
            target, reason = "WAITING_APPROVAL", ",".join(flags)
            state.update(approval_reason=reason, checkpoint_path=f"state/checkpoints/{path.stem}.json")
        else:
            target, reason = "IMPLEMENTING", "VERIFIED_POLICY_AUTOMATIC"
        transition(state, log, target, reason)
        log["policy_decision"] = {"decision": target, "reason": reason,
                                  "result_sha256": digest(result), "manifest_sha256": digest(analysis["manifest"])}
        if target == "WAITING_APPROVAL":
            atomic_json(checkpoint_path(path), {"state": state, "log": log, "resume_status": "ANALYZING"})
        if target == "IMPLEMENTING":
            prepared = make_authority(path, analysis, "POLICY", persist=False)
            atomic_json(journal, {"before": before, "after": {"state": state, "log": log}, "proof": prepared})
            make_authority(path, analysis, "POLICY")
        save_pair(path, state, log)
        if target == "IMPLEMENTING":
            journal.unlink()
        return {"decision": target, "status": target, "reason": reason, "session_id": analysis["session_id"]}


def execution_context(task_file: Path, input_directory: Path,
                      allowed_statuses=("IMPLEMENTING", "VALIDATING")) -> dict:
    """Caller holds the Task lock. Only this producer creates execution authority."""
    path = task_file.absolute()
    analysis = validated_analysis(path, input_directory)
    state, log, result = analysis["state"], analysis["log"], analysis["result"]
    if state["status"] not in allowed_statuses:
        raise ValueError("EXECUTION_STAGE_REFUSED")
    if result["change_required"] != "REQUIRED" or result["risk_level"] == "CRITICAL":
        raise ValueError("EXECUTION_NOT_REQUIRED")
    if any(result["impacts"][key]["status"] == "PRESENT" for key in ("dependency", "android_permission")):
        raise ValueError("UNSUPPORTED_VALIDATION_SCOPE")
    if state["source_type"] == "SSTC_FEATURE" and any(result["impacts"][key]["status"] == "PRESENT"
            for key in ("protocol_contract", "sstd_change_required")):
        raise ValueError("PROTOCOL_APPROVAL_REQUIRED")
    output = authority_path(path)
    if not output.exists():
        # Strict legacy migration: reconstruct only the known worker receipt; never
        # discard arbitrary log mutations or grant permission from a session ID.
        original = {"state": copy.deepcopy(state), "log": copy.deepcopy(log)}
        if "codex_worker" in original["log"]:
            worker = original["log"].pop("codex_worker")
            command = original["log"]["commands"].pop()
            if (command.get("name") != "codex-sstc-worker" or worker.get("session_id") != analysis["session_id"]
                    or command.get("started_at") != worker.get("started_at")):
                raise ValueError("LEGACY_WORKER_RECEIPT_REFUSED")
        approved_session(path, original["state"], original["log"], analysis["manifest"])
        proof = make_authority(path, analysis, "SLACK", original)
    else:
        proof = load_document(output)
    if (proof["task_id"] != path.stem or proof["session_id"] != analysis["session_id"] or
            proof["manifest_sha256"] != digest(analysis["manifest"]) or
            proof["result_sha256"] != digest(result) or proof["analysis_record"] != analysis["record"] or
            proof["policy_sha256"] != digest(POLICY.read_text(encoding="utf-8")) or
            proof.get("schema_version") != "1.0" or
            proof.get("source_revision") != analysis["manifest"]["input_context"]["sstc_revision"] or
            proof["identity"] != {key: state.get(key) for key in IDENTITY}):
        raise ValueError("EXECUTION_AUTHORITY_CHANGED")
    original = proof["original_pair"]
    validate_pair(path, original["state"], original["log"])
    if ({key: original["state"].get(key) for key in IDENTITY} != proof["identity"] or
            original["state"]["status"] != "IMPLEMENTING" or
            original["log"].get("codex_analysis") != proof["analysis_record"]):
        raise ValueError("ORIGINAL_EXECUTION_CONTEXT_CHANGED")
    if proof["authorization"] == "SLACK":
        if log.get("approvals") != proof["original_pair"]["log"].get("approvals"):
            raise ValueError("APPROVAL_HISTORY_CHANGED")
        approved_session(path, proof["original_pair"]["state"], proof["original_pair"]["log"], analysis["manifest"],
                         proof["approval_checkpoint"], proof["approval_request"])
    elif proof["authorization"] == "POLICY":
        decision = {"decision": "IMPLEMENTING", "reason": "VERIFIED_POLICY_AUTOMATIC",
                    "result_sha256": digest(result), "manifest_sha256": digest(analysis["manifest"])}
        expected_transition = {"from_status": "ANALYZING", "to_status": "IMPLEMENTING",
                               "occurred_at": original["state"]["updated_at"], "reason": decision["reason"]}
        if (reasons(state, result) or state["risk_level"] in {"HIGH", "CRITICAL"} or
                original["log"].get("policy_decision") != decision or
                original["log"]["state_transitions"][-1] != expected_transition):
            raise ValueError("AUTOMATIC_POLICY_REFUSED")
    else:
        raise ValueError("UNKNOWN_EXECUTION_AUTHORITY")
    return {**analysis, "proof": proof}


class Budget:
    """Persist actual active seconds; human waiting/poll intervals never consume time."""
    def __init__(self, path: Path, label: str, *, attempt=False, legacy_budget=None):
        self.path, self.output = path, companion(path, "execution")
        if self.output.exists():
            self.value = load_document(self.output)
        else:
            state, log = pair(path)
            seconds = 0.0
            for entry in log.get("commands", []):
                if (entry.get("name") in {"codex-impact", "codex-sstc-worker"} and
                        entry.get("started_at") and entry.get("finished_at")):
                    start = datetime.fromisoformat(entry["started_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(entry["finished_at"].replace("Z", "+00:00"))
                    seconds += max(0.0, (end - start).total_seconds())
            self.value = {"active_seconds": seconds, "attempts": int("codex_worker" in log),
                          "analysis_retries": max(0, state["attempt"] - 1),
                          "write_steps": 0, "stage": label}
            if "codex_worker" in log:
                if (legacy_budget is None or len(legacy_budget) != 2 or
                        type(legacy_budget[0]) not in {int, float} or not math.isfinite(legacy_budget[0]) or
                        legacy_budget[0] < seconds or type(legacy_budget[1]) is not int or legacy_budget[1] < 1):
                    raise ValueError("LEGACY_BUDGET_OBSERVATION_REQUIRED")
                self.value.update(active_seconds=legacy_budget[0], write_steps=legacy_budget[1],
                                  legacy_observation={"active_seconds": legacy_budget[0],
                                                      "write_steps": legacy_budget[1], "recorded_at": utc_now()})
        if (type(self.value.get("active_seconds")) not in {int, float} or
                not math.isfinite(self.value["active_seconds"]) or self.value["active_seconds"] < 0 or
                any(type(self.value.get(key)) is not int or self.value[key] < 0
                    for key in ("attempts", "write_steps")) or
                type(self.value.get("analysis_retries", 0)) is not int or self.value.get("analysis_retries", 0) < 0):
            raise ValueError("EXECUTION_CHECKPOINT_INVALID")
        binding = digest(load_document(authority_path(path))) if authority_path(path).exists() else None
        previous_binding = self.value.get("authority_sha256", binding)
        # Analysis starts without write authority. Seal its counters exactly once
        # to the immutable authority issued for that same retained analysis.
        seal_analysis = (previous_binding is None and binding is not None and
                         self.value.get("stage") == "ANALYZING" and not self.value.get("active") and
                         self.value["attempts"] == 0 and
                         self.value.get("analysis_sha256") ==
                         digest(load_document(authority_path(path))["analysis_record"]))
        if (self.value.get("task_id", path.stem) != path.stem or
                previous_binding != binding and not seal_analysis):
            raise ValueError("EXECUTION_CHECKPOINT_MISMATCH")
        self.value.update(task_id=path.stem, authority_sha256=binding)
        if attempt:
            if (self.value["attempts"] >= MAX_ATTEMPTS or
                    self.value["attempts"] + self.value.get("analysis_retries", 0) > MAX_ATTEMPTS):
                raise ValueError("EXECUTION_ATTEMPT_LIMIT")
            self.value["attempts"] += 1
        if self.value["active_seconds"] >= MAX_ACTIVE_SECONDS or self.value["write_steps"] > MAX_WRITE_STEPS:
            raise ValueError("EXECUTION_BUDGET_EXHAUSTED")
        self.value.update(stage=label, active=True)
        self.last = time.monotonic()
        self.lock = threading.Lock()
        atomic_json(self.output, self.value)

    @property
    def remaining(self) -> float:
        return max(0.01, MAX_ACTIVE_SECONDS - self.value["active_seconds"])

    def tick(self, writes=0) -> None:
        with self.lock:
            now = time.monotonic()
            self.value["active_seconds"] += now - self.last
            self.last = now
            self.value["write_steps"] += writes
            atomic_json(self.output, self.value)
            if self.value["active_seconds"] >= MAX_ACTIVE_SECONDS or self.value["write_steps"] > MAX_WRITE_STEPS:
                raise ValueError("EXECUTION_BUDGET_EXHAUSTED")

    def finish(self, outcome: str) -> None:
        try:
            self.tick()
        except ValueError:
            self.value["stop_reason"] = "EXECUTION_BUDGET_EXHAUSTED"
        finally:
            self.value.update(active=False, outcome=outcome)
            atomic_json(self.output, self.value)


def defer(path: Path, state: dict, log: dict) -> None:
    transition(state, log, "DEFERRED_RATE_LIMIT", "CODEX_USAGE_UNAVAILABLE")
    state.update(deferred_until=None, checkpoint_path=f"state/checkpoints/{path.stem}.json")
    atomic_json(checkpoint_path(path), {"state": state, "log": log, "resume_status": "IMPLEMENTING"})
    save_pair(path, state, log)


def stop_budget(path: Path, reason: str) -> None:
    """Caller holds the Task lock; exhausted work must not be retried by polling."""
    state, log = pair(path)
    target = {"ANALYZING": "ANALYSIS_FAILED", "IMPLEMENTING": "IMPLEMENTATION_FAILED",
              "VALIDATING": "SECURITY_REVIEW_FAILED"}.get(state["status"])
    if target:
        transition(state, log, target, reason)
        log.update(stop_reason=reason, finished_at=utc_now())
        state["checkpoint_path"] = f"state/checkpoints/{path.stem}.json"
        save_pair(path, state, log)
        atomic_json(checkpoint_path(path), {"state": state, "log": log,
                    "resume_status": "ANALYZING" if target == "ANALYSIS_FAILED" else "IMPLEMENTING"})


class BudgetClient:
    """Count Controller network work and mutation steps without counting polling waits."""
    def __init__(self, client, path: Path):
        self.client, self.path = client, path

    def api(self, endpoint, payload=None, *, binary=False):
        try:
            budget = Budget(self.path, "GITHUB")
        except ValueError as error:
            if str(error) == "EXECUTION_BUDGET_EXHAUSTED":
                stop_budget(self.path, str(error))
            raise
        outcome = "API_FAILED"
        try:
            budget.tick(writes=int(payload is not None))
            options = {"binary": binary}
            if isinstance(self.client, GitHub):
                options["timeout"] = min(30, budget.remaining)
            result = self.client.api(endpoint, payload, **options)
            outcome = "API_COMPLETED"
            return result
        finally:
            budget.finish(outcome)
            if budget.value["active_seconds"] >= MAX_ACTIVE_SECONDS or budget.value["write_steps"] > MAX_WRITE_STEPS:
                stop_budget(self.path, "EXECUTION_BUDGET_EXHAUSTED")
                raise ValueError("EXECUTION_BUDGET_EXHAUSTED")
