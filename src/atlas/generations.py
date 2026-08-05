"""Generation links, child leases, and safe generation garbage collection."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from .files import remove_path


def active_generation(link: Path, generations: Path, *, label: str) -> Path:
    """Resolve one active generation link without following a chain."""
    if generations.is_symlink() or not generations.is_dir():
        raise ValueError(f"{label} generations path must be a directory: {generations}")
    if not link.exists() and not link.is_symlink():
        raise ValueError(f"active {label} is missing: {link}")
    if not link.is_symlink():
        raise ValueError(f"active {label} must be a symlink: {link}")
    raw = os.readlink(link)
    raw_path = Path(raw)
    if raw_path.is_absolute() or any(part == ".." for part in raw_path.parts):
        raise ValueError(f"active {label} contains path traversal: {link}")
    raw_target = link.parent / raw_path
    if (
        raw_target.parent.resolve() != generations.resolve()
        or raw_target.is_symlink()
        or not raw_target.is_dir()
    ):
        raise ValueError(f"active {label} target is not a generation: {link}")
    return raw_target.resolve()


def _lease_root(generations: Path) -> Path:
    return generations.parent / "leases"


@contextmanager
def _generation_lease(
    generations: Path,
    generation: Path,
    lease_id: str,
) -> Iterator[None]:
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
                remove_path(path)
            except OSError:
                # A stale lease is safer than deleting a generation still in use.
                pass


@contextmanager
def generation_lease(
    runtime_generations: Path,
    runtime_generation: Path,
    artifact_generations: Path,
    artifact_generation: Path,
) -> Iterator[None]:
    """Keep one concrete runtime and artifact generation alive for a child."""
    lease_id = uuid4().hex
    with ExitStack() as leases:
        leases.enter_context(_generation_lease(runtime_generations, runtime_generation, lease_id))
        leases.enter_context(_generation_lease(artifact_generations, artifact_generation, lease_id))
        yield


def _leased_names(generations: Path) -> tuple[set[str], bool]:
    leases = _lease_root(generations)
    if not leases.exists():
        return set(), True
    if leases.is_symlink() or not leases.is_dir():
        raise ValueError(f"generation leases path must be a directory: {leases}")
    names: set[str] = set()
    safe_to_collect = True
    for path in sorted(leases.iterdir()):
        if not path.name.endswith(".lease"):
            safe_to_collect = False
            continue
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            safe_to_collect = False
            continue
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.seek(0)
                name = handle.readline().strip()
                generation = generations / name
                if (
                    name
                    and Path(name).name == name
                    and name not in {".", ".."}
                    and generation.is_dir()
                    and not generation.is_symlink()
                ):
                    names.add(name)
                else:
                    safe_to_collect = False
                continue
            handle.seek(0)
            if handle.readline().strip():
                try:
                    remove_path(path)
                except OSError:
                    safe_to_collect = False
            else:
                safe_to_collect = False
        finally:
            handle.close()
    return names, safe_to_collect


def collect_generation_garbage(
    generations: Path,
    active_link: Path,
    *,
    label: str,
) -> None:
    """Best-effort remove of unreferenced, unleased generation directories."""
    if generations.is_symlink() or (generations.exists() and not generations.is_dir()):
        raise ValueError(f"{label} generations path must be a directory: {generations}")
    if not generations.exists():
        return
    active = active_generation(active_link, generations, label=label)
    leased, safe_to_collect = _leased_names(generations)
    if not safe_to_collect:
        return
    for generation in sorted(generations.iterdir()):
        if generation == active or generation.name in leased:
            continue
        if generation.name.startswith(".") or generation.is_symlink() or not generation.is_dir():
            continue
        try:
            remove_path(generation)
        except OSError:
            # A failed cleanup must leave the generation available for a later GC pass.
            continue
