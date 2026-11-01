"""Private membership files with atomic replacement and local write serialization."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from atlas_core.cluster import ClusterError

MAX_BYTES = 1024 * 1024


def private_file(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(fd)
        raise ClusterError("unsafe_state")
    return fd


class Store:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or (
            Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
            / "atlas/cluster"
        )
        self.path = self.directory / "membership.json"

    @contextmanager
    def locked(self) -> Iterator[Store]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ClusterError("unsafe_state")
        fd = private_file(self.directory / ".lock", os.O_CREAT | os.O_RDWR)
        try:
            # Only short local transactions hold this lock, never SSH requests.
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self
        finally:
            os.close(fd)

    def read(self) -> dict | None:
        try:
            fd = private_file(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            data = stream.read(MAX_BYTES + 1)
        try:
            value = json.loads(data)
            if len(data) > MAX_BYTES or value["schema"] != "atlas.membership/v1":
                raise ValueError
            return value
        except (ValueError, KeyError, TypeError):
            raise ClusterError("invalid_state") from None

    def write(self, state: dict) -> None:
        data = json.dumps(state, sort_keys=True).encode()
        if len(data) > MAX_BYTES:
            raise ClusterError("membership_too_large")
        fd, temporary = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self) -> None:
        self.path.unlink()
