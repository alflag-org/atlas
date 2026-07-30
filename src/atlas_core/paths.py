"""Path discovery for release artifacts executed by Atlas."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AtlasPaths:
    """Resolved Atlas filesystem locations visible to an artifact."""

    home: Path
    etc: Path
    var: Path
    runtime: Path
    tmp: Path
    releases_root: Path
    current_root: Path
    release_root: Path
    logs: Path
    locks: Path
    cache: Path
    config_file: Path
    host_file: Path

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation of the paths."""
        return {
            "home": str(self.home),
            "etc": str(self.etc),
            "var": str(self.var),
            "runtime": str(self.runtime),
            "tmp": str(self.tmp),
            "releases_root": str(self.releases_root),
            "current_root": str(self.current_root),
            "release_root": str(self.release_root),
            "config_file": str(self.config_file),
            "host_file": str(self.host_file),
            "logs": str(self.logs),
            "locks": str(self.locks),
            "cache": str(self.cache),
        }


def get_paths(env: Mapping[str, str] | None = None) -> AtlasPaths:
    """Resolve Atlas paths from an environment mapping.

    Args:
        env: Optional environment mapping. When omitted, ``os.environ`` is
            used.
    """
    read_env = os.environ if env is None else env
    home = Path(read_env.get("ATLAS_HOME", "/opt/atlas"))
    etc = Path(read_env.get("ATLAS_ETC_DIR", "/etc/atlas"))
    var = Path(read_env.get("ATLAS_VAR_DIR", "/var/lib/atlas"))
    runtime = Path(read_env.get("ATLAS_RUNTIME_DIR", str(home / "runtime")))
    tmp = Path(read_env.get("ATLAS_TMP_DIR", str(home / "tmp")))
    release_root = Path(_require_env(read_env, "ATLAS_RELEASE_ROOT"))
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtime=runtime,
        tmp=tmp,
        releases_root=home / "releases",
        current_root=home / "current",
        release_root=release_root,
        logs=var / "logs",
        locks=var / "locks",
        cache=var / "cache",
        config_file=etc / "config.yml",
        host_file=Path(read_env.get("ATLAS_HOST_FILE", str(etc / "host.yml"))),
    )


def _require_env(read_env: Mapping[str, str], key: str) -> str:
    value = read_env.get(key)
    if not value:
        raise RuntimeError(f"{key} is required")
    return value
