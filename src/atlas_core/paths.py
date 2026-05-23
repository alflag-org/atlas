from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class AtlasPaths:
    home: Path
    etc: Path
    var: Path
    runtime: Path
    scripts_root: Path
    scripts_current_root: Path
    script_release_root: Path
    logs: Path
    cache: Path

    @property
    def scripts(self) -> Path:
        return self.script_release_root

    @property
    def config_file(self) -> Path:
        return self.etc / "config.yml"

    @property
    def host_file(self) -> Path:
        return Path(os.environ.get("ATLAS_HOST_FILE", str(self.etc / "host.yml")))

    def to_dict(self) -> dict[str, str]:
        return {
            "home": str(self.home),
            "etc": str(self.etc),
            "var": str(self.var),
            "runtime": str(self.runtime),
            "scripts_root": str(self.scripts_root),
            "scripts_current_root": str(self.scripts_current_root),
            "script_release_root": str(self.script_release_root),
            "scripts": str(self.scripts),
            "config_file": str(self.config_file),
            "host_file": str(self.host_file),
            "logs": str(self.logs),
            "cache": str(self.cache),
        }


def get_paths() -> AtlasPaths:
    home = Path(os.environ.get("ATLAS_HOME", "/opt/atlas"))
    etc = Path(os.environ.get("ATLAS_ETC_DIR", "/etc/atlas"))
    var = Path(os.environ.get("ATLAS_VAR_DIR", "/var/lib/atlas"))
    runtime = Path(os.environ.get("ATLAS_RUNTIME_DIR", str(home / "runtime")))
    scripts_root = home / "scripts"
    scripts_current_root = Path(
        os.environ.get(
            "ATLAS_SCRIPTS_CURRENT_DIR",
            os.environ.get("ATLAS_SCRIPTS_DIR", str(scripts_root / "current")),
        )
    )
    script_release_root = Path(os.environ.get("ATLAS_SCRIPTS_DIR", str(scripts_current_root)))
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtime=runtime,
        scripts_root=scripts_root,
        scripts_current_root=scripts_current_root,
        script_release_root=script_release_root,
        logs=var / "logs",
        cache=var / "cache",
    )
