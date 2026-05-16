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
    scripts_releases_root: Path
    scripts_current_root: Path
    scripts: Path
    logs: Path
    cache: Path
    shims: Path
    bin_dir: Path
    script_runner: Path
    scripts_python: Path


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
    scripts_releases_root = scripts_root / "releases"
    scripts = scripts_current_root
    logs = var / "logs"
    cache = var / "cache"
    shims = home / "shims"
    bin_dir = home / "bin"
    script_runner = bin_dir / "script-runner"
    scripts_python = runtime / "python" / "envs" / "scripts" / "bin" / "python"
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtime=runtime,
        scripts_root=scripts_root,
        scripts_releases_root=scripts_releases_root,
        scripts_current_root=scripts_current_root,
        scripts=scripts,
        logs=logs,
        cache=cache,
        shims=shims,
        bin_dir=bin_dir,
        script_runner=script_runner,
        scripts_python=scripts_python,
    )


def ensure_dirs(paths: AtlasPaths) -> None:
    paths.var.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    paths.cache.mkdir(parents=True, exist_ok=True)
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.shims.mkdir(parents=True, exist_ok=True)
    paths.scripts_releases_root.mkdir(parents=True, exist_ok=True)
