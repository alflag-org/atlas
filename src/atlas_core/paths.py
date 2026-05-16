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
    scripts: Path
    logs: Path
    cache: Path


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
    scripts = Path(os.environ.get("ATLAS_SCRIPTS_DIR", str(scripts_current_root)))
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtime=runtime,
        scripts_root=scripts_root,
        scripts_current_root=scripts_current_root,
        scripts=scripts,
        logs=var / "logs",
        cache=var / "cache",
    )
