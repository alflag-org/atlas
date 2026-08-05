"""Validation, content-addressed installation, and activation of Atlas releases."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .files import remove_path
from .locks import acquire_lock
from .manifests import ReleaseManifest, load_manifest


@dataclass(frozen=True)
class ValidatedRelease:
    """A release directory after complete validation."""

    root: Path
    version: str
    manifest: ReleaseManifest
    content_digest: str


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


def release_digest(release_root: Path) -> str:
    """Hash every regular file and relative path in one release tree."""
    if release_root.is_symlink() or not release_root.is_dir():
        raise ValueError(f"release directory not found: {release_root}")
    root = release_root.resolve()
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        if item.is_symlink():
            raise ValueError(f"symlink is not allowed in release: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise ValueError(f"release entry must be a regular file: {item}")
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        with item.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def validate_release(source: Path) -> ValidatedRelease:
    """Validate a release directory and its explicit command manifest."""
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"release directory not found: {source}")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"symlink is not allowed in release: {item}")
    root = source.resolve()
    return ValidatedRelease(
        root=root,
        version=read_version(root),
        manifest=load_manifest(root),
        content_digest=release_digest(root),
    )


def _snapshot_name(release: ValidatedRelease) -> str:
    return f"{release.version}-{release.content_digest}"


def _replace_symlink(current_link: Path, target: Path) -> None:
    tmp_link = current_link.parent / f".{current_link.name}.tmp.{uuid4().hex}"
    remove_path(tmp_link)
    try:
        tmp_link.symlink_to(target, target_is_directory=True)
        tmp_link.replace(current_link)
    finally:
        remove_path(tmp_link)


def _current_target(link: Path, releases_root: Path) -> Path | None:
    if not link.exists() and not link.is_symlink():
        return None
    if not link.is_symlink():
        raise ValueError(f"current entry must be a symlink: {link}")
    raw_link = os.readlink(link)
    raw_target = Path(raw_link)
    if any(part == ".." for part in raw_link.split("/")):
        raise ValueError(f"current entry contains path traversal: {link}")
    raw_path = link.parent / raw_target
    if raw_path.is_symlink():
        raise ValueError(f"current entry uses a symlink chain: {link}")
    target = raw_path.resolve()
    resolved_releases = releases_root.resolve()
    if resolved_releases not in target.parents:
        raise ValueError(f"current entry escapes releases root: {link}")
    if not target.is_dir():
        raise ValueError(f"active release target not found: {link}")
    active = validate_release(target)
    expected_parent = resolved_releases / active.manifest.name
    if target.parent != expected_parent or target.name != _snapshot_name(active):
        raise ValueError(f"current entry is not a validated release snapshot: {link}")
    return target


def _make_tree_read_only(root: Path) -> None:
    """Remove write bits from every snapshot entry, including the root."""
    entries = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for item in [*entries, root]:
        if item.is_symlink():
            raise ValueError(f"symlink is not allowed in release: {item}")
        mode = stat.S_IMODE(item.stat().st_mode)
        item.chmod(mode & ~0o222)


def _make_tree_writable(root: Path) -> None:
    """Restore transient staging write bits before cleanup after a failed copy."""
    entries = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for item in [*entries, root]:
        if item.is_symlink():
            continue
        mode = stat.S_IMODE(item.stat().st_mode)
        item.chmod(mode | 0o200)


def _stage_snapshot(
    source: ValidatedRelease,
    target: Path,
) -> None:
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError(f"release snapshot must be a directory: {target}")
    if target.exists():
        existing = validate_release(target)
        if existing.content_digest != source.content_digest:
            raise ValueError(f"release snapshot digest mismatch: {target}")
        _make_tree_read_only(target)
        return

    staging = target.parent / f".{target.name}.tmp.{uuid4().hex}"
    remove_path(staging)
    try:
        shutil.copytree(source.root, staging)
        staged = validate_release(staging)
        if (
            staged.version != source.version
            or staged.manifest.name != source.manifest.name
            or staged.content_digest != source.content_digest
        ):
            raise ValueError(f"staged release content changed: {source.manifest.name}")
        _make_tree_read_only(staging)
        verified = validate_release(staging)
        if verified.content_digest != source.content_digest:
            raise ValueError(f"staged release content changed: {source.manifest.name}")
        staging.rename(target)
    finally:
        if staging.exists():
            _make_tree_writable(staging)
        remove_path(staging)


@contextmanager
def reversible_release_install(
    source: Path,
    releases_root: Path,
    current_root: Path,
) -> Iterator[Path]:
    """Activate one never-replaced snapshot under a per-release transaction lock."""
    release = validate_release(source)
    if releases_root.is_symlink() or (
        releases_root.exists() and not releases_root.is_dir()
    ):
        raise ValueError(f"releases root must be a directory: {releases_root}")
    releases_root.mkdir(parents=True, exist_ok=True)
    lock_root = releases_root / ".locks"
    with acquire_lock(lock_root, release.manifest.name, wait=True):
        if current_root.exists() and (
            not current_root.is_dir() or current_root.is_symlink()
        ):
            raise ValueError(f"current root must be a directory: {current_root}")
        target = releases_root / release.manifest.name / _snapshot_name(release)
        link = current_root / release.manifest.name
        if target.parent.is_symlink():
            raise ValueError(f"release directory must not be a symlink: {target.parent}")
        previous_target = _current_target(link, releases_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target_existed = target.exists()
        _stage_snapshot(release, target)
        activated = False
        try:
            current_root.mkdir(parents=True, exist_ok=True)
            _replace_symlink(link, target)
            activated = True
            yield target
        except BaseException:
            try:
                current_target = _current_target(link, releases_root)
                if current_target == target:
                    remove_path(link)
                    if previous_target is not None:
                        _replace_symlink(link, previous_target)
                    if not target_existed:
                        _make_tree_writable(target)
                        remove_path(target)
                elif not activated and not target_existed:
                    _make_tree_writable(target)
                    remove_path(target)
            except Exception as rollback_error:
                recovery_path = previous_target or target
                raise RuntimeError(
                    "release installation failed and rollback failed or activation "
                    f"changed; recovery path: {recovery_path}"
                ) from rollback_error
            raise


def install_release(source: Path, releases_root: Path, current_root: Path) -> Path:
    """Install and atomically activate one content-addressed snapshot."""
    with reversible_release_install(source, releases_root, current_root) as target:
        return target
