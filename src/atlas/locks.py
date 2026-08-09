"""Non-blocking advisory locks for Atlas jobs."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from .errors import LockUnavailableError
from .manifests import validate_name


@contextmanager
def acquire_lock(
    locks_root: Path,
    name: str,
    *,
    wait: bool = False,
) -> Iterator[Path]:
    """Acquire an OS-level advisory lock, optionally waiting for ownership."""
    validate_name(name, kind="lock")
    if locks_root.is_symlink() or (locks_root.exists() and not locks_root.is_dir()):
        raise ValueError(f"locks path must be a directory: {locks_root}")
    locks_root.mkdir(parents=True, exist_ok=True)
    path = locks_root / f"{name}.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise ValueError(f"lock file must be a regular file: {path}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"lock file must be a regular file: {path}")
    handle: TextIO = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        try:
            flags = fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise LockUnavailableError(f"lock is already held: {name}") from exc
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
