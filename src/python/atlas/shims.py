"""Generated command shims."""

from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

from .catalog import CommandRef, command_index
from .config import AtlasConfig
from .paths import AtlasPaths

_MARKER = "# atlas-shim: generated"


def _write_shim(path: Path, command: CommandRef) -> None:
    content = "\n".join(
        (
            "#!/bin/sh",
            _MARKER,
            "set -eu",
            "exec "
            + shlex.quote(sys.executable)
            + " -m atlas.cli run "
            + shlex.quote(command.name)
            + ' "$@"',
            "",
        )
    )
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    os.replace(temporary, path)


def generate_shims(paths: AtlasPaths, config: AtlasConfig) -> list[str]:
    """Generate one stable shim per discovered command."""
    if paths.shims.is_symlink() or (paths.shims.exists() and not paths.shims.is_dir()):
        raise ValueError(f"shims path must be a directory: {paths.shims}")
    paths.shims.mkdir(parents=True, exist_ok=True)
    commands = command_index(config)
    for name, command in commands.items():
        target = paths.shims / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"shim path must be a regular file: {target}")
        if target.exists() and _MARKER not in target.read_text(encoding="utf-8"):
            raise ValueError(f"refusing to replace non-Atlas shim: {target}")
        _write_shim(target, command)
    for path in paths.shims.iterdir():
        if path.is_file() and not path.is_symlink() and _MARKER in path.read_text(encoding="utf-8"):
            if path.name not in commands:
                path.unlink()
    return sorted(commands)
