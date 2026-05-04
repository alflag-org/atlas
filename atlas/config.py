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


def load_compat_config(etc: Path, stem: str) -> tuple[dict, Path | None]:
    """Load config with migration-period compatibility order (YAML first, JSON fallback)."""
    yml = etc / f"{stem}.yml"
    if yml.exists():
        from .models import parse_yaml_like

        return parse_yaml_like(yml.read_text()), yml
    json_path = etc / f"{stem}.json"
    if json_path.exists():
        import json

        return json.loads(json_path.read_text()), json_path
    return {}, None


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

    return AtlasPaths(config, install, state, releases, current, libexec, shims, state_file, staging, logs, locks, cache)


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
