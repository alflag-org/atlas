"""Program registration and public command discovery."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import AtlasConfig, ProgramConfig, validate_name


@dataclass(frozen=True)
class CommandRef:
    """A command discovered from one registered program."""

    name: str
    program: ProgramConfig
    path: Path
    type: str

    @property
    def relative_path(self) -> str:
        """Return the command path relative to its program root."""
        return self.path.relative_to(self.program.root).as_posix()


def _command_name(relative: Path, *, python: bool) -> str:
    parts = list(relative.parts)
    if python:
        parts[-1] = Path(parts[-1]).stem
    for part in parts:
        validate_name(part, kind="command segment")
    name = "-".join(parts)
    validate_name(name, kind="command")
    return name


def _walk_files(directory: Path) -> list[Path]:
    if directory.is_symlink():
        raise ValueError(f"commands directory must not be a symlink: {directory}")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError(f"commands directory must be a directory: {directory}")
    files: list[Path] = []
    for root, directories, names in os.walk(directory, followlinks=False):
        root_path = Path(root)
        for name in sorted(directories):
            path = root_path / name
            if path.is_symlink():
                raise ValueError(f"symlink is not allowed: {path}")
        for name in sorted(names):
            path = root_path / name
            if path.is_symlink():
                raise ValueError(f"symlink is not allowed: {path}")
            if not path.is_file():
                raise ValueError(f"command entry is not a regular file: {path}")
            files.append(path)
    return files


def discover_commands(program: ProgramConfig) -> list[CommandRef]:
    """Discover Python commands and native executables for one program."""
    root = program.root
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"program root not found: {root}")
    directories: list[tuple[Path, bool]] = []
    commands_dir = root / "commands"
    if commands_dir.exists() or commands_dir.is_symlink():
        directories.append((commands_dir, True))
    if program.runtime.type == "native":
        bin_dir = root / "bin"
        if bin_dir.exists() or bin_dir.is_symlink():
            directories.append((bin_dir, False))

    found: dict[str, CommandRef] = {}
    for directory, python_directory in directories:
        for path in _walk_files(directory):
            is_python = (
                python_directory
                and program.runtime.type == "python"
                and path.suffix == ".py"
            )
            if not is_python and not (path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
                continue
            relative = path.relative_to(directory)
            name = _command_name(relative, python=is_python)
            if name in found:
                raise ValueError(
                    f"command name collision in program {program.name}: {name}"
                )
            found[name] = CommandRef(
                name=name,
                program=program,
                path=path,
                type="python" if is_python else "native",
            )
    return [found[name] for name in sorted(found)]


def command_index(config: AtlasConfig) -> dict[str, CommandRef]:
    """Build a collision-checked index of all registered commands."""
    index: dict[str, CommandRef] = {}
    for program in config.programs.values():
        for command in discover_commands(program):
            previous = index.get(command.name)
            if previous is not None:
                raise ValueError(
                    f"command name collision: {command.name} found in programs: "
                    f"{previous.program.name}, {command.program.name}"
                )
            index[command.name] = command
    return index


def resolve_command(config: AtlasConfig, name: str) -> CommandRef:
    """Resolve one public command from the current local program set."""
    command = command_index(config).get(name)
    if command is None:
        raise ValueError(f"unknown command: {name}")
    return command
