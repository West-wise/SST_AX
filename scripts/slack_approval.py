"""One-task approval core; caller authenticates the Socket Mode transport.

Task paths and companions must be Controller-owned, never selected by payload.
This does not authenticate against local writers. Invalid actions are IGNORED;
storage/configuration failures raise. Interrupted saves need CLI --recover.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_storage import (
    atomic_json, companion, load_checkpoint, load_json, save_pair, task_lock,
    validate_pair,
)
from update_task_state import utc_now


def _ids(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Nonempty Slack IDs are required")


def _pair(task_file: Path) -> tuple[dict, dict]:
    if companion(task_file, "pending").exists():
        raise ValueError("Interrupted save: run --recover first")
    state = load_json(task_file)
    log = load_json(companion(task_file, "log"))
    validate_pair(task_file, state, log)
    return state, log


def _snapshot(task_file: Path, state: dict, log: dict) -> str:
    if state["status"] != "WAITING_APPROVAL":
        raise ValueError("Task must be WAITING_APPROVAL")
    checkpoint = load_checkpoint(task_file, state, log)
    canonical = json.dumps(
        {"state": state, "log": log, "checkpoint": checkpoint},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unexpired(request: dict) -> bool:
    expires = datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00"))
    return expires.tzinfo is not None and expires > datetime.now(timezone.utc)


def prepare_request(task_file: Path, team_id: str, channel_id: str, app_id: str) -> dict:
    """Persist a 24-hour request; reuse only a bound, unchanged live request."""
    _ids(team_id, channel_id, app_id)
    with task_lock(task_file):
        state, log = _pair(task_file)
        snapshot = _snapshot(task_file, state, log)
        identity = {"task_id": state["task_id"], "team_id": team_id,
                    "channel_id": channel_id, "app_id": app_id}
        path = companion(task_file, "slack-request")
        if path.exists():
            request = load_json(path)
            if (all(request.get(key) == value for key, value in identity.items())
                    and request.get("snapshot_hash") == snapshot
                    and request.get("message_ts") and _unexpired(request)):
                return request
        request = dict(identity, nonce=secrets.token_urlsafe(32),
                       expires_at=(datetime.now(timezone.utc) + timedelta(hours=24))
                       .isoformat(),
                       snapshot_hash=snapshot, message_ts=None)
        atomic_json(path, request)
        return request


def bind_message(task_file: Path, nonce: str, message_ts: str) -> None:
    """Bind a successful Slack send once, without rebinding another message."""
    _ids(nonce, message_ts)
    with task_lock(task_file):
        state, log = _pair(task_file)
        request = load_json(companion(task_file, "slack-request"))
        if (request["task_id"] != state["task_id"] or request["nonce"] != nonce
                or not _unexpired(request)
                or request["snapshot_hash"] != _snapshot(task_file, state, log)
                or request["message_ts"] not in (None, message_ts)):
            raise ValueError("Approval request is stale or already bound")
        request["message_ts"] = message_ts
        atomic_json(companion(task_file, "slack-request"), request)


def approval_review(task_file: Path, request: dict) -> dict:
    """Read the verified analysis in the exact approval snapshot, never raw logs."""
    from codex_impact import bundle, canonical, digest, regular
    from impact_collection import check_content
    from impact_validation import load_document, validate_impact

    with task_lock(task_file):
        state, log = _pair(task_file)
        if request["snapshot_hash"] != _snapshot(task_file, state, log):
            raise ValueError("Approval review snapshot changed")
        record = log.get("codex_analysis", {})
        if record.get("outcome") != "VALID":
            raise ValueError("Verified analysis is required for approval")
        name = record.get("result_file")
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("Invalid analysis artifact")
        result_file = task_file.parent / name
        regular(result_file)
        result = load_document(result_file)
        check_content(canonical(result))
        if digest(result) != record.get("result_sha256"):
            raise ValueError("Approval result changed")
        # Operators may have retained older, incomplete bundles. Only the exact
        # retained manifest, with every evidence hash recomputed, can be used.
        manifests = list(task_file.parent.glob(task_file.stem + ".inputs*/manifest.json"))
        if record.get("inputs_directory"):
            manifests.insert(0, Path(record["inputs_directory"]) / "manifest.json")
        manifest = None
        for candidate in manifests:
            regular(candidate)
            if digest(load_document(candidate)) == record.get("manifest_sha256"):
                manifest, _ = bundle(state, candidate.parent)
                break
        if manifest is None or validate_impact(state, manifest, result):
            raise ValueError("Approval input or result is invalid")
        return {
            "source_type": state["source_type"],
            "source_reference": state["source_reference"],
            "risk_level": state["risk_level"],
            "approval_reason": state["approval_reason"],
            "input_context": result["input_context"],
            "summary": result["summary"],
            "impacts": result["impacts"],
            "approval_reasons": result["approval_reasons"],
            "unresolved_questions": result["unresolved_questions"],
        }


def apply_decision(task_file: Path, payload: dict, *, team_id: str, channel_id: str,
                   app_id: str, approver_ids: set[str]) -> str:
    """Validate an authenticated block_actions payload and journal its decision."""
    _ids(team_id, channel_id, app_id)
    if not isinstance(approver_ids, set) or not approver_ids:
        raise ValueError("A nonempty Slack approver allowlist is required")
    _ids(*approver_ids)
    with task_lock(task_file):
        state, log = _pair(task_file)
        path = companion(task_file, "slack-request")
        if not path.exists():
            return "IGNORED"
        request = load_json(path)
        try:
            actions = payload["actions"]
            if (payload["type"] != "block_actions"
                    or payload["team"]["id"] != team_id
                    or payload["api_app_id"] != app_id
                    or payload["channel"]["id"] != channel_id
                    or payload["user"]["id"] not in approver_ids
                    or request["team_id"] != team_id
                    or request["channel_id"] != channel_id
                    or request["app_id"] != app_id
                    or request["task_id"] != state["task_id"]
                    or not request["message_ts"]
                    or payload["container"]["message_ts"] != request["message_ts"]
                    or not isinstance(actions, list) or len(actions) != 1
                    or actions[0]["action_id"] not in {"ax_approve", "ax_reject"}
                    or actions[0]["value"] != request["nonce"]
                    or not _unexpired(request)):
                return "IGNORED"
        except (KeyError, TypeError, ValueError, AttributeError):
            return "IGNORED"
        decision = "IMPLEMENTING" if actions[0]["action_id"] == "ax_approve" else "REJECTED"
        if decision == "IMPLEMENTING" and state["risk_level"] == "CRITICAL":
            return "IGNORED"
        audits = log.get("approvals", [])
        # Retries may return a completed decision, never authorize a later cycle.
        if any(entry["nonce"] == request["nonce"] for entry in audits):
            last = audits[-1]
            if (last["nonce"] == request["nonce"] and last["decision"] == decision
                    and last["snapshot_hash"] == request["snapshot_hash"]
                    and state["status"] == decision
                    and log["state_transitions"][-1] == {
                        "from_status": "WAITING_APPROVAL", "to_status": decision,
                        "occurred_at": last["occurred_at"], "reason": "Verified Slack decision",
                    }):
                return decision
            return "IGNORED"
        try:
            if _snapshot(task_file, state, log) != request["snapshot_hash"]:
                return "IGNORED"
        except (OSError, ValueError, KeyError, TypeError):
            return "IGNORED"
        now = utc_now()
        log.setdefault("approvals", []).append({
            "task_id": state["task_id"], "team_id": team_id, "channel_id": channel_id,
            "app_id": app_id, "user_id": payload["user"]["id"],
            "message_ts": request["message_ts"], "nonce": request["nonce"],
            "decision": decision, "occurred_at": now,
            "snapshot_hash": request["snapshot_hash"],
        })
        log["state_transitions"].append({
            "from_status": "WAITING_APPROVAL", "to_status": decision,
            "occurred_at": now, "reason": "Verified Slack decision",
        })
        state["status"] = log["status"] = decision
        state["updated_at"] = now
        if decision == "REJECTED":
            log["finished_at"] = now
            log["stop_reason"] = "Verified Slack decision"
        save_pair(task_file, state, log)
        return decision
