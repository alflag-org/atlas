"""Publish one complete host artifact generation for Atlas."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .catalog import command_index
from .files import remove_path
from .generations import _remove_unleased_generation, active_generation
from .locks import acquire_lock
from .paths import AtlasPaths


@dataclass(frozen=True)
class _LauncherState:
    content: bytes
    mode: int


@dataclass(frozen=True)
class HostArtifactState:
    """Mutable host selection state and pre-existing artifact generations."""

    backup_root: Path
    entries: tuple[tuple[Path, Path], ...]
    artifact_generation_names: frozenset[str]


def _atomic_write(path: Path, content: str) -> None:
    """Replace one host-owned regular file without exposing partial content."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"host artifact must be a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp.{uuid4().hex}"
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o755)
        temporary.replace(path)
    finally:
        remove_path(temporary)


def _capture_launcher(path: Path) -> _LauncherState | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"host artifact must be a regular file: {path}")
    if not path.exists():
        return None
    return _LauncherState(content=path.read_bytes(), mode=path.stat().st_mode & 0o777)


def _restore_launcher(path: Path, state: _LauncherState | None) -> None:
    if state is None:
        remove_path(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.restore.{uuid4().hex}"
    try:
        temporary.write_bytes(state.content)
        temporary.chmod(state.mode)
        temporary.replace(path)
    finally:
        remove_path(temporary)


def _copy_state_entry(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    elif source.is_file():
        shutil.copy2(source, destination)
    else:
        raise ValueError(f"host artifact state entry is not a regular path: {source}")


def _artifact_state_paths(paths: AtlasPaths) -> tuple[Path, ...]:
    return (
        paths.artifact_current,
        paths.home / "lib",
        paths.shims,
        paths.bin_dir / "atlas",
        paths.artifact_runner,
        *_legacy_state_paths(paths),
    )


@contextmanager
def capture_host_artifact_state(paths: AtlasPaths) -> Iterator[HostArtifactState]:
    """Capture mutable host selection state without copying immutable generations."""
    generations = paths.artifact_root / "generations"
    if generations.is_symlink() or not generations.is_dir():
        raise ValueError(f"artifact generations path must be a directory: {generations}")
    generation_names = frozenset(path.name for path in generations.iterdir())
    with TemporaryDirectory(prefix="artifact-state.", dir=paths.tmp) as temporary:
        backup_root = Path(temporary)
        entries: list[tuple[Path, Path]] = []
        for index, path in enumerate(_artifact_state_paths(paths)):
            if not path.exists() and not path.is_symlink():
                continue
            backup = backup_root / str(index)
            _copy_state_entry(path, backup)
            entries.append((path, backup))
        yield HostArtifactState(
            backup_root=backup_root,
            entries=tuple(entries),
            artifact_generation_names=generation_names,
        )


def _legacy_state_paths(paths: AtlasPaths) -> tuple[Path, ...]:
    return tuple(
        sorted(
            [
                *paths.home.glob(".shims.legacy.*"),
                *(paths.home / "lib").glob(".python.legacy.*"),
            ]
        )
    )


def restore_host_artifact_state(paths: AtlasPaths, state: HostArtifactState) -> None:
    """Restore mutable links and launchers, then clean only new safe candidates."""
    current_paths = {
        path
        for path, _ in state.entries
    }
    current_paths.update(_artifact_state_paths(paths))
    current_paths.update(_legacy_state_paths(paths))
    for path in sorted(current_paths, key=lambda item: len(item.parts), reverse=True):
        remove_path(path)
    for path, backup in sorted(state.entries, key=lambda item: len(item[0].parts)):
        _copy_state_entry(backup, path)
    generations = paths.artifact_root / "generations"
    if not generations.is_dir() or generations.is_symlink():
        return
    for generation in sorted(generations.iterdir()):
        if generation.name in state.artifact_generation_names or generation.name.startswith("."):
            continue
        if generation.is_symlink() or not generation.is_dir():
            continue
        _remove_unleased_generation(
            generations,
            generation,
            active_link=paths.artifact_current,
            label="artifact generation",
        )


def _atlas_launcher_content() -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec \"{sys.executable}\" -m atlas.cli \"$@\"\n"
    )


def _artifact_runner_content(atlas_launcher: Path) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "name=\"$(basename \"$0\")\"\n"
        f"exec \"{atlas_launcher}\" run \"$name\" \"$@\"\n"
    )


def _validate_artifact_current(paths: AtlasPaths) -> None:
    current = paths.artifact_current
    if not current.exists() and not current.is_symlink():
        return
    if not current.is_symlink():
        raise ValueError(f"active artifact generation must be a symlink: {current}")
    generations = (paths.artifact_root / "generations").resolve()
    active_generation(current, generations, label="artifact generation")


def active_artifact_generation(paths: AtlasPaths) -> Path:
    """Resolve the concrete generation selected by ``artifacts/current``."""
    return active_generation(
        paths.artifact_current,
        paths.artifact_root / "generations",
        label="artifact generation",
    )


def _ensure_generation_link(
    path: Path,
    relative_target: Path,
) -> Path | None:
    """Ensure a stable path follows the one active generation.

    The returned path is a legacy directory moved aside during first publication.
    """
    expected = (path.parent / relative_target).resolve(strict=False)
    if path.is_symlink():
        actual = (path.parent / os.readlink(path)).resolve(strict=False)
        if actual != expected:
            raise ValueError(f"host artifact link escapes Atlas home: {path}")
        return None
    if path.exists() and not path.is_dir():
        raise ValueError(f"host artifact link destination must be a directory: {path}")
    backup: Path | None = None
    if path.exists():
        backup = path.parent / f".{path.name}.legacy.{uuid4().hex}"
        path.rename(backup)
    try:
        path.symlink_to(relative_target, target_is_directory=True)
    except BaseException:
        if backup is not None:
            remove_path(path)
            backup.rename(path)
        raise
    return backup


def _stage_generation(paths: AtlasPaths, names: list[str]) -> Path:
    generations = paths.artifact_root / "generations"
    generation_name = uuid4().hex
    staging = generations / f".{generation_name}.tmp"
    final = generations / generation_name
    remove_path(staging)
    staging.mkdir(parents=True)
    try:
        source_core = Path(__file__).resolve().parents[1] / "atlas_core"
        shutil.copytree(source_core, staging / "python/atlas_core")
        shutil.copyfile(
            Path(__file__).with_name("release_runner.py"),
            staging / "python/atlas_release_runner.py",
        )
        shutil.copyfile(
            Path(__file__).with_name("target_contract.py"),
            staging / "python/target_contract.py",
        )
        shims = staging / "shims"
        shims.mkdir()
        if paths.shims.is_dir():
            for item in paths.shims.iterdir():
                if item.is_dir() and not item.is_symlink():
                    (shims / item.name).mkdir()
        for name in names:
            shim = shims / name
            if shim.exists() or shim.is_symlink():
                raise ValueError(f"duplicate generated shim: {shim}")
            shim.symlink_to(paths.artifact_runner)
        for path in (
            staging / "python/atlas_release_runner.py",
            staging / "python/target_contract.py",
        ):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"staged host artifact is not a regular file: {path}")
        final.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(final)
    except BaseException:
        remove_path(staging)
        raise
    return final


def _publish_current(paths: AtlasPaths, generation: Path) -> None:
    current = paths.artifact_current
    temporary = current.parent / f".{current.name}.tmp.{uuid4().hex}"
    try:
        temporary.symlink_to(Path("generations") / generation.name, target_is_directory=True)
        temporary.replace(current)
    finally:
        remove_path(temporary)


def _restore_current(paths: AtlasPaths, previous_target: str | None) -> None:
    """Restore the exact previously published artifact link target."""
    current = paths.artifact_current
    if previous_target is None:
        remove_path(current)
        return
    temporary = current.parent / f".{current.name}.restore.{uuid4().hex}"
    try:
        temporary.symlink_to(previous_target, target_is_directory=True)
        temporary.replace(current)
    finally:
        remove_path(temporary)


def publish_host_artifacts(paths: AtlasPaths, *, _lock_held: bool = False) -> list[str]:
    """Stage and atomically switch Atlas core, runner, and command shims."""
    lock_context = (
        nullcontext()
        if _lock_held
        else acquire_lock(paths.locks, "host-artifacts", wait=True)
    )
    with lock_context:
        return _publish_host_artifacts(paths)


def _publish_host_artifacts(paths: AtlasPaths) -> list[str]:
    paths.artifact_root.mkdir(parents=True, exist_ok=True)
    generations = paths.artifact_root / "generations"
    if generations.is_symlink() or (generations.exists() and not generations.is_dir()):
        raise ValueError(f"artifact generations path must be a directory: {generations}")
    generations.mkdir(parents=True, exist_ok=True)
    _validate_artifact_current(paths)

    atlas_launcher = paths.bin_dir / "atlas"
    launcher_states = {
        atlas_launcher: _capture_launcher(atlas_launcher),
        paths.artifact_runner: _capture_launcher(paths.artifact_runner),
    }
    paths.home.joinpath("lib").mkdir(parents=True, exist_ok=True)
    names = list(command_index(paths.current_root, paths.releases_root))
    generation = _stage_generation(paths, names)
    previous_current = (
        os.readlink(paths.artifact_current)
        if paths.artifact_current.is_symlink()
        else None
    )
    backups: list[tuple[Path, Path]] = []
    try:
        for path, relative_target in (
            (paths.home / "lib/python", Path("../artifacts/current/python")),
            (paths.shims, Path("artifacts/current/shims")),
        ):
            backup = _ensure_generation_link(
                path,
                relative_target,
            )
            if backup is not None:
                backups.append((path, backup))
        _publish_current(paths, generation)
        _atomic_write(atlas_launcher, _atlas_launcher_content())
        _atomic_write(
            paths.artifact_runner,
            _artifact_runner_content(atlas_launcher),
        )
    except BaseException:
        rollback_error: BaseException | None = None
        try:
            _restore_current(paths, previous_current)
        except BaseException as exc:
            rollback_error = exc
        for path, backup in reversed(backups):
            try:
                remove_path(path)
                backup.rename(path)
            except BaseException as exc:
                rollback_error = rollback_error or exc
        for path, state in launcher_states.items():
            try:
                _restore_launcher(path, state)
            except BaseException as exc:
                rollback_error = rollback_error or exc
        _remove_unleased_generation(
            generations,
            generation,
            active_link=paths.artifact_current,
            label="artifact generation",
        )
        if rollback_error is not None:
            raise RuntimeError("host artifact publication rollback failed") from rollback_error
        raise
    for _, backup in backups:
        remove_path(backup)
    return names
