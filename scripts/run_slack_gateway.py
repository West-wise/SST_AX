#!/usr/bin/env python3
"""Isolated Slack Gateway: authenticated approvals and scheduled Korean reports."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import time

sys.dont_write_bytecode = True

from impact_validation import load_document
from slack_credentials import credential_path, read_credentials
from slack_gateway_client import TASK_ID, peer_uid, read_frame, write_frame
from slack_runner import listen_many, run as slack_run


def configuration(path: Path) -> dict:
    config = load_document(path)
    keys = {"socket_path", "tasks_directory", "reports_directory", "client_uid"}
    if not isinstance(config, dict) or set(config) != keys:
        raise ValueError("GATEWAY_CONFIGURATION_REFUSED")
    info = path.stat()
    if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022:
        raise ValueError("GATEWAY_CONFIGURATION_OWNER_REQUIRED")
    if type(config["client_uid"]) is not int or config["client_uid"] <= 0 or config["client_uid"] == os.getuid():
        raise ValueError("GATEWAY_SEPARATE_UID_REQUIRED")
    for key in keys - {"client_uid"}:
        value = config[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("GATEWAY_ABSOLUTE_PATH_REQUIRED")
        config[key] = Path(value)
    root = config["tasks_directory"]
    if not root.is_dir() or root.resolve() != root.absolute():
        raise ValueError("GATEWAY_TASK_DIRECTORY_REQUIRED")
    reports = config["reports_directory"]
    if reports.resolve() == root.resolve() or root.resolve() in reports.resolve().parents:
        raise ValueError("GATEWAY_REPORT_DIRECTORY_REFUSED")
    return config


def scoped_task(root: Path, task_id: str) -> Path:
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise ValueError("GATEWAY_TASK_ID_REFUSED")
    path = root / (task_id + ".json")
    for item in root.glob(task_id + "*"):
        if item.is_symlink() or root.resolve() not in item.resolve().parents:
            raise ValueError("GATEWAY_TASK_SYMLINK_REFUSED")
    if path.resolve().parent != root.resolve() or not path.is_file():
        raise ValueError("GATEWAY_TASK_SCOPE_REFUSED")
    # Inputs are locally preserved, and must never select another arbitrary path.
    log = load_document(root / (task_id + ".log.json"))
    inputs = log.get("codex_analysis", {}).get("inputs_directory")
    if inputs is not None:
        location = Path(inputs)
        if (not location.is_absolute() or location.resolve().parent != root.resolve() or
                not location.name.startswith(task_id + ".inputs")):
            raise ValueError("GATEWAY_INPUT_SCOPE_REFUSED")
    return path


def dispatch(request: dict, config: dict, credentials: dict) -> str:
    # Only fixed authenticated operations. No payload, token, channel or text API.
    if request == {"operation": "check"}:
        args = argparse.Namespace(command="check", task_file=None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = slack_run(args, credentials)
        return "OK" if code == 0 else "REFUSED"
    if (set(request) != {"operation", "task_ids"} or request["operation"] != "approvals" or
            not isinstance(request["task_ids"], list) or not 0 < len(request["task_ids"]) <= 128):
        raise ValueError("GATEWAY_OPERATION_REFUSED")
    ids = request["task_ids"]
    if any(not isinstance(value, str) for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("GATEWAY_TASK_IDS_REFUSED")
    paths = [scoped_task(config["tasks_directory"], value) for value in ids]
    # Slack approval core still verifies analysis hashes, checkpoint, workspace,
    # app, channel, human, nonce, expiry and exact message before any transition.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = listen_many(paths, credentials, timeout_seconds=5)
    return "OK" if code == 0 else "REFUSED"


def report_tick(config: dict, credentials: dict, now: datetime | None = None) -> str | None:
    from daily_report import build_report, due_date, send_report
    from task_storage import atomic_json
    day = due_date(now or datetime.now(timezone.utc))
    if day is None:
        return None
    checkpoint = config["reports_directory"] / "gateway-report-checkpoint.json"
    record = load_document(checkpoint) if checkpoint.exists() else {}
    if record.get("report_date") != day.isoformat():
        record = {"report_date": day.isoformat(), "failures": 0, "status": None}
    if type(record.get("failures")) is not int or not 0 <= record["failures"] <= 3:
        raise ValueError("REPORT_CHECKPOINT_REFUSED")
    if record["failures"] >= 3 or record["status"] in {"UNCERTAIN", "RETRY_LIMIT"}:
        return "REVIEW_REQUIRED"
    try:
        report = build_report(config["tasks_directory"], day)
        status = send_report(report, config["reports_directory"], credentials)
    except Exception:
        status = "INPUT_OR_TRANSPORT_REVIEW_REQUIRED"
    if status not in {"SENT", "ALREADY_SENT", "UNCERTAIN", "RETRY_LIMIT", "RETRY_LATER"}:
        record["failures"] += 1
    record["status"] = status
    atomic_json(checkpoint, record)
    return status


def serve(config: dict, credentials: dict) -> None:
    path = config["socket_path"]
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022 or path.exists():
        raise ValueError("GATEWAY_SOCKET_DIRECTORY_REFUSED")
    stop = False
    def finish(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        os.chmod(path, 0o660)
        server.listen(8)
        server.settimeout(1)
        next_report, previous_report = 0.0, None
        print("SLACK_GATEWAY=READY", flush=True)
        try:
            while not stop:
                if time.monotonic() >= next_report:
                    try:
                        status = report_tick(config, credentials)
                    except Exception:
                        status = "INPUT_OR_TRANSPORT_REVIEW_REQUIRED"
                    if status and status != previous_report:
                        print("SLACK_DAILY_REPORT=" + status, flush=True)
                    previous_report, next_report = status, time.monotonic() + 60
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    status = "REFUSED"
                    try:
                        if peer_uid(connection) == config["client_uid"]:
                            status = dispatch(read_frame(connection), config, credentials)
                    except Exception:
                        pass
                    try:
                        write_frame(connection, {"status": status})
                    except (OSError, ValueError):
                        pass
        finally:
            path.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Authenticate without sending or updating Tasks")
    mode.add_argument("--report-once", action="store_true", help="One scheduled report, without a persistent service")
    parser.add_argument("--credentials-file", type=Path,
                        help="Operator-owned private file for one-shot acceptance only")
    parser.add_argument("--report-snapshot", type=Path,
                        help="Prevalidated safe projection for one-shot acceptance only")
    args = parser.parse_args(argv)
    try:
        config = configuration(args.config)
        # Validate separate UID before reading any secret, including its path.
        if args.credentials_file and not (args.check or args.report_once):
            raise ValueError("SYSTEMD_CREDENTIALS_REQUIRED_FOR_SERVICE")
        if args.report_snapshot and not args.report_once:
            raise ValueError("REPORT_SNAPSHOT_IS_ACCEPTANCE_ONLY")
        credentials = read_credentials(args.credentials_file or credential_path(dict(os.environ)))
        if args.check:
            status = dispatch({"operation": "check"}, config, credentials)
            print("SLACK_GATEWAY_CHECK=" + status)
            return 0 if status == "OK" else 2
        if args.report_once:
            if args.report_snapshot:
                from daily_report import due_date, send_report, validate_report
                report = load_document(args.report_snapshot)
                validate_report(report)
                if report["report_date"] != str(due_date(datetime.now(timezone.utc))):
                    raise ValueError("REPORT_NOT_DUE")
                outcome = send_report(report, config["reports_directory"], credentials)
            else:
                outcome = report_tick(config, credentials)
            print("SLACK_DAILY_REPORT=" + (outcome or "NOT_DUE"))
            return 0 if outcome in {None, "SENT", "ALREADY_SENT"} else 2
        serve(config, credentials)
        return 0
    except Exception:
        print("SLACK_GATEWAY=CONFIGURATION_CREDENTIAL_OR_RUNTIME_REFUSED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
