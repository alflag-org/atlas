"""Validation, installation, and activation of Atlas releases."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .files import remove_path
from .manifests import ReleaseManifest, load_manifest


@dataclass(frozen=True)
class ValidatedRelease:
    """A release directory after complete validation."""

    root: Path
    version: str
    manifest: ReleaseManifest


def read_version(release_root: Path) -> str:
    """Read the non-empty ``VERSION`` value from a release root."""
    version_file = release_root / "VERSION"
    if not version_file.is_file() or version_file.is_symlink():
        raise ValueError(f"missing VERSION file: {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION is empty")
    if "/" in version or "\\" in version or version in {".", ".."}:
        raise ValueError(f"invalid release version: {version}")
    return version


def validate_release(source: Path) -> ValidatedRelease:
    """Validate a release directory and its explicit command manifest."""
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"release directory not found: {source}")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"symlink is not allowed in release: {item}")
    return ValidatedRelease(
        root=source.resolve(),
        version=read_version(source),
        manifest=load_manifest(source),
    )


def _replace_directory(source: Path, target: Path) -> Path | None:
    pid = os.getpid()
    staging = target.parent / f"{target.name}.tmp.{pid}"
    backup = target.parent / f"{target.name}.bak.{pid}"
    remove_path(staging)
    remove_path(backup)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError(f"release target must be a directory: {target}")
    shutil.copytree(source, staging)

    replaced = False
    if target.exists():
        target.rename(backup)
        replaced = True
    try:
        staging.rename(target)
    except Exception:
        if replaced and backup.exists() and not target.exists():
            try:
                backup.rename(target)
            except Exception as restore_error:
                raise RuntimeError(
                    "release installation failed and the previous release could not be "
                    f"restored; backup retained at {backup}"
                ) from restore_error
        raise
    finally:
        remove_path(staging)
    return backup if replaced else None


def _replace_symlink(current_link: Path, target: Path) -> None:
    pid = os.getpid()
    tmp_link = current_link.parent / f"{current_link.name}.tmp.{pid}"
    remove_path(tmp_link)
    tmp_link.symlink_to(target, target_is_directory=True)
    tmp_link.replace(current_link)


def _current_target(link: Path) -> Path | None:
    if not link.exists() and not link.is_symlink():
        return None
    if not link.is_symlink():
        raise ValueError(f"current entry must be a symlink: {link}")
    target = link.resolve()
    if not target.is_dir():
        raise ValueError(f"active release target not found: {link}")
    return target


@contextmanager
def reversible_release_install(
    source: Path,
    releases_root: Path,
    current_root: Path,
) -> Iterator[Path]:
    """Restore the previous release directory and link if surrounding work fails."""
    release = validate_release(source)
    if current_root.exists() and (not current_root.is_dir() or current_root.is_symlink()):
        raise ValueError(f"current root must be a directory: {current_root}")
    target = releases_root / release.manifest.name / release.version
    link = current_root / release.manifest.name
    previous_target = _current_target(link)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _replace_directory(release.root, target)
    try:
        current_root.mkdir(parents=True, exist_ok=True)
        _replace_symlink(link, target)
        yield target
    except BaseException:
        try:
            remove_path(link)
            remove_path(target)
            if backup is not None:
                backup.rename(target)
            if previous_target is not None:
                _replace_symlink(link, previous_target)
        except Exception as rollback_error:
            recovery_path = backup if backup is not None and backup.exists() else target
            raise RuntimeError(
                "release installation failed and rollback failed; "
                f"recovery path: {recovery_path}"
            ) from rollback_error
        raise
    else:
        if backup is not None:
            remove_path(backup)


def install_release(source: Path, releases_root: Path, current_root: Path) -> Path:
    """Install and atomically activate one manifest-named release."""
    with reversible_release_install(source, releases_root, current_root) as target:
        return target
