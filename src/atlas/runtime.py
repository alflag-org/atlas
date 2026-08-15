"""Atlas-managed Python interpreter links and per-program virtual environments."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import AtlasConfig, ProgramConfig
from .paths import AtlasPaths


@dataclass(frozen=True)
class RuntimeStatus:
    """Availability of one selected Python runtime."""

    version: str
    executable: Path | None
    atlas_path: Path
    available: bool


def _check_executable(path: Path, label: str) -> Path:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    if not os.access(path, os.X_OK):
        raise PermissionError(f"{label} is not executable: {path}")
    return path.resolve()


def _pyenv_python(version: str) -> Path | None:
    pyenv = shutil.which("pyenv")
    if pyenv is None:
        return None
    try:
        result = subprocess.run(
            [pyenv, "prefix", version],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    prefix_value = result.stdout.strip()
    if not prefix_value:
        return None
    prefix = Path(prefix_value)
    candidate = prefix / "bin" / "python"
    return candidate if candidate.exists() else None


def _install_pyenv_python(version: str) -> Path | None:
    """Install one missing Python version through an existing pyenv installation."""
    pyenv = shutil.which("pyenv")
    if pyenv is None:
        return None
    try:
        subprocess.run([pyenv, "install", "--skip-existing", version], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"pyenv could not install Python {version}") from exc
    candidate = _pyenv_python(version)
    if candidate is None:
        raise FileNotFoundError(f"pyenv Python executable not found for version: {version}")
    return _check_executable(candidate, "pyenv Python executable")


def resolve_python(
    version: str,
    *,
    executable: Path | None = None,
) -> Path:
    """Resolve an explicitly selected Python without downloading anything."""
    if not version and executable is None:
        raise ValueError("Python runtime version or executable is required")
    if executable is not None:
        return _check_executable(executable, "configured Python executable")
    pyenv_candidate = _pyenv_python(version)
    if pyenv_candidate is not None:
        return _check_executable(pyenv_candidate, "pyenv Python executable")
    raise FileNotFoundError(
        f"Python runtime not found for version: {version}; configure pyenv or an executable"
    )


def _managed_python(version: str, executable: Path | None) -> Path:
    if executable is not None:
        return _check_executable(executable, "configured Python executable")
    pyenv_python = _install_pyenv_python(version)
    if pyenv_python is not None:
        return pyenv_python
    return resolve_python(version)


def configured_version(config: AtlasConfig, program: ProgramConfig) -> str | None:
    """Return the program-specific version or the global default."""
    return program.runtime.python_version or config.runtime.python_version


def configured_executable(config: AtlasConfig, program: ProgramConfig) -> Path | None:
    """Return a global executable only when it matches the program selection."""
    executable = config.runtime.executable
    if (
        program.runtime.python_version is not None
        and program.runtime.python_version != config.runtime.python_version
    ):
        return None
    return executable


def ensure_python_runtime(
    paths: AtlasPaths,
    config: AtlasConfig,
    program: ProgramConfig,
) -> Path:
    """Select and expose one Python interpreter under Atlas's runtime tree."""
    version = configured_version(config, program)
    if version is None:
        if config.runtime.executable is None:
            raise ValueError(f"Python version is not configured for program: {program.name}")
        version = "configured"
    source = _managed_python(version, configured_executable(config, program))
    target = paths.python_runtime(version)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == source:
            return target
        target.unlink()
    elif target.exists():
        raise ValueError(f"Atlas Python runtime path is not a symlink: {target}")
    target.symlink_to(source)
    return target


def _venv_python(venv: Path) -> Path:
    candidates = (venv / "bin/python", venv / "Scripts/python.exe")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"venv Python executable not found: {venv}")


def create_venv(paths: AtlasPaths, config: AtlasConfig, program: ProgramConfig) -> Path:
    """Create a dedicated venv for a Python program."""
    if program.runtime.type != "python" or program.runtime.venv is None:
        raise ValueError(f"program does not use Python: {program.name}")
    base_python = ensure_python_runtime(paths, config, program)
    target = paths.venv(program.runtime.venv)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError(f"venv path must be a directory: {target}")
    if target.exists():
        _venv_python(target)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{target.name}.tmp-", dir=target.parent))
    try:
        subprocess.run([str(base_python), "-m", "venv", str(temporary)], check=True)
        _venv_python(temporary)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def venv_python(paths: AtlasPaths, program: ProgramConfig) -> Path:
    """Return the already-created interpreter for a Python program."""
    if program.runtime.type != "python" or program.runtime.venv is None:
        raise ValueError(f"program does not use Python: {program.name}")
    return _check_executable(_venv_python(paths.venv(program.runtime.venv)), "venv Python executable")


def runtime_versions(config: AtlasConfig) -> list[tuple[str, Path | None]]:
    """Return the distinct Python selections required by the configuration."""
    selections: dict[str, Path | None] = {}
    if config.runtime.python_version is not None:
        selections[config.runtime.python_version] = config.runtime.executable
    for program in config.programs.values():
        if program.runtime.type != "python":
            continue
        version = configured_version(config, program)
        if version is None:
            if config.runtime.executable is None:
                raise ValueError(f"Python version is not configured for program: {program.name}")
            version = "configured"
        selections.setdefault(version, configured_executable(config, program))
    return list(selections.items())


def install_configured_runtimes(paths: AtlasPaths, config: AtlasConfig) -> list[Path]:
    """Expose every Python runtime required by configured programs."""
    installed: list[Path] = []
    for version, executable in runtime_versions(config):
        source = _managed_python(version, executable)
        target = paths.python_runtime(version)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            if target.resolve() != source:
                target.unlink()
            else:
                installed.append(target)
                continue
        elif target.exists():
            raise ValueError(f"Atlas Python runtime path is not a symlink: {target}")
        target.symlink_to(source)
        installed.append(target)
    return installed


def runtime_status(paths: AtlasPaths, config: AtlasConfig) -> list[RuntimeStatus]:
    """Return runtime status without changing the filesystem."""
    statuses: list[RuntimeStatus] = []
    for version, executable in runtime_versions(config):
        atlas_path = paths.python_runtime(version)
        try:
            resolved = resolve_python(version, executable=executable)
        except (FileNotFoundError, PermissionError, ValueError):
            resolved = None
        statuses.append(
            RuntimeStatus(
                version=version,
                executable=resolved,
                atlas_path=atlas_path,
                available=resolved is not None,
            )
        )
    return statuses
