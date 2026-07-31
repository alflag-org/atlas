"""Path discovery for scripts executed by Atlas."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AtlasPaths:
    """Resolved Atlas filesystem locations visible to a script."""

    home: Path
    etc: Path
    var: Path
    runtime: Path
    tmp: Path
    scripts_root: Path
    scripts_current_root: Path
    script_release_root: Path
    logs: Path
    cache: Path
    config_file: Path
    host_file: Path

    @property
    def scripts(self) -> Path:
        """Backward-compatible alias for ``script_release_root``."""
        return self.script_release_root

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation of the paths."""
        return {
            "home": str(self.home),
            "etc": str(self.etc),
            "var": str(self.var),
            "runtime": str(self.runtime),
            "tmp": str(self.tmp),
            "scripts_root": str(self.scripts_root),
            "scripts_current_root": str(self.scripts_current_root),
            "script_release_root": str(self.script_release_root),
            "scripts": str(self.scripts),
            "config_file": str(self.config_file),
            "host_file": str(self.host_file),
            "logs": str(self.logs),
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
    scripts_root = home / "scripts"
    scripts_current_root = Path(read_env.get("ATLAS_SCRIPTS_CURRENT_DIR", str(scripts_root / "current")))
    script_release_root = Path(read_env.get("ATLAS_SCRIPTS_DIR", str(scripts_current_root)))
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtime=runtime,
        tmp=tmp,
        scripts_root=scripts_root,
        scripts_current_root=scripts_current_root,
        script_release_root=script_release_root,
        logs=var / "logs",
        cache=var / "cache",
        config_file=etc / "config.yml",
        host_file=Path(read_env.get("ATLAS_HOST_FILE", str(etc / "host.yml"))),
    )
