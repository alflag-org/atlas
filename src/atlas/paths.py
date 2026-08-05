"""Host-side Atlas path resolution and directory creation."""

from __future__ import annotations

import os
from dataclasses import dataclass
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
    artifact_root: Path
    artifact_current: Path
    shims: Path
    bin_dir: Path
    artifact_runner: Path
    release_runner: Path
    runtime_python: Path


def get_paths() -> AtlasPaths:
    """Resolve final Atlas paths from process environment variables."""
    home = Path(os.environ.get("ATLAS_HOME", "/opt/atlas"))
    etc = Path(os.environ.get("ATLAS_ETC_DIR", "/etc/atlas"))
    var = Path(os.environ.get("ATLAS_VAR_DIR", "/var/lib/atlas"))
    runtime = Path(os.environ.get("ATLAS_RUNTIME_DIR", str(home / "runtime")))
    tmp = Path(os.environ.get("ATLAS_TMP_DIR", str(home / "tmp")))
    jobs_dir = etc / "jobs.d"
    env_dir = etc / "env"
    logs = var / "logs"
    locks = var / "locks"
    cache = var / "cache"
    artifact_root = home / "artifacts"
    artifact_current = artifact_root / "current"
    shims = home / "shims"
    bin_dir = home / "bin"
    artifact_runner = bin_dir / "artifact-runner"
    release_runner = home / "lib/python/atlas_release_runner.py"
    runtime_python = runtime / "python" / "envs" / "scripts" / "bin" / "python"
    return AtlasPaths(
        home=home,
        etc=etc,
        var=var,
        runtime=runtime,
        tmp=tmp,
        releases_root=home / "releases",
        current_root=home / "current",
        jobs_dir=jobs_dir,
        env_dir=env_dir,
        logs=logs,
        locks=locks,
        cache=cache,
        artifact_root=artifact_root,
        artifact_current=artifact_current,
        shims=shims,
        bin_dir=bin_dir,
        artifact_runner=artifact_runner,
        release_runner=release_runner,
        runtime_python=runtime_python,
    )


def ensure_dirs(paths: AtlasPaths) -> None:
    """Create writable Atlas state directories if they are missing."""
    paths.var.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)
    paths.locks.mkdir(parents=True, exist_ok=True)
    paths.cache.mkdir(parents=True, exist_ok=True)
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.tmp.mkdir(parents=True, exist_ok=True)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.artifact_root.mkdir(parents=True, exist_ok=True)
    generations = paths.artifact_root / "generations"
    if generations.is_symlink() or (generations.exists() and not generations.is_dir()):
        raise ValueError(f"artifact generations path must be a directory: {generations}")
    generations.mkdir(parents=True, exist_ok=True)
    paths.releases_root.mkdir(parents=True, exist_ok=True)
    paths.current_root.mkdir(parents=True, exist_ok=True)
