"""Private filesystem and service-manager adapters for independent daemons."""

from __future__ import annotations

import os
import socket
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in value:
        raise ValueError("daemon directory must be an absolute path without parent traversal")
    return path


def _private_directory(path: Path) -> Path:
    # Check ancestors before creating children: never write through a symlink.
    for directory in (*reversed(path.parents), path):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("daemon directory components must be real directories")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("daemon directory must belong to the caller and have mode 0700")
    if not os.access(path, os.W_OK | os.X_OK):
        raise PermissionError("daemon directory is not writable")
    return path


def _manager_directory(key: str) -> Path | None:
    value = os.environ.get(key)
    if value is None:
        return None
    if ":" in value:
        raise ValueError("daemon requires exactly one service-manager directory")
    return _private_directory(_absolute(value))


@contextmanager
def directories(name: str) -> Iterator[tuple[Path, Path]]:
    """Retain persistent state; remove only a runtime directory created by mkdtemp."""
    state = _manager_directory("STATE_DIRECTORY")
    if state is None:
        value = os.environ.get("XDG_STATE_HOME")
        base = _absolute(value) if value else _absolute(str(Path.home())) / ".local/state"
        state = _private_directory(base / "atlas" / name)
    runtime = _manager_directory("RUNTIME_DIRECTORY")
    if runtime is not None:
        yield state, runtime
        return
    value = os.environ.get("XDG_RUNTIME_DIR")
    if value:
        base = _private_directory(_absolute(value))
        yield state, _private_directory(base / "atlas" / name)
        return
    # Random, owner-only fallback: never claim a predictable shared /tmp socket path.
    with tempfile.TemporaryDirectory(prefix=f"{name}-") as temporary:
        yield state, Path(temporary)


def notify_ready() -> None:
    """Send readiness once requested; absence of systemd is a normal standalone case."""
    address = os.environ.get("NOTIFY_SOCKET")
    if address is None:
        return
    if not address or address[0] not in "/@" or address in ("/", "@") or "\0" in address:
        raise ValueError("invalid readiness socket")
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        # A full or unavailable manager socket must not hang the event loop.
        connection.setblocking(False)
        connection.sendto(b"READY=1", address)
