"""Active release and executable artifact lookup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .manifests import (
    ExecutableArtifact,
    ReleaseManifest,
    ServiceArtifact,
    load_manifest,
    validate_name,
)
from .releases import read_version, release_digest


@dataclass(frozen=True)
class ActiveRelease:
    """One release activated under the current release root."""

    name: str
    version: str
    root: Path
    manifest: ReleaseManifest
    content_digest: str = ""


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


def active_releases(current_root: Path, releases_root: Path) -> list[ActiveRelease]:
    """Return all active releases, failing closed on malformed entries."""
    if not current_root.exists():
        return []
    if not current_root.is_dir() or current_root.is_symlink():
        raise ValueError(f"current root must be a directory: {current_root}")
    if releases_root.is_symlink() or not releases_root.is_dir():
        raise ValueError(f"releases root must be a directory: {releases_root}")
    resolved_releases = releases_root.resolve()
    releases: list[ActiveRelease] = []
    for entry in sorted(current_root.iterdir(), key=lambda item: item.name):
        if not entry.is_symlink():
            raise ValueError(f"current entry must be a symlink: {entry}")
        name = validate_name(entry.name, kind="release")
        raw_link = os.readlink(entry)
        raw_target = Path(raw_link)
        if any(part == ".." for part in raw_link.split("/")):
            raise ValueError(f"current entry contains path traversal: {entry}")
        raw_path = entry.parent / raw_target
        if raw_path.is_symlink():
            raise ValueError(f"current entry uses a symlink chain: {entry}")
        direct_target = raw_path.resolve()
        root = direct_target
        if not root.is_dir():
            raise ValueError(f"active release target not found: {entry}")
        expected_parent = resolved_releases / name
        if root.parent != expected_parent:
            raise ValueError(f"active release target is outside releases root: {entry}")
        manifest = load_manifest(root)
        if manifest.name != name:
            raise ValueError(f"active release name mismatch: {name} != {manifest.name}")
        version = read_version(root)
        digest = release_digest(root)
        if root.name != f"{version}-{digest}":
            raise ValueError(f"active release snapshot name mismatch: {entry}")
        releases.append(
            ActiveRelease(
                name=name,
                version=version,
                root=root,
                manifest=manifest,
                content_digest=digest,
            )
        )
    return releases


def release_index(current_root: Path, releases_root: Path) -> dict[str, ActiveRelease]:
    """Build an index of active releases by name."""
    return {
        release.name: release
        for release in active_releases(current_root, releases_root)
    }


def command_index(current_root: Path, releases_root: Path) -> dict[str, ExecutableRef]:
    """Build a collision-checked public command index."""
    index: dict[str, ExecutableRef] = {}
    for release in active_releases(current_root, releases_root):
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


def resolve_command(current_root: Path, releases_root: Path, name: str) -> ExecutableRef:
    """Resolve one public command."""
    command = command_index(current_root, releases_root).get(name)
    if command is None:
        raise ValueError(f"unknown command: {name}")
    return command


def resolve_command_from_release(release: ActiveRelease, name: str) -> ExecutableRef:
    """Resolve one command directly from an already selected release snapshot."""
    command = release.manifest.commands.get(name)
    if command is None:
        raise ValueError(f"unknown command: {name}")
    return ExecutableRef(release=release, artifact_type="command", artifact=command)


def resolve_job(
    current_root: Path,
    releases_root: Path,
    release_name: str,
    job_name: str,
) -> ExecutableRef:
    """Resolve one non-public job."""
    release = release_index(current_root, releases_root).get(release_name)
    if release is None:
        raise ValueError(f"unknown release: {release_name}")
    job = release.manifest.jobs.get(job_name)
    if job is None:
        raise ValueError(f"unknown job: {release_name}/{job_name}")
    return ExecutableRef(release=release, artifact_type="job", artifact=job)


def resolve_job_from_release(release: ActiveRelease, job_name: str) -> ExecutableRef:
    """Resolve one job directly from an already selected release snapshot."""
    job = release.manifest.jobs.get(job_name)
    if job is None:
        raise ValueError(f"unknown job: {release.name}/{job_name}")
    return ExecutableRef(release=release, artifact_type="job", artifact=job)


def release_from_snapshot(
    root: Path,
    releases_root: Path,
    *,
    expected_name: str,
    expected_version: str,
    expected_digest: str,
) -> ActiveRelease:
    """Load and verify one immutable release snapshot without using current links."""
    if not root.is_absolute() or not releases_root.is_absolute():
        raise ValueError("selected release paths must be absolute")
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"selected release snapshot is not a directory: {root}")
    if releases_root.is_symlink() or not releases_root.is_dir():
        raise ValueError(f"releases root must be a directory: {releases_root}")
    resolved_root = root.resolve()
    resolved_releases = releases_root.resolve()
    validate_name(expected_name, kind="release")
    if resolved_root.parent != resolved_releases / expected_name:
        raise ValueError("selected release snapshot is outside its release directory")
    manifest = load_manifest(resolved_root)
    version = read_version(resolved_root)
    digest = release_digest(resolved_root)
    if manifest.name != expected_name:
        raise ValueError("selected release name does not match its manifest")
    if version != expected_version or digest != expected_digest:
        raise ValueError("selected release identity changed")
    if resolved_root.name != f"{version}-{digest}":
        raise ValueError("selected release snapshot name does not match its identity")
    return ActiveRelease(
        name=expected_name,
        version=version,
        root=resolved_root,
        manifest=manifest,
        content_digest=digest,
    )


def resolve_service(
    current_root: Path,
    releases_root: Path,
    release_name: str,
    service_name: str,
) -> ServiceRef:
    """Resolve one service definition."""
    release = release_index(current_root, releases_root).get(release_name)
    if release is None:
        raise ValueError(f"unknown release: {release_name}")
    service = release.manifest.services.get(service_name)
    if service is None:
        raise ValueError(f"unknown service: {release_name}/{service_name}")
    return ServiceRef(release=release, service=service)
