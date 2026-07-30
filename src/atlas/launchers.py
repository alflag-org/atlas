"""Launcher, script runner, and shim generation."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .catalog import command_index
from .files import remove_path


def sync_atlas_core(home: Path) -> None:
    """Copy the stable ``atlas_core`` package into the Atlas home."""
    src = Path(__file__).resolve().parents[1] / "atlas_core"
    dst = home / "lib/python/atlas_core"
    dst.parent.mkdir(parents=True, exist_ok=True)
    remove_path(dst)
    shutil.copytree(src, dst)


def ensure_atlas_launcher(path: Path) -> None:
    """Create the host-side ``atlas`` launcher script."""
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec \"{sys.executable}\" -m atlas.cli \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def ensure_script_runner(path: Path, atlas_bin: Path) -> None:
    """Create the common script runner used by generated shims."""
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "name=\"$(basename \"$0\")\"\n"
        f"exec \"{atlas_bin}\" run \"$name\" \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def regenerate_shims(current_root: Path, shims_dir: Path, script_runner: Path) -> list[str]:
    """Regenerate command shims for all active release commands."""
    names = list(command_index(current_root))
    shims_dir.mkdir(parents=True, exist_ok=True)
    for item in shims_dir.iterdir():
        if item.is_dir() and not item.is_symlink():
            continue
        item.unlink()
    for name in names:
        shim = shims_dir / name
        if shim.exists() and shim.is_dir() and not shim.is_symlink():
            raise ValueError(f"shim path is a directory: {shim}")
        shim.symlink_to(script_runner)
    return names
