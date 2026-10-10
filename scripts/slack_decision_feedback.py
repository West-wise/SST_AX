"""Durable decision replies; only committed approval audits select recipients."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from codex_impact import regular
from task_storage import atomic_json, companion, load_json, task_lock, validate_pair

TASK_ID = re.compile(r"(?:sstd-sync|sstc-feature)-[0-9]{8}-[0-9]{4}")
STATES = {"PENDING", "SENDING", "SENT", "UNCERTAIN"}


def _require(condition):
    if not condition:
        raise ValueError("SLACK_FEEDBACK_RECORD_REFUSED")


def _identity(task_file, decision, config):
    _require(decision in {"IMPLEMENTING", "REJECTED"})
    _require(not companion(task_file, "pending").exists())
    for path in (task_file, companion(task_file, "log"), companion(task_file, "slack-request")):
        regular(path)
    state = load_json(task_file)
    log = load_json(companion(task_file, "log"))
    validate_pair(task_file, state, log)
    _require(TASK_ID.fullmatch(task_file.stem))
    request = load_json(companion(task_file, "slack-request"))
    audits = log.get("approvals", [])
    _require(isinstance(audits, list) and audits)
    audit = audits[-1]
    for key, value in (("task_id", task_file.stem), ("team_id", config["SLACK_TEAM_ID"]),
                       ("channel_id", config["SLACK_CHANNEL_ID"]), ("app_id", config["SLACK_APP_ID"])):
        _require(audit.get(key) == request.get(key) == value)
    _require(audit.get("decision") == decision and
             audit.get("user_id") in config["SLACK_APPROVER_IDS"].split(","))
    for key in ("nonce", "snapshot_hash", "message_ts"):
        _require(isinstance(audit.get(key), str) and audit[key] == request.get(key))
    _require(re.fullmatch(r"[0-9a-f]{64}", audit["snapshot_hash"]) and
             re.fullmatch(r"[0-9]+\.[0-9]+", audit["message_ts"]))
    _require(any(entry == {"from_status": "WAITING_APPROVAL", "to_status": decision,
                           "occurred_at": audit.get("occurred_at"),
                           "reason": "Verified Slack decision"}
                 for entry in log["state_transitions"]))
    key = hashlib.sha256((audit["nonce"] + ":" + audit["snapshot_hash"]).encode()).hexdigest()
    identity = {"decision": decision, "channel_id": audit["channel_id"],
                "thread_ts": audit["message_ts"], "snapshot_hash": audit["snapshot_hash"]}
    return key, identity


def _ledger(task_file):
    path = companion(task_file, "slack-feedback")
    value = {"schema_version": "1.0", "task_id": task_file.stem, "deliveries": {}}
    if path.exists():
        regular(path)
        value = load_json(path)
    _require(set(value) == {"schema_version", "task_id", "deliveries"} and
             value["schema_version"] == "1.0" and value["task_id"] == task_file.stem and
             isinstance(value["deliveries"], dict))
    for key, entry in value["deliveries"].items():
        _require(isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) and
                 isinstance(entry, dict) and set(entry) == {
                     "decision", "channel_id", "thread_ts", "snapshot_hash", "status", "message_ts"} and
                 entry["decision"] in {"IMPLEMENTING", "REJECTED"} and entry["status"] in STATES and
                 isinstance(entry["channel_id"], str) and re.fullmatch(r"[CG][A-Z0-9]+", entry["channel_id"]) and
                 isinstance(entry["thread_ts"], str) and re.fullmatch(r"[0-9]+\.[0-9]+", entry["thread_ts"]) and
                 isinstance(entry["snapshot_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["snapshot_hash"]) and
                 (entry["message_ts"] is None or isinstance(entry["message_ts"], str) and
                  re.fullmatch(r"[0-9]+\.[0-9]+", entry["message_ts"])) and
                 (entry["status"] != "SENT" or entry["message_ts"] is not None))
    return path, value


def prepare_feedback(task_file: Path, decision: str, config: dict) -> str:
    """Journal before ack. Duplicate envelopes reuse the same delivery key."""
    with task_lock(task_file):
        key, identity = _identity(task_file, decision, config)
        path, ledger = _ledger(task_file)
        entry = ledger["deliveries"].get(key)
        if entry is None:
            ledger["deliveries"][key] = dict(identity, status="PENDING", message_ts=None)
            atomic_json(path, ledger)
        else:
            _require(all(entry.get(name) == value for name, value in identity.items()))
        return key


def recover_feedback(task_file: Path, config: dict) -> str | None:
    """Recover a committed decision; an interrupted send is never sent again."""
    if not companion(task_file, "slack-feedback").exists():
        # Older decisions may already have a manual reply. No blind resend.
        return None
    with task_lock(task_file):
        regular(companion(task_file, "log"))
        audits = load_json(companion(task_file, "log")).get("approvals", [])
        if not audits:
            return None
        decision = audits[-1].get("decision")
    key = prepare_feedback(task_file, decision, config)
    with task_lock(task_file):
        path, ledger = _ledger(task_file)
        if ledger["deliveries"][key]["status"] == "SENDING":
            ledger["deliveries"][key]["status"] = "UNCERTAIN"
            atomic_json(path, ledger)
    return key


def send_feedback(task_file: Path, key: str, config: dict, web) -> str:
    """Send once after ack, outside the Socket Mode callback and task lock."""
    with task_lock(task_file):
        path, ledger = _ledger(task_file)
        entry = ledger["deliveries"].get(key)
        _require(entry is not None)
        verified_key, identity = _identity(task_file, entry["decision"], config)
        _require(verified_key == key and all(entry.get(name) == value for name, value in identity.items()))
        if entry["status"] != "PENDING":
            return entry["status"]
        entry["status"] = "SENDING"
        atomic_json(path, ledger)
        channel, thread, decision = entry["channel_id"], entry["thread_ts"], entry["decision"]
    text = ("SST-AX 승인 접수 완료: " if decision == "IMPLEMENTING" else "SST-AX 거절 접수 완료: ") + task_file.stem
    text += "\n승인 결정이 저장되었습니다." if decision == "IMPLEMENTING" else "\n거절 결정이 저장되었습니다."
    status, message_ts = "UNCERTAIN", None
    try:
        response = web.chat_postMessage(channel=channel, thread_ts=thread, text=text,
                                        unfurl_links=False, unfurl_media=False)
        timestamp = response.get("ts")
        if (response.get("ok") is True and response.get("channel") == channel and
                isinstance(timestamp, str) and re.fullmatch(r"[0-9]+\.[0-9]+", timestamp)):
            status, message_ts = "SENT", timestamp
    except Exception:
        # SDK exceptions and responses may contain credentials or payloads.
        pass
    with task_lock(task_file):
        path, ledger = _ledger(task_file)
        _require(ledger["deliveries"][key]["status"] == "SENDING")
        ledger["deliveries"][key].update(status=status, message_ts=message_ts)
        atomic_json(path, ledger)
    return status
