"""Bounded local IPC; no Slack credentials or user-authored messages cross it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
import struct

TASK_ID = re.compile(r"(?:sstd-sync|sstc-feature)-[0-9]{8}-[0-9]{4}")
MAX_FRAME = 32_768


def peer_uid(connection) -> int:
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                    struct.calcsize("3i")))[1]


def read_frame(connection) -> dict:
    data = bytearray()
    while len(data) <= MAX_FRAME:
        chunk = connection.recv(min(4096, MAX_FRAME + 1 - len(data)))
        if not chunk:
            raise ValueError("GATEWAY_INCOMPLETE_FRAME")
        data.extend(chunk)
        if b"\n" in chunk:
            line, rest = bytes(data).split(b"\n", 1)
            if rest or len(line) > MAX_FRAME:
                raise ValueError("GATEWAY_FRAME_REFUSED")
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("GATEWAY_DUPLICATE_FIELD")
                    result[key] = value
                return result
            value = json.loads(line.decode("utf-8"), object_pairs_hook=unique)
            if not isinstance(value, dict):
                raise ValueError("GATEWAY_OBJECT_REQUIRED")
            return value
    raise ValueError("GATEWAY_FRAME_TOO_LARGE")


def write_frame(connection, value: dict) -> None:
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_FRAME:
        raise ValueError("GATEWAY_FRAME_TOO_LARGE")
    connection.sendall(raw + b"\n")


class Gateway:
    def __init__(self, socket_path: Path, gateway_uid: int):
        if (not socket_path.is_absolute() or type(gateway_uid) is not int or
                gateway_uid <= 0 or gateway_uid == os.getuid()):
            raise ValueError("ISOLATED_GATEWAY_REQUIRED")
        self.path, self.uid = socket_path, gateway_uid

    def request(self, payload: dict) -> str:
        try:
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                raise ValueError
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(55)
                connection.connect(str(self.path))
                if peer_uid(connection) != self.uid:
                    raise ValueError
                write_frame(connection, payload)
                response = read_frame(connection)
            if set(response) != {"status"} or response["status"] not in {"OK", "REFUSED"}:
                raise ValueError
            return response["status"]
        except (OSError, ValueError, UnicodeError):
            raise ValueError("SLACK_GATEWAY_UNAVAILABLE_OR_REFUSED") from None

    def poll(self, task_files) -> int:
        ids = [path.stem for path in task_files]
        if len(ids) > 128 or len(set(ids)) != len(ids) or any(not TASK_ID.fullmatch(x) for x in ids):
            raise ValueError("GATEWAY_TASK_IDS_REFUSED")
        return 0 if self.request({"operation": "approvals", "task_ids": ids}) == "OK" else 2

    def check(self) -> int:
        return 0 if self.request({"operation": "check"}) == "OK" else 2
