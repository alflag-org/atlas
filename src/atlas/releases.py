"""Validation, installation, and activation of Atlas releases."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from .files import remove_path
from .manifests import ReleaseManifest, load_manifest


@dataclass(frozen=True)
class ValidatedRelease:
    """A release directory after complete validation."""

    root: Path
    version: str
    manifest: ReleaseManifest


def read_version(release_root: Path) -> str:
    """Read a non-empty release ``VERSION``."""
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
    """Validate every file-level and manifest invariant for a release."""
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


def _replace_directory(source: Path, target: Path) -> None:
    staging = target.parent / f"{target.name}.tmp.{os.getpid()}"
    backup = target.parent / f"{target.name}.bak.{os.getpid()}"
    remove_path(staging)
    remove_path(backup)
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
    remove_path(backup)


def _replace_symlink(link: Path, target: Path) -> None:
    temporary = link.parent / f"{link.name}.tmp.{os.getpid()}"
    remove_path(temporary)
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(link)


def install_release(source: Path, releases_root: Path, current_root: Path) -> Path:
    """Install and atomically activate one manifest-named release."""
    release = validate_release(source)
    if current_root.exists() and (not current_root.is_dir() or current_root.is_symlink()):
        raise ValueError(f"current root must be a directory: {current_root}")
    target = releases_root / release.manifest.name / release.version
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_directory(release.root, target)
    current_root.mkdir(parents=True, exist_ok=True)
    _replace_symlink(current_root / release.manifest.name, target)
    return target
