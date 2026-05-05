from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class AtlasPaths:
    config: Path
    install: Path
    state: Path
    releases: Path
    current: Path
    libexec: Path
    shims: Path
    state_file: Path
    staging: Path
    logs: Path
    locks: Path
    cache: Path


def resolve_paths() -> AtlasPaths:
    config = Path(os.environ.get("ATLAS_ETC_DIR", "/etc/atlas"))
    install = Path(os.environ.get("ATLAS_OPT_DIR", "/opt/atlas"))
    state = Path(os.environ.get("ATLAS_VAR_DIR", "/var/lib/atlas"))

    releases = install / "releases"
    current = install / "current"
    libexec = install / "libexec"
    shims = install / "shims"

    state_file = state / "state.yml"
    staging = state / "staging"
    logs = state / "logs"
    locks = state / "locks"
    cache = state / "cache"

    return AtlasPaths(
        config,
        install,
        state,
        releases,
        current,
        libexec,
        shims,
        state_file,
        staging,
        logs,
        locks,
        cache,
    )


def ensure_dirs(paths: AtlasPaths) -> None:
    for p in [
        paths.config,
        paths.install,
        paths.state,
        paths.releases,
        paths.libexec,
        paths.shims,
        paths.staging,
        paths.logs,
        paths.locks,
        paths.cache,
    ]:
        p.mkdir(parents=True, exist_ok=True)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    if not paths.state_file.exists() or paths.state_file.stat().st_size == 0:
        paths.state_file.write_text("{}")
