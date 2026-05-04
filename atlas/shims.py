from __future__ import annotations

from pathlib import Path
import json


def generate_shims(active_dir: Path, shims_dir: Path) -> int:
    idx = active_dir / "command-index.json"
    if not idx.exists():
        return 0
    commands: dict[str, str] = json.loads(idx.read_text())
    shims_dir.mkdir(parents=True, exist_ok=True)
    for cmd in commands:
        shim = shims_dir / cmd
        shim.write_text(f"#!/usr/bin/env bash\nexec atlas run {cmd} \"$@\"\n")
        shim.chmod(0o755)
    return len(commands)
