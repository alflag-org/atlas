from __future__ import annotations

from pathlib import Path

from .scripts import discover_commands


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
        (shims_dir / name).symlink_to(script_runner)
    return names
