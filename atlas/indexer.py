from __future__ import annotations

from pathlib import Path
import json


def discover_commands(release_dir: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    packs_dir = release_dir / "packs"
    if not packs_dir.exists():
        return result
    for pack in packs_dir.iterdir():
        bindir = pack / "bin"
        if not bindir.exists():
            continue
        for cmd in bindir.iterdir():
            if cmd.is_file() and cmd.stat().st_mode & 0o111:
                result[cmd.name] = {
                    "path": str(cmd.relative_to(release_dir)),
                    "pack": pack.name,
                    "roles": [],
                    "destructive": False,
                }
    return result


def write_command_index(release_dir: Path) -> Path:
    data = discover_commands(release_dir)
    target = release_dir / "command-index.json"
    target.write_text(json.dumps(data, indent=2))
    return target
