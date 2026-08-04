"""Atlas launcher, artifact runner, and command shim generation."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .catalog import command_index
from .files import remove_path


def sync_atlas_core(home: Path) -> None:
    """Copy the ``atlas_core`` package into the Atlas home."""
    src = Path(__file__).resolve().parents[1] / "atlas_core"
    dst = home / "lib/python/atlas_core"
    dst.parent.mkdir(parents=True, exist_ok=True)
    remove_path(dst)
    shutil.copytree(src, dst)


def sync_release_runner(home: Path) -> None:
    """Copy the standalone release child runner into the Atlas home."""
    source = Path(__file__).with_name("release_runner.py")
    destination = home / "lib/python/atlas_release_runner.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError(f"release runner destination must be a regular file: {destination}")
    shutil.copyfile(source, destination)


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


def ensure_artifact_runner(path: Path, atlas_bin: Path) -> None:
    """Create the shared artifact runner used by generated shims."""
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "name=\"$(basename \"$0\")\"\n"
        f"exec \"{atlas_bin}\" run \"$name\" \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def regenerate_shims(current_root: Path, shims_dir: Path, artifact_runner: Path) -> list[str]:
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
        shim.symlink_to(artifact_runner)
    return names
