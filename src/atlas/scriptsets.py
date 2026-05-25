"""Active release and command index helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .commands import discover_commands
from .releases import read_version


RELEASE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
RESERVED_RELEASE_NAMES = {"", ".", "..", "current", "releases", "tmp"}


@dataclass(frozen=True)
class ActiveRelease:
    """A release currently activated through the current symlink tree."""

    name: str
    root: Path
    version: str


@dataclass(frozen=True)
class ReleaseCommand:
    """A command with release metadata attached."""

    name: str
    release_name: str
    release_version: str
    script_path: Path
    release_root: Path


def validate_release_name(name: str) -> str:
    """Validate and return a release name."""
    if name in RESERVED_RELEASE_NAMES:
        raise ValueError(f"invalid release name: {name}")
    if not RELEASE_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid release name: {name}")
    return name


def active_releases(current_root: Path) -> list[ActiveRelease]:
    """Return releases activated under ``current_root``."""
    if not current_root.exists():
        return []
    if not current_root.is_dir():
        raise ValueError(f"scripts current root must be a directory: {current_root}")
    releases: list[ActiveRelease] = []
    for entry in sorted(current_root.iterdir(), key=lambda item: item.name):
        if not entry.is_symlink():
            continue
        name = validate_release_name(entry.name)
        root = entry.resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"active release target not found: {entry}")
        releases.append(ActiveRelease(name=name, root=root, version=read_version(root)))
    return releases


def discover_release_commands(current_root: Path) -> list[ReleaseCommand]:
    """Discover commands across all active releases."""
    commands: list[ReleaseCommand] = []
    for release in active_releases(current_root):
        for entry in discover_commands(release.root / "commands"):
            commands.append(
                ReleaseCommand(
                    name=entry.name,
                    release_name=release.name,
                    release_version=release.version,
                    script_path=entry.script_path,
                    release_root=release.root,
                )
            )
    return commands


def build_command_index(current_root: Path) -> dict[str, ReleaseCommand]:
    """Build a collision-checked command index for active releases."""
    index: dict[str, ReleaseCommand] = {}
    for command in discover_release_commands(current_root):
        existing = index.get(command.name)
        if existing is not None:
            raise ValueError(
                f"command name collision: {command.name} found in releases: "
                f"{existing.release_name}, {command.release_name}"
            )
        index[command.name] = command
    return index
