"""Non-blocking advisory locks for Atlas jobs."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Iterator, TextIO

from .errors import LockUnavailableError
from .manifests import validate_name


@contextmanager
def acquire_lock(locks_root: Path, name: str) -> Iterator[Path]:
    """Acquire an OS-level advisory lock without waiting."""
    validate_name(name, kind="lock")
    locks_root.mkdir(parents=True, exist_ok=True)
    path = locks_root / f"{name}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle: TextIO = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockUnavailableError(f"lock is already held: {name}") from exc
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
