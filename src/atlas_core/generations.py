"""Child-owned leases for immutable Atlas runtime selections."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

_HANDOFF_RUNTIME_FD = "ATLAS_LEASE_HANDOFF_RUNTIME_FD"
_HANDOFF_ARTIFACT_FD = "ATLAS_LEASE_HANDOFF_ARTIFACT_FD"
_HANDOFF_ACK_FD = "ATLAS_LEASE_HANDOFF_ACK_FD"
_HANDOFF_ENVIRONMENT_KEYS = (
    _HANDOFF_RUNTIME_FD,
    _HANDOFF_ARTIFACT_FD,
    _HANDOFF_ACK_FD,
)


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


def _handoff_fds() -> tuple[int, int, int] | None:
    values = [os.environ.get(key) for key in _HANDOFF_ENVIRONMENT_KEYS]
    if all(value is None for value in values):
        return None
    if not all(value is not None for value in values):
        raise ValueError("generation lease handoff variables are all required")
    try:
        fds = tuple(int(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError("generation lease handoff descriptors must be integers") from exc
    if len(set(fds)) != 3 or any(fd <= 2 for fd in fds):
        raise ValueError("generation lease handoff descriptors are invalid")
    return fds[0], fds[1], fds[2]


def _handoff_lease_path(
    fd: int,
    generations: Path,
    generation: Path,
    *,
    label: str,
) -> Path:
    try:
        mode = os.fstat(fd).st_mode
    except OSError as exc:
        raise ValueError(f"{label} handoff descriptor is unavailable") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} handoff descriptor is not a lease file")
    leases = _lease_root(generations)
    if leases.is_symlink() or not leases.is_dir():
        raise ValueError(f"{label} generation leases path is invalid")
    try:
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError as exc:
        raise ValueError(f"{label} handoff descriptor path is unavailable") from exc
    if (
        path.parent.resolve() != leases.resolve()
        or not path.name.endswith(".lease")
        or path.is_symlink()
    ):
        raise ValueError(f"{label} handoff descriptor path is invalid")
    expected = f"{generation.name}\n".encode()
    try:
        content = os.pread(fd, len(expected) + 1, 0)
    except OSError as exc:
        raise ValueError(f"{label} handoff descriptor cannot be read") from exc
    if content != expected:
        raise ValueError(f"{label} handoff descriptor selects the wrong generation")
    return path


def _validate_handoff(
    runtime_fd: int,
    artifact_fd: int,
    ack_fd: int,
    runtime_generation: Path,
    artifact_generation: Path,
) -> None:
    runtime_path = _handoff_lease_path(
        runtime_fd,
        runtime_generation.parent,
        runtime_generation,
        label="runtime",
    )
    artifact_path = _handoff_lease_path(
        artifact_fd,
        artifact_generation.parent,
        artifact_generation,
        label="artifact",
    )
    if runtime_path.name != artifact_path.name:
        raise ValueError("runtime and artifact handoff leases do not match")
    try:
        mode = os.fstat(ack_fd).st_mode
    except OSError as exc:
        raise ValueError("generation lease handoff acknowledgement is unavailable") from exc
    if not stat.S_ISFIFO(mode):
        raise ValueError("generation lease handoff acknowledgement is not a pipe")


def _send_handoff_ack(fd: int, acknowledged: bool) -> None:
    try:
        os.write(fd, b"1" if acknowledged else b"0")
    except OSError:
        # A hard-killed parent closes the acknowledgement reader; child-owned
        # leases are already active when the success acknowledgement is sent.
        pass


def _close_handoff_fds(fds: tuple[int, int, int] | None) -> None:
    if fds is None:
        return
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


@contextmanager
def generation_lease_from_environment() -> Iterator[None]:
    """Hold leases in the actual release child for its complete lifetime."""
    runtime_raw = os.environ.get("ATLAS_RUNTIME_GENERATION")
    artifact_raw = os.environ.get("ATLAS_ARTIFACT_GENERATION")
    handoff: tuple[int, int, int] | None = None
    acknowledged = False
    try:
        handoff = _handoff_fds()
        if runtime_raw is None and artifact_raw is None:
            if handoff is not None:
                raise ValueError("generation selections are required with a lease handoff")
            yield
            return
        if runtime_raw is None or artifact_raw is None:
            raise ValueError("runtime and artifact generation selections are both required")
        runtime_generation = Path(runtime_raw)
        artifact_generation = Path(artifact_raw)
        if handoff is not None:
            _validate_handoff(
                *handoff,
                runtime_generation,
                artifact_generation,
            )
        lease_id = uuid4().hex
        with ExitStack() as leases:
            leases.enter_context(
                _generation_lease(runtime_generation.parent, runtime_generation, lease_id)
            )
            leases.enter_context(
                _generation_lease(artifact_generation.parent, artifact_generation, lease_id)
            )
            if handoff is not None:
                _send_handoff_ack(handoff[2], True)
                acknowledged = True
            yield
    except BaseException:
        if handoff is not None and not acknowledged:
            _send_handoff_ack(handoff[2], False)
        raise
    finally:
        _close_handoff_fds(handoff)
        for key in _HANDOFF_ENVIRONMENT_KEYS:
            os.environ.pop(key, None)
