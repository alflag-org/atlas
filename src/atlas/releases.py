from __future__ import annotations

from pathlib import Path
import os
import shutil
import re
import time

from .commands import discover_commands
from .files import remove_path


RELEASE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
RESERVED_RELEASE_NAMES = {"", ".", "..", "current", "releases", "tmp"}


def read_version(release_root: Path) -> str:
    version_file = release_root / "VERSION"
    if not version_file.exists():
        raise ValueError(f"missing VERSION file: {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION is empty")
    return version


def validate_release(source: Path) -> str:
    if not source.exists() or not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    version = read_version(source)
    discover_commands(source / "commands")
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"symlink is not allowed in scripts release: {item}")
    return version


def _validate_release_name(name: str) -> str:
    if name in RESERVED_RELEASE_NAMES:
        raise ValueError(f"invalid release name: {name}")
    if not RELEASE_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid release name: {name}")
    return name


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
    if current_root.is_symlink():
        backup = current_root.parent / f"{current_root.name}.legacy.{int(time.time())}.{os.getpid()}"
        current_root.rename(backup)
    elif current_root.exists() and not current_root.is_dir():
        backup = current_root.parent / f"{current_root.name}.legacy.{int(time.time())}.{os.getpid()}"
        current_root.rename(backup)
    current_root.mkdir(parents=True, exist_ok=True)


def install_release(source: Path, releases_root: Path, current_link: Path) -> Path:
    version = validate_release(source)
    target = releases_root / version
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_release(source, target)
    current_link.parent.mkdir(parents=True, exist_ok=True)
    _replace_current_link(current_link, target)
    return target


def install_named_release(source: Path, releases_root: Path, current_root: Path, release_name: str) -> Path:
    version = validate_release(source)
    name = _validate_release_name(release_name)
    target = releases_root / name / version
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace_release(source, target)
    ensure_current_root(current_root)
    _replace_current_link(current_root / name, target)
    return target
