from __future__ import annotations

from pathlib import Path

from .models import load_yaml_file


_RESERVED_NAMES = {"atlas"}
_CONFLICT_DIRS = (Path("/usr/bin"), Path("/bin"), Path("/usr/local/bin"))


def _load_active_commands(index_path: Path) -> list[str]:
    raw = load_yaml_file(index_path)
    entries = raw.get("commands", raw) if isinstance(raw, dict) else {}
    commands: list[str] = []
    for name, meta in entries.items():
        if isinstance(meta, dict) and meta.get("enabled") is False:
            continue
        commands.append(str(name))
    return commands


def _validate_shim_collisions(commands: list[str]) -> None:
    for cmd in commands:
        if cmd in _RESERVED_NAMES:
            raise ValueError(f"reserved command name cannot be shimmed: {cmd}")
        for bindir in _CONFLICT_DIRS:
            if (bindir / cmd).exists():
                raise ValueError(f"shim command conflicts with system binary: {cmd} ({bindir / cmd})")


def _ensure_single_shim_impl(libexec_dir: Path) -> Path:
    libexec_dir.mkdir(parents=True, exist_ok=True)
    shim = libexec_dir / "atlas-shim"
    shim.write_text('#!/usr/bin/env bash\nexec atlas run "$(basename "$0")" "$@"\n')
    shim.chmod(0o755)
    return shim


def generate_shims(active_dir: Path, shims_dir: Path, libexec_dir: Path) -> int:
    idx = active_dir / "command-index.yml"
    if not idx.exists():
        return 0

    commands = _load_active_commands(idx)
    _validate_shim_collisions(commands)
    shim_impl = _ensure_single_shim_impl(libexec_dir)

    if shims_dir.exists():
        for existing in shims_dir.iterdir():
            if existing.is_file() and not existing.is_symlink():
                existing.unlink()

    shims_dir.mkdir(parents=True, exist_ok=True)
    for cmd in commands:
        shim = shims_dir / cmd
        if shim.is_symlink() or shim.exists():
            shim.unlink()
        shim.symlink_to(shim_impl)
    return len(commands)
