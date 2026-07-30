"""Host-side Atlas path resolution and directory creation."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class AtlasPaths:
    """Host-side filesystem locations used by Atlas."""

    home: Path
    etc: Path
    var: Path
    runtime: Path
    tmp: Path
    releases_root: Path
    current_root: Path
    jobs_dir: Path
    env_dir: Path
    logs: Path
    locks: Path
    cache: Path
    shims: Path
    bin_dir: Path
    artifact_runner: Path
    runtime_python: Path


def get_paths() -> AtlasPaths:
    """Resolve final Atlas paths from process environment variables."""
    home = Path(os.environ.get("ATLAS_HOME", "/opt/atlas"))
    etc = Path(os.environ.get("ATLAS_ETC_DIR", "/etc/atlas"))
    var = Path(os.environ.get("ATLAS_VAR_DIR", "/var/lib/atlas"))
    runtime = Path(os.environ.get("ATLAS_RUNTIME_DIR", str(home / "runtime")))
    tmp = Path(os.environ.get("ATLAS_TMP_DIR", str(home / "tmp")))
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtime=runtime,
        tmp=tmp,
        releases_root=home / "releases",
        current_root=home / "current",
        jobs_dir=etc / "jobs.d",
        env_dir=etc / "env",
        logs=var / "logs",
        locks=var / "locks",
        cache=var / "cache",
        shims=home / "shims",
        bin_dir=home / "bin",
        artifact_runner=home / "bin/artifact-runner",
        runtime_python=runtime / "python/envs/scripts/bin/python",
    )


def ensure_dirs(paths: AtlasPaths) -> None:
    """Create Atlas-owned writable directories."""
    for path in (
        paths.home,
        paths.tmp,
        paths.releases_root,
        paths.current_root,
        paths.bin_dir,
        paths.shims,
        paths.var,
        paths.logs,
        paths.locks,
        paths.cache,
    ):
        path.mkdir(parents=True, exist_ok=True)
