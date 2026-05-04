from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil


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


LEGACY_ROOT = Path("/opt/atlas")
DEFAULT_STATE_ROOT = Path("/var/lib/atlas")


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
    root = Path(os.environ.get("ATLAS_ROOT", str(DEFAULT_STATE_ROOT)))
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


def plan_layout_migration(paths: AtlasPaths, legacy_root: Path = LEGACY_ROOT) -> list[tuple[Path, Path, str]]:
    mapping = {
        legacy_root / "state": paths.state,
        legacy_root / "logs": paths.logs,
        legacy_root / "locks": paths.locks,
    }
    planned: list[tuple[Path, Path, str]] = []
    for src, dst in mapping.items():
        if src.exists() and src.resolve() != dst.resolve():
            if dst.exists() and any(dst.iterdir()):
                action = "skip (destination exists)"
            else:
                action = "move"
            planned.append((src, dst, action))
    return planned


def execute_layout_migration(paths: AtlasPaths, legacy_root: Path = LEGACY_ROOT) -> list[tuple[Path, Path, str]]:
    planned = plan_layout_migration(paths, legacy_root=legacy_root)
    results: list[tuple[Path, Path, str]] = []
    for src, dst, action in planned:
        if action != "move":
            results.append((src, dst, action))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.is_dir() and not any(dst.iterdir()):
            dst.rmdir()
        shutil.move(str(src), str(dst))
        results.append((src, dst, "moved"))
    return results
