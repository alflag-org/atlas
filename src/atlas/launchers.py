"""Publish one complete host artifact generation for Atlas."""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from .catalog import command_index
from .files import remove_path
from .locks import acquire_lock
from .paths import AtlasPaths


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
    raw = os.readlink(current)
    target = (current.parent / raw).resolve(strict=False)
    generations = (paths.artifact_root / "generations").resolve()
    if generations not in target.parents:
        raise ValueError(f"artifact generation escapes its root: {current}")
    if not target.is_dir():
        raise ValueError(f"active artifact generation is missing: {current}")


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
        temporary.symlink_to(generation, target_is_directory=True)
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
    _atomic_write(atlas_launcher, _atlas_launcher_content())
    _atomic_write(
        paths.artifact_runner,
        _artifact_runner_content(atlas_launcher),
    )
    paths.home.joinpath("lib").mkdir(parents=True, exist_ok=True)
    names = list(command_index(paths.current_root, paths.releases_root))
    generation = _stage_generation(paths, names)
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
    except BaseException:
        for path, backup in reversed(backups):
            path.unlink()
            backup.rename(path)
        remove_path(generation)
        raise
    for _, backup in backups:
        remove_path(backup)
    return names
