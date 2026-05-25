"""Runtime context helpers for scripts executed by Atlas."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from .host import HostProfile, get_host
from .paths import AtlasPaths, get_paths


@dataclass(frozen=True)
class ScriptInfo:
    """Metadata for the currently executing script command."""

    name: str
    release_name: str
    version: str
    release_root: Path

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation of the script metadata."""
        return {
            "name": self.name,
            "release_name": self.release_name,
            "version": self.version,
            "release_root": str(self.release_root),
        }


@dataclass(frozen=True)
class AtlasContext:
    """Complete script context assembled from Atlas environment variables."""

    host: HostProfile
    paths: AtlasPaths
    script: ScriptInfo

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation of the context."""
        return {
            "host": self.host.to_dict(),
            "paths": self.paths.to_dict(),
            "script": self.script.to_dict(),
        }


def _require_env(read_env: Mapping[str, str], key: str) -> str:
    value = read_env.get(key)
    if not value:
        raise RuntimeError(f"{key} is required")
    return value


def get_context(env: Mapping[str, str] | None = None) -> AtlasContext:
    """Load the current Atlas script context.

    Args:
        env: Optional environment mapping. When omitted, ``os.environ`` is
            used. Tests can pass a mapping explicitly.

    Raises:
        RuntimeError: If required Atlas runtime environment variables are
            missing or empty.
        FileNotFoundError: If the resolved host profile file does not exist.
        ValueError: If the host profile file is invalid.
    """
    read_env = os.environ if env is None else env
    script_name = _require_env(read_env, "ATLAS_SCRIPT_NAME")
    script_release_name = _require_env(read_env, "ATLAS_SCRIPT_RELEASE_NAME")
    script_release_root = Path(_require_env(read_env, "ATLAS_SCRIPTS_DIR"))
    script_version = read_env.get("ATLAS_SCRIPT_VERSION", "")
    paths = get_paths(env=read_env)
    return AtlasContext(
        host=get_host(paths.host_file),
        paths=paths,
        script=ScriptInfo(
            name=script_name,
            release_name=script_release_name,
            version=script_version,
            release_root=script_release_root,
        ),
    )
