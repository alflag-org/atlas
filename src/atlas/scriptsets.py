"""Active release and command index helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .manifests import ReleaseManifest, load_manifest, validate_name
from .releases import read_version


@dataclass(frozen=True)
class ActiveRelease:
    """A release currently activated through the current symlink tree."""

    name: str
    root: Path
    version: str
    manifest: ReleaseManifest


@dataclass(frozen=True)
class ReleaseCommand:
    """A command with release metadata attached."""

    name: str
    release_name: str
    release_version: str
    script_path: Path
    release_root: Path


def active_releases(current_root: Path) -> list[ActiveRelease]:
    """Return releases activated under ``current_root``."""
    if not current_root.exists():
        return []
    if not current_root.is_dir() or current_root.is_symlink():
        raise ValueError(f"scripts current root must be a directory: {current_root}")
    releases: list[ActiveRelease] = []
    for entry in sorted(current_root.iterdir(), key=lambda item: item.name):
        if not entry.is_symlink():
            raise ValueError(f"scripts current entry must be a symlink: {entry}")
        name = validate_name(entry.name, kind="release")
        root = entry.resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"active release target not found: {entry}")
        manifest = load_manifest(root)
        if manifest.name != name:
            raise ValueError(f"active release name mismatch: {name} != {manifest.name}")
        releases.append(
            ActiveRelease(
                name=name,
                root=root,
                version=read_version(root),
                manifest=manifest,
            )
        )
    return releases


def discover_release_commands(current_root: Path) -> list[ReleaseCommand]:
    """Discover commands across all active releases."""
    commands: list[ReleaseCommand] = []
    for release in active_releases(current_root):
        for name, artifact in release.manifest.commands.items():
            commands.append(
                ReleaseCommand(
                    name=name,
                    release_name=release.name,
                    release_version=release.version,
                    script_path=artifact.entrypoint,
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
