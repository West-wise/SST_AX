"""Read an operator-owned credential file without shell expansion or env export."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from slack_runner import configuration

FIELDS = {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_TEAM_ID", "SLACK_CHANNEL_ID",
          "SLACK_DAILY_REPORT_CHANNEL_ID", "SLACK_APP_ID", "SLACK_APPROVER_IDS"}


def read_credentials(path: Path) -> dict[str, str]:
    """Keep secrets in this process; callers must run in the isolated Gateway UID."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                    info.st_nlink != 1 or info.st_size > 16_384):
                raise ValueError
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ValueError
        values = {}
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if not separator or name not in FIELDS or name in values:
                raise ValueError
            if value[:1] in {"'", '"'}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError
                value = value[1:-1]
            if not value or any(char.isspace() or ord(char) < 32 for char in value):
                raise ValueError
            values[name] = value
        if set(values) != FIELDS:
            raise ValueError
        result = configuration(values)
        channel = values["SLACK_DAILY_REPORT_CHANNEL_ID"]
        if not re.fullmatch(r"[CG][A-Z0-9]+", channel):
            raise ValueError
        result["SLACK_DAILY_REPORT_CHANNEL_ID"] = channel
        return result
    except (OSError, UnicodeError, ValueError):
        # Exception bodies and field values must never reach diagnostics.
        raise ValueError("SLACK_CREDENTIAL_FILE_REFUSED") from None


def credential_path(environ: dict[str, str]) -> Path:
    directory = environ.get("CREDENTIALS_DIRECTORY")
    if not directory or not Path(directory).is_absolute():
        raise ValueError("SLACK_CREDENTIAL_DIRECTORY_REQUIRED")
    return Path(directory) / "slack.env"
