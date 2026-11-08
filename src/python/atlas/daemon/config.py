"""Host-owned daemon instance declarations."""

from __future__ import annotations

import math
import os
import pwd
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from atlas.yamlutil import load_yaml_file


def instance_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", value):
        raise ValueError("invalid daemon instance name")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ValueError("daemon fields must be strings without control characters")
    return value


def _path(value: object) -> Path:
    path = Path(_text(value))
    if not path.is_absolute() or ".." in path.parts or str(path) != str(path).strip():
        raise ValueError("daemon paths must be absolute without '..'")
    return path


def trusted_file(path: Path) -> None:
    """Reject links, special files and declarations writable by another account."""
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("daemon configuration directory must not be a symlink")
        if info.st_uid not in (0, os.geteuid()) or (
            info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
        ):
            raise ValueError("daemon configuration directory is not owner controlled")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in (0, os.geteuid())
        or info.st_mode & 0o022
    ):
        raise ValueError(
            "daemon configuration must be an owner-controlled regular file"
        )


@dataclass(frozen=True)
class DaemonInstance:
    name: str
    executable: str
    arguments: tuple[str, ...]
    user: str
    working_directory: str
    environment_files: tuple[str, ...] = ()
    restart: str = "on-failure"
    restart_delay_seconds: float = 5
    startup_timeout_seconds: float = 30
    stop_timeout_seconds: float = 30
    notify: bool = True

    @property
    def unit(self) -> str:
        return f"atlas-daemon-{self.name}.service"

    def to_dict(self) -> dict:
        return {"schema": "atlas.daemon-instance/v1", **asdict(self)}


def load_instance(
    directory: Path, name: str, *, check_resources: bool = False
) -> DaemonInstance:
    path = directory / f"{instance_name(name)}.yml"
    trusted_file(path)
    try:
        data = load_yaml_file(path)
    except yaml.YAMLError:
        raise ValueError("invalid daemon YAML") from None
    fields = set(DaemonInstance.__dataclass_fields__)
    if not isinstance(data, dict) or data.keys() - fields - {"schema"}:
        raise ValueError("invalid daemon instance fields")
    if data.get("schema") != "atlas.daemon-instance/v1" or data.get("name") != name:
        raise ValueError("daemon schema or instance name mismatch")
    required = {"name", "executable", "user", "working_directory"}
    if not required <= data.keys():
        raise ValueError("missing required daemon instance fields")
    for field in ("executable", "working_directory"):
        target = _path(data[field])
        if not check_resources:
            continue
        if field == "executable":
            if not target.is_file() or not os.access(target, os.X_OK):
                raise ValueError("daemon executable must be an executable file")
        elif not target.is_dir():
            raise ValueError("daemon working_directory must exist")
    user = _text(data["user"])
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", user):
        raise ValueError("daemon user must be an account name")
    try:
        pwd.getpwnam(user)
    except KeyError:
        raise ValueError("daemon user does not exist") from None
    for field in ("arguments", "environment_files"):
        values = data.get(field, [])
        if not isinstance(values, list):
            raise TypeError(f"daemon {field} must be a list")
        data[field] = tuple(_text(value) for value in values)
    for value in data["environment_files"]:
        target = _path(value)
        if check_resources:
            trusted_file(target)
    for field in (
        "restart_delay_seconds",
        "startup_timeout_seconds",
        "stop_timeout_seconds",
    ):
        value = data.get(field, 5 if field == "restart_delay_seconds" else 30)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"daemon {field} must be finite and positive")
    if data.get("restart", "on-failure") not in ("no", "on-failure", "always"):
        raise ValueError("unsupported daemon restart policy")
    if type(data.get("notify", True)) is not bool:
        raise ValueError("daemon notify must be a boolean")
    return DaemonInstance(
        **{key: value for key, value in data.items() if key != "schema"}
    )
