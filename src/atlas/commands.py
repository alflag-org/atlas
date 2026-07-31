"""Command discovery and naming rules for scripts releases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
RESERVED = {"atlas", "script-runner"}


@dataclass(frozen=True)
class CommandEntry:
    """A command discovered from a Python file in a release."""

    name: str
    script_path: Path


def _validate_segment(segment: str) -> None:
    if not NAME_RE.fullmatch(segment):
        raise ValueError(f"invalid command segment: {segment}")


def command_name_from_relative_path(relative_python_file: Path) -> str:
    """Convert a command file path under ``commands/`` into a CLI name."""
    parts = list(relative_python_file.parts)
    stem = Path(parts[-1]).stem
    segments = [*parts[:-1], stem]
    for segment in segments:
        _validate_segment(segment)
    name = "-".join(segments)
    if "--" in name or name.endswith("-"):
        raise ValueError(f"invalid command name: {name}")
    if name in RESERVED:
        raise ValueError(f"reserved command name: {name}")
    return name


def discover_commands(commands_dir: Path) -> list[CommandEntry]:
    """Discover and validate Python command files in ``commands_dir``."""
    if not commands_dir.exists() or not commands_dir.is_dir():
        raise ValueError(f"commands directory not found: {commands_dir}")

    root = commands_dir.resolve()
    seen: dict[str, Path] = {}
    entries: list[CommandEntry] = []
    for py_file in sorted(commands_dir.rglob("*.py")):
        if py_file.is_symlink():
            raise ValueError(f"symlink is not allowed: {py_file}")
        resolved = py_file.resolve()
        if root not in resolved.parents:
            raise ValueError(f"path traversal detected: {py_file}")
        relative_path = resolved.relative_to(root)
        name = command_name_from_relative_path(relative_path)
        if name in seen:
            raise ValueError(f"command name conflict: {seen[name]} vs {py_file}")
        seen[name] = py_file
        entries.append(CommandEntry(name=name, script_path=resolved))
    return entries
