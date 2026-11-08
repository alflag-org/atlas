"""Interpreter selection delegated to pyenv, plenv, or explicit executables."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AtlasConfig, ProgramConfig, RuntimeConfig
from .paths import AtlasPaths


@dataclass(frozen=True)
class RuntimeStatus:
    """Availability of one configured interpreter selection."""

    type: str
    version: str
    executable: Path | None
    atlas_path: Path
    available: bool


def check_executable(path: Path, label: str) -> Path:
    """Check a local executable while preserving a venv's interpreter symlink."""
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    if not os.access(path, os.X_OK):
        raise PermissionError(f"{label} is not executable: {path}")
    return path


def selection(config: AtlasConfig, program: ProgramConfig) -> RuntimeConfig:
    """Apply a program version override to its language's host default."""
    default = config.runtime.get(program.runtime.type, RuntimeConfig())
    version = program.runtime.version or default.version
    executable = default.executable if version == default.version else None
    if version is None and executable is None:
        raise ValueError(
            f"runtime version or executable is not configured for program: {program.name}"
        )
    return RuntimeConfig(version=version, executable=executable)


def _manager_interpreter(language: str, version: str) -> Path | None:
    manager = shutil.which("pyenv" if language == "python" else "plenv")
    if manager is None:
        return None
    try:
        result = subprocess.run(
            [manager, "prefix", version],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    prefix = result.stdout.strip()
    if not prefix:
        return None
    candidate = Path(prefix) / "bin" / language
    return candidate if candidate.is_file() else None


def resolve_runtime(
    language: str, selected: RuntimeConfig, *, install: bool = False
) -> Path:
    """Select a local interpreter; only explicit installation may invoke a manager."""
    if selected.executable is not None:
        return check_executable(
            selected.executable, f"configured {language} executable"
        ).resolve()
    version = selected.version
    if not version:
        raise ValueError(f"{language} runtime version or executable is required")
    candidate = _manager_interpreter(language, version)
    if candidate is None and install:
        manager = shutil.which("pyenv" if language == "python" else "plenv")
        if manager is not None:
            try:
                subprocess.run([manager, "install", version], check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise RuntimeError(
                    f"{manager} could not install {language} {version}"
                ) from exc
            candidate = _manager_interpreter(language, version)
    if candidate is None:
        raise FileNotFoundError(
            f"{language} runtime not found for version: {version}; configure "
            f"{'pyenv' if language == 'python' else 'plenv'} or an executable"
        )
    return check_executable(candidate, f"{language} executable").resolve()


def ensure_runtime(paths: AtlasPaths, language: str, selected: RuntimeConfig) -> Path:
    """Install if necessary, then register an interpreter reference."""
    target = paths.runtime(language, selected.version or "configured")
    source = resolve_runtime(language, selected, install=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == source:
            return target
        target.unlink()
    elif target.exists():
        raise ValueError(f"Atlas runtime path is not a symlink: {target}")
    target.symlink_to(source)
    return target


def runtime_selections(config: AtlasConfig) -> list[tuple[str, RuntimeConfig]]:
    """Return distinct selections including host defaults without programs."""
    selections = {
        (language, value.version): value for language, value in config.runtime.items()
    }
    for program in config.programs.values():
        if program.runtime.type == "native":
            continue
        selected = selection(config, program)
        selections[(program.runtime.type, selected.version)] = selected
    return [(language, value) for (language, _version), value in selections.items()]


def install_configured_runtimes(paths: AtlasPaths, config: AtlasConfig) -> list[Path]:
    """Register every interpreter selected by the host and programs."""
    return [
        ensure_runtime(paths, language, value)
        for language, value in runtime_selections(config)
    ]


def runtime_status(paths: AtlasPaths, config: AtlasConfig) -> list[RuntimeStatus]:
    """Inspect availability without installing interpreters or creating paths."""
    statuses = []
    for language, value in runtime_selections(config):
        version = value.version or "configured"
        atlas_path = paths.runtime(language, version)
        try:
            resolved = resolve_runtime(language, value)
        except (FileNotFoundError, PermissionError, ValueError):
            resolved = None
        statuses.append(
            RuntimeStatus(language, version, resolved, atlas_path, resolved is not None)
        )
    return statuses
