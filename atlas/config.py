from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class AtlasPaths:
    root: Path
    etc: Path
    state: Path
    releases: Path
    active: Path
    staged: Path
    shims: Path
    locks: Path
    logs: Path


def resolve_paths() -> AtlasPaths:
    root = Path(os.environ.get("ATLAS_ROOT", "/opt/atlas"))
    etc = Path(os.environ.get("ATLAS_ETC", "/etc/atlas"))
    state = root / "state"
    releases = root / "releases"
    active = root / "active"
    staged = root / "staged"
    shims = root / "shims"
    locks = root / "locks"
    logs = root / "logs"
    return AtlasPaths(root, etc, state, releases, active, staged, shims, locks, logs)


def ensure_dirs(paths: AtlasPaths) -> None:
    for p in [paths.root, paths.etc, paths.state, paths.releases, paths.shims, paths.locks, paths.logs]:
        p.mkdir(parents=True, exist_ok=True)
