from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil


NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
RESERVED = {"atlas", "script-runner"}


@dataclass(frozen=True)
class CommandEntry:
    name: str
    script_path: Path


def _validate_segment(segment: str) -> None:
    if not NAME_RE.fullmatch(segment):
        raise ValueError(f"invalid command segment: {segment}")


def _command_name_from_rel_py(rel_py: Path) -> str:
    parts = list(rel_py.parts)
    stem = Path(parts[-1]).stem
    segments = [*parts[:-1], stem]
    for seg in segments:
        _validate_segment(seg)
    name = "-".join(segments)
    if "--" in name or name.endswith("-"):
        raise ValueError(f"invalid command name: {name}")
    if name in RESERVED:
        raise ValueError(f"reserved command name: {name}")
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid command name: {name}")
    return name


def discover_commands(commands_dir: Path) -> list[CommandEntry]:
    if not commands_dir.exists() or not commands_dir.is_dir():
        raise ValueError(f"commands directory not found: {commands_dir}")
    root = commands_dir.resolve()
    seen: dict[str, Path] = {}
    out: list[CommandEntry] = []
    for py_file in sorted(commands_dir.rglob("*.py")):
        if py_file.is_symlink():
            raise ValueError(f"symlink is not allowed: {py_file}")
        resolved = py_file.resolve()
        if root not in resolved.parents:
            raise ValueError(f"path traversal detected: {py_file}")
        rel = resolved.relative_to(root)
        name = _command_name_from_rel_py(rel)
        if name in seen:
            raise ValueError(f"command name conflict: {seen[name]} vs {py_file}")
        seen[name] = py_file
        out.append(CommandEntry(name=name, script_path=resolved))
    return out


def read_version(release_root: Path) -> str:
    version_file = release_root / "VERSION"
    if not version_file.exists():
        raise ValueError(f"missing VERSION file: {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("VERSION is empty")
    return version


def install_release(source: Path, releases_root: Path, current_link: Path) -> Path:
    if not source.exists() or not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    version = read_version(source)
    commands = source / "commands"
    discover_commands(commands)

    target = releases_root / version
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    current_link.parent.mkdir(parents=True, exist_ok=True)
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(target, target_is_directory=True)
    return target


def resolve_source(source: str) -> Path:
    if source.startswith("file://"):
        return Path(source[7:])
    return Path(source)
