"""Child-owned leases for immutable Atlas runtime selections."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4


def _lease_root(generations: Path) -> Path:
    return generations.parent / "leases"


@contextmanager
def _generation_lease(
    generations: Path,
    generation: Path,
    lease_id: str,
) -> Iterator[None]:
    if not generations.is_absolute() or not generation.is_absolute():
        raise ValueError("generation lease paths must be absolute")
    if generations.is_symlink() or not generations.is_dir():
        raise ValueError(f"generation path must be a directory: {generations}")
    if (
        generation.parent.resolve() != generations.resolve()
        or generation.is_symlink()
        or not generation.is_dir()
    ):
        raise ValueError(f"generation lease target is not a generation: {generation}")
    leases = _lease_root(generations)
    if leases.is_symlink() or (leases.exists() and not leases.is_dir()):
        raise ValueError(f"generation leases path must be a directory: {leases}")
    leases.mkdir(parents=True, exist_ok=True)
    path = leases / f"{lease_id}.lease"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise ValueError(f"generation lease must be a regular file: {path}") from exc
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(generation.name + "\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # A stale lease is safer than deleting a generation still in use.
                pass


@contextmanager
def generation_lease_from_environment() -> Iterator[None]:
    """Hold leases in the actual release child for its complete lifetime."""
    runtime_raw = os.environ.get("ATLAS_RUNTIME_GENERATION")
    artifact_raw = os.environ.get("ATLAS_ARTIFACT_GENERATION")
    if runtime_raw is None and artifact_raw is None:
        yield
        return
    if runtime_raw is None or artifact_raw is None:
        raise ValueError("runtime and artifact generation selections are both required")
    runtime_generation = Path(runtime_raw)
    artifact_generation = Path(artifact_raw)
    lease_id = uuid4().hex
    with ExitStack() as leases:
        leases.enter_context(
            _generation_lease(runtime_generation.parent, runtime_generation, lease_id)
        )
        leases.enter_context(
            _generation_lease(artifact_generation.parent, artifact_generation, lease_id)
        )
        yield
