"""Trusted, versioned execution profiles; repository inputs cannot set limits."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = "balanced-v2"

@dataclass(frozen=True)
class Limits:
    version: str
    active_seconds: int = 1800
    attempts: int = 3
    write_steps: int = 20
    read_steps: int = 0
    publication_reserve: int = 0
    retries: int = 2

PROFILES = {"legacy-v1": Limits("legacy-v1"),
            "balanced-v2": Limits("balanced-v2", read_steps=512, publication_reserve=5)}

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def limits_for_log(log):
    version = log.get("execution_profile", "legacy-v1")
    if not isinstance(version, str) or version not in PROFILES:
        raise ValueError("EXECUTION_PROFILE_REFUSED")
    return PROFILES[version]

def limits_for_task(path):
    from task_storage import companion
    from impact_validation import load_document
    return limits_for_log(load_document(companion(path, "log")))

def limits_hash(limits):
    return digest(asdict(limits))

def policy_hash(path, expected=None):
    limits = limits_for_task(path)
    document = ROOT / "policies/versions" / (limits.version + ".md")
    from codex_impact import regular
    regular(document)
    value = digest(document.read_text(encoding="utf-8"))
    if expected is not None and expected != value:
        raise ValueError("EXECUTION_AUTHORITY_CHANGED")
    return value

def worker_context(analysis, budget, *, max_files=8, max_bytes=65536):
    """Supply only already hash-verified evidence. No shell or source dereference."""
    if budget.limits.version == "legacy-v1":
        return None
    budget.tick(reads=1)
    selected, omitted, size = [], [], 0
    referenced = {item["evidence_id"] for item in analysis["result"]["evidence"]}
    for item in analysis["manifest"]["evidence"]:
        key = item["evidence_id"]
        if key not in referenced:
            continue
        body = analysis["bodies"].get(key)
        if body is None or len(selected) >= max_files or size + len(body.encode()) > max_bytes:
            omitted.append(key)
            continue
        size += len(body.encode())
        selected.append({"evidence_id": key, "source": item["source"], "path": item["path"],
                         "content_sha256": item["content_sha256"], "text": body})
    return {"evidence": selected, "omitted_evidence_ids": omitted, "bytes": size}
