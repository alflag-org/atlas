"""Validation and installation of scripts releases."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .files import remove_path
from .manifests import ReleaseManifest, load_manifest, validate_name


@dataclass(frozen=True)
class ValidatedRelease:
    """A release directory after complete manifest validation."""

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


def _replace_release(source: Path, target: Path) -> None:
    pid = os.getpid()
    staging = target.parent / f"{target.name}.tmp.{pid}"
    backup = target.parent / f"{target.name}.bak.{pid}"
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
            backup.rename(target)
        raise
    finally:
        remove_path(staging)
        remove_path(backup)


def _replace_current_link(current_link: Path, target: Path) -> None:
    pid = os.getpid()
    tmp_link = current_link.parent / f"{current_link.name}.tmp.{pid}"
    remove_path(tmp_link)
    tmp_link.symlink_to(target, target_is_directory=True)
    tmp_link.replace(current_link)


def ensure_current_root(current_root: Path) -> None:
    """Ensure the active release root is a directory."""
    if current_root.exists() and (not current_root.is_dir() or current_root.is_symlink()):
        raise ValueError(f"scripts current root must be a directory: {current_root}")
    current_root.mkdir(parents=True, exist_ok=True)


def install_release(source: Path, releases_root: Path, current_root: Path) -> Path:
    """Install and activate one manifest-named release."""
    release = validate_release(source)
    name = validate_name(release.manifest.name, kind="release")
    target = releases_root / name / release.version
    ensure_current_root(current_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_release(release.root, target)
    _replace_current_link(current_root / name, target)
    return target
