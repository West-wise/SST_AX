"""Local task transactions and checkpoints; single task writer enforced by OS lock."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from validate_task_state import DEFAULT_SCHEMA_PATH, load_json, validate_task_state


def companion(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.stem}.{suffix}.json")


def process_stopped(pid: int) -> bool:
    """Confirm a recorded worker PID exited; an uncertain check never permits retry."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("PROCESS_CONFIRMATION_REQUIRED")
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID absent
                return True
            raise ValueError("PROCESS_CONFIRMATION_REQUIRED")
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ValueError("PROCESS_CONFIRMATION_REQUIRED")
            return code.value != 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        raise ValueError("PROCESS_CONFIRMATION_REQUIRED") from None
    return False


def atomic_json(path: Path, value: dict) -> None:
    """Replace one file after flushing its complete contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".task-")
    temporary = Path(name)
    try:
        # Explicitly shared Controller/Gateway state directories have setgid.
        # Publish the group-readable/writable mode before atomic replacement;
        # private directories retain mkstemp's 0600 default. No secret store uses
        # this helper, and an other-writable directory is never a shared store.
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            directory = path.parent.stat()
            if directory.st_mode & stat.S_ISGID:
                if directory.st_mode & 0o007:
                    raise ValueError("SHARED_STATE_DIRECTORY_MUST_BE_PRIVATE")
                os.fchmod(stream.fileno(), 0o660 if directory.st_mode & 0o020 else 0o640)
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


def _write_bundle(path: Path, bundle: dict) -> None:
    state, log = bundle["state"], bundle["log"]
    validate_pair(path, state, log)
    resume = bundle.get("checkpoint_status")
    if resume is not None and resume not in {"ANALYZING", "IMPLEMENTING", "WAITING_APPROVAL"}:
        raise ValueError("Invalid checkpoint resume status")
    if type(bundle.get("codex_checkpoint", False)) is not bool:
        raise ValueError("Invalid Codex checkpoint flag")
    atomic_json(path, state)
    atomic_json(companion(path, "log"), log)
    if resume is not None:
        atomic_json(checkpoint_path(path), {"state": state, "log": log, "resume_status": resume})
    if bundle.get("codex_checkpoint"):
        atomic_json(companion(path, "codex-checkpoint"), {
            "state": state, "log": log, "resume_status": state["status"]})


def save_pair(path: Path, state: dict, log: dict, *, checkpoint_status: str | None = None,
              codex_checkpoint: bool = False) -> None:
    """Journal pair and optional fixed-path checkpoints before replacing any file."""
    validate_pair(path, state, log)
    pending = companion(path, "pending")
    bundle = {"state": state, "log": log}
    if checkpoint_status is not None:
        if checkpoint_status not in {"ANALYZING", "IMPLEMENTING", "WAITING_APPROVAL"}:
            raise ValueError("Invalid checkpoint resume status")
        bundle["checkpoint_status"] = checkpoint_status
    if type(codex_checkpoint) is not bool:
        raise ValueError("Invalid Codex checkpoint flag")
    if codex_checkpoint:
        bundle["codex_checkpoint"] = True
    atomic_json(pending, bundle)
    _write_bundle(path, bundle)
    pending.unlink()


def recover_pair(path: Path) -> None:
    """Roll forward an interrupted save without appending another transition."""
    pending = companion(path, "pending")
    if not pending.exists():
        validate_pair(path, load_json(path), load_json(companion(path, "log")))
        return
    bundle = load_json(pending)
    _write_bundle(path, bundle)
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
