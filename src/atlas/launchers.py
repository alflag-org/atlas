"""Atlas launcher, artifact runner, and command shim generation."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

from .catalog import command_index
from .files import remove_path


def sync_atlas_core(home: Path) -> None:
    """Copy the stable release-facing API into Atlas home."""
    source = Path(__file__).resolve().parents[1] / "atlas_core"
    destination = home / "lib/python/atlas_core"
    destination.parent.mkdir(parents=True, exist_ok=True)
    remove_path(destination)
    shutil.copytree(source, destination)


def ensure_atlas_launcher(path: Path) -> None:
    """Create the stable host-side ``atlas`` executable."""
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec \"{sys.executable}\" -m atlas.cli \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def ensure_artifact_runner(path: Path, atlas_bin: Path) -> None:
    """Create the common command runner targeted by every shim."""
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "name=\"$(basename \"$0\")\"\n"
        f"exec \"{atlas_bin}\" run \"$name\" \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def validate_shim_destinations(names: list[str], shims_dir: Path) -> None:
    """Reject public command destinations that Atlas cannot replace."""
    if shims_dir.is_symlink() or (shims_dir.exists() and not shims_dir.is_dir()):
        raise ValueError(f"shims path must be a directory: {shims_dir}")
    for name in names:
        shim = shims_dir / name
        if shim.exists() and shim.is_dir() and not shim.is_symlink():
            raise ValueError(f"shim path is a directory: {shim}")


def regenerate_shims(current_root: Path, shims_dir: Path, artifact_runner: Path) -> list[str]:
    """Replace public command shims; jobs are intentionally excluded."""
    names = list(command_index(current_root))
    validate_shim_destinations(names, shims_dir)
    shims_dir.mkdir(parents=True, exist_ok=True)
    for item in shims_dir.iterdir():
        if item.is_dir() and not item.is_symlink():
            continue
        item.unlink()
    for name in names:
        shim = shims_dir / name
        shim.symlink_to(artifact_runner)
    return names
