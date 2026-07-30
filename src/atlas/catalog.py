"""Active release and executable artifact lookup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .manifests import (
    ExecutableArtifact,
    ReleaseManifest,
    ServiceArtifact,
    load_manifest,
    validate_name,
)
from .releases import read_version


@dataclass(frozen=True)
class ActiveRelease:
    """One release activated under the scripts current directory."""

    name: str
    version: str
    root: Path
    manifest: ReleaseManifest


@dataclass(frozen=True)
class ExecutableRef:
    """An executable artifact with active release metadata."""

    release: ActiveRelease
    artifact_type: str
    artifact: ExecutableArtifact


@dataclass(frozen=True)
class ServiceRef:
    """A service artifact with active release metadata."""

    release: ActiveRelease
    service: ServiceArtifact


def active_releases(current_root: Path) -> list[ActiveRelease]:
    """Return all active releases, failing closed on malformed entries."""
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
        if not root.is_dir():
            raise ValueError(f"active release target not found: {entry}")
        manifest = load_manifest(root)
        if manifest.name != name:
            raise ValueError(f"active release name mismatch: {name} != {manifest.name}")
        releases.append(
            ActiveRelease(
                name=name,
                version=read_version(root),
                root=root,
                manifest=manifest,
            )
        )
    return releases


def release_index(current_root: Path) -> dict[str, ActiveRelease]:
    """Build an index of active releases by name."""
    return {release.name: release for release in active_releases(current_root)}


def command_index(current_root: Path) -> dict[str, ExecutableRef]:
    """Build a collision-checked public command index."""
    index: dict[str, ExecutableRef] = {}
    for release in active_releases(current_root):
        for name, artifact in release.manifest.commands.items():
            if name in index:
                previous = index[name]
                raise ValueError(
                    f"command name collision: {name} found in releases: "
                    f"{previous.release.name}, {release.name}"
                )
            index[name] = ExecutableRef(
                release=release,
                artifact_type="command",
                artifact=artifact,
            )
    return index


def resolve_command(current_root: Path, name: str) -> ExecutableRef:
    """Resolve one public command."""
    command = command_index(current_root).get(name)
    if command is None:
        raise ValueError(f"unknown command: {name}")
    return command


def resolve_job(current_root: Path, release_name: str, job_name: str) -> ExecutableRef:
    """Resolve one non-public job."""
    release = release_index(current_root).get(release_name)
    if release is None:
        raise ValueError(f"unknown release: {release_name}")
    job = release.manifest.jobs.get(job_name)
    if job is None:
        raise ValueError(f"unknown job: {release_name}/{job_name}")
    return ExecutableRef(release=release, artifact_type="job", artifact=job)


def resolve_service(
    current_root: Path,
    release_name: str,
    service_name: str,
) -> ServiceRef:
    """Resolve one service definition."""
    release = release_index(current_root).get(release_name)
    if release is None:
        raise ValueError(f"unknown release: {release_name}")
    service = release.manifest.services.get(service_name)
    if service is None:
        raise ValueError(f"unknown service: {release_name}/{service_name}")
    return ServiceRef(release=release, service=service)
