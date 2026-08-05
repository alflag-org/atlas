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
    shims: Path
    bin_dir: Path
    artifact_runner: Path
    release_runner: Path
    process_supervisor: Path
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
    shims = home / "shims"
    bin_dir = home / "bin"
    artifact_runner = bin_dir / "artifact-runner"
    release_runner = home / "lib/python/atlas_release_runner.py"
    process_supervisor = home / "lib/python/atlas_process_supervisor.py"
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
        shims=shims,
        bin_dir=bin_dir,
        artifact_runner=artifact_runner,
        release_runner=release_runner,
        process_supervisor=process_supervisor,
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
    paths.shims.mkdir(parents=True, exist_ok=True)
    paths.releases_root.mkdir(parents=True, exist_ok=True)
    paths.current_root.mkdir(parents=True, exist_ok=True)
