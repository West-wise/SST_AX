"""Local task transactions and checkpoints; single task writer enforced by OS lock."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from validate_task_state import DEFAULT_SCHEMA_PATH, load_json, validate_task_state


def companion(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.stem}.{suffix}.json")


def atomic_json(path: Path, value: dict) -> None:
    """Replace one file after flushing its complete contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".task-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def task_lock(path: Path):
    """Release automatically on process exit, including an interrupted writer."""
    with path.with_suffix(".lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def validate_pair(path: Path, state: dict, log: dict) -> None:
    errors = validate_task_state(state, load_json(DEFAULT_SCHEMA_PATH))
    if errors:
        raise ValueError("; ".join(errors))
    if not isinstance(log, dict) or state["task_id"] != path.stem:
        raise ValueError("Task identity does not match file")
    for key in ("task_id", "source_type", "source_reference", "status"):
        if log.get(key) != state[key]:
            raise ValueError(f"Task/log mismatch: {key}")
    transitions = log.get("state_transitions")
    if not isinstance(transitions, list) or not transitions:
        raise ValueError("Missing transition history")
    if transitions[-1].get("to_status") != state["status"]:
        raise ValueError("Transition history does not match task")


def save_pair(path: Path, state: dict, log: dict) -> None:
    """Journal the intended pair before replacing either file. Caller holds lock."""
    validate_pair(path, state, log)
    pending = companion(path, "pending")
    atomic_json(pending, {"state": state, "log": log})
    atomic_json(path, state)
    atomic_json(companion(path, "log"), log)
    pending.unlink()


def recover_pair(path: Path) -> None:
    """Roll forward an interrupted save without appending another transition."""
    pending = companion(path, "pending")
    if not pending.exists():
        validate_pair(path, load_json(path), load_json(companion(path, "log")))
        return
    bundle = load_json(pending)
    validate_pair(path, bundle["state"], bundle["log"])
    atomic_json(path, bundle["state"])
    atomic_json(companion(path, "log"), bundle["log"])
    pending.unlink()


def checkpoint_path(path: Path) -> Path:
    return path.parent.parent / "checkpoints" / f"{path.stem}.json"


def load_checkpoint(path: Path, state: dict, log: dict) -> dict:
    checkpoint = load_json(checkpoint_path(path))
    if checkpoint["state"] != state or checkpoint["log"] != log:
        raise ValueError("Checkpoint is stale or belongs to another task")
    if checkpoint.get("resume_status") not in {"ANALYZING", "IMPLEMENTING", "WAITING_APPROVAL"}:
        raise ValueError("Invalid checkpoint resume status")
    return checkpoint
