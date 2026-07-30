"""Stable runtime context for release artifacts."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .host import HostProfile, get_host
from .paths import AtlasPaths, get_paths


@dataclass(frozen=True)
class ArtifactInfo:
    """Metadata for the currently executing command or job."""

    name: str
    artifact_type: str
    release_name: str
    version: str
    release_root: Path
    run_id: str
    parent_run_id: str | None
    operation_id: str

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable artifact representation."""
        return {
            "name": self.name,
            "artifact_type": self.artifact_type,
            "release_name": self.release_name,
            "version": self.version,
            "release_root": str(self.release_root),
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class AtlasContext:
    """Host, path, and active artifact context."""

    host: HostProfile
    paths: AtlasPaths
    artifact: ArtifactInfo

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation of the context."""
        return {
            "host": self.host.to_dict(),
            "paths": self.paths.to_dict(),
            "artifact": self.artifact.to_dict(),
        }


def _require_env(read_env: Mapping[str, str], key: str) -> str:
    value = read_env.get(key)
    if not value:
        raise RuntimeError(f"{key} is required")
    return value


def get_context(env: Mapping[str, str] | None = None) -> AtlasContext:
    """Build the stable context from the execution environment."""
    read_env = os.environ if env is None else env
    paths = get_paths(env=read_env)
    parent_run_id = read_env.get("ATLAS_PARENT_RUN_ID") or None
    return AtlasContext(
        host=get_host(paths.host_file),
        paths=paths,
        artifact=ArtifactInfo(
            name=_require_env(read_env, "ATLAS_ARTIFACT_NAME"),
            artifact_type=_require_env(read_env, "ATLAS_ARTIFACT_TYPE"),
            release_name=_require_env(read_env, "ATLAS_RELEASE_NAME"),
            version=_require_env(read_env, "ATLAS_RELEASE_VERSION"),
            release_root=Path(_require_env(read_env, "ATLAS_RELEASE_ROOT")),
            run_id=_require_env(read_env, "ATLAS_RUN_ID"),
            parent_run_id=parent_run_id,
            operation_id=_require_env(read_env, "ATLAS_OPERATION_ID"),
        ),
    )
