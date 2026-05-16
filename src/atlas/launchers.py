from __future__ import annotations

from pathlib import Path
import shutil
import sys

from .commands import discover_commands
from .files import remove_path


def sync_atlas_core(home: Path) -> None:
    src = Path(__file__).resolve().parents[1] / "atlas_core"
    dst = home / "lib/python/atlas_core"
    dst.parent.mkdir(parents=True, exist_ok=True)
    remove_path(dst)
    shutil.copytree(src, dst)


def ensure_atlas_launcher(path: Path) -> None:
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec \"{sys.executable}\" -m atlas.cli \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def ensure_script_runner(path: Path, atlas_bin: Path) -> None:
    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "name=\"$(basename \"$0\")\"\n"
        f"exec \"{atlas_bin}\" run \"$name\" \"$@\"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def regenerate_shims(commands_dir: Path, shims_dir: Path, script_runner: Path) -> list[str]:
    names = [entry.name for entry in discover_commands(commands_dir)]
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
