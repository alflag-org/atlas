"""pyenv-backed scripts runtime installation and status checks."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from pathlib import Path
import os
import shutil
import subprocess

from .files import remove_path

RUNTIME_PROVIDER = "pyenv"


@dataclass(frozen=True)
class RuntimeStatus:
    """Status information for the scripts Python runtime."""

    provider: str
    provider_available: bool
    scripts_venv: Path
    scripts_python: Path
    scripts_python_exists: bool
    configured_version: str | None = None
    pyenv_python: Path | None = None
    pyenv_python_error: str | None = None


def python_bin(venv_dir: Path) -> Path:
    """Return the Python executable path for a venv directory."""
    return venv_dir / "bin" / "python"


def pyenv_available() -> bool:
    """Return whether the pyenv command is available on PATH."""
    return shutil.which(RUNTIME_PROVIDER) is not None


def _run_stdout(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{cmd[0]} command is required for atlas runtime install") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{cmd[0]} command failed: {' '.join(cmd)}") from exc
    return proc.stdout.strip()


def _run_checked(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{cmd[0]} command is required for atlas runtime install") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{cmd[0]} command failed: {' '.join(cmd)}") from exc


def _requirements_candidates(scripts_root: Path) -> list[Path]:
    return [
        scripts_root / "requirements.lock",
        scripts_root / "requirements.txt",
    ]


def _normalize_scripts_roots(scripts_roots: Iterable[Path] | Path | None) -> list[Path] | None:
    if scripts_roots is None:
        return None
    if isinstance(scripts_roots, Path):
        return [scripts_roots]
    return list(scripts_roots)


def _runtime_requirements(scripts_roots: Iterable[Path] | None) -> list[str]:
    base = ["fire", "PyYAML"]
    normalized = _normalize_scripts_roots(scripts_roots)
    if normalized is None:
        return base
    requirements = [*base]
    for scripts_root in normalized:
        for candidate in _requirements_candidates(scripts_root):
            if candidate.exists():
                requirements.extend(["-r", str(candidate)])
                break
    return requirements


def _ensure_pyenv_runtime(version: str) -> Path:
    if not pyenv_available():
        raise ValueError("pyenv command is required for atlas runtime install")
    _run_checked([RUNTIME_PROVIDER, "install", "-s", version])
    prefix = _run_stdout([RUNTIME_PROVIDER, "prefix", version])
    if not prefix:
        raise ValueError(f"pyenv did not return an install prefix for Python {version}")
    python = python_bin(Path(prefix))
    if not python.exists():
        raise ValueError(f"pyenv Python executable not found: {python}")
    return python


def install_runtime(
    runtime_root: Path,
    python_version: str,
    scripts_roots: Iterable[Path] | Path | None = None,
) -> Path:
    """Install or replace the scripts runtime venv.

    The venv is built in a temporary directory and renamed into place after
    dependencies pass ``pip check``.
    """
    scripts = runtime_root / "python" / "envs" / "scripts"
    scripts.parent.mkdir(parents=True, exist_ok=True)
    python = _ensure_pyenv_runtime(python_version)

    tmp_scripts = scripts.parent / f"scripts.tmp.{os.getpid()}"
    remove_path(tmp_scripts)
    _run_checked([str(python), "-m", "venv", str(tmp_scripts)])

    scripts_py = python_bin(tmp_scripts)
    _run_checked([str(scripts_py), "-m", "pip", "install", "--upgrade", "pip"])
    _run_checked([str(scripts_py), "-m", "pip", "install", *_runtime_requirements(scripts_roots)])
    _run_checked([str(scripts_py), "-m", "pip", "check"])

    backup_scripts = scripts.parent / f"scripts.bak.{os.getpid()}"
    remove_path(backup_scripts)
    if scripts.exists() or scripts.is_symlink():
        scripts.rename(backup_scripts)
    try:
        tmp_scripts.rename(scripts)
    except Exception:
        if backup_scripts.exists() or backup_scripts.is_symlink():
            backup_scripts.rename(scripts)
        raise
    remove_path(backup_scripts)
    return python_bin(scripts)


def runtime_status(runtime_root: Path, python_version: str | None = None) -> RuntimeStatus:
    """Return status information for the configured scripts runtime."""
    scripts_venv = runtime_root / "python" / "envs" / "scripts"
    scripts = python_bin(scripts_venv)
    provider_available = pyenv_available()
    pyenv_python = None
    pyenv_python_error = None
    if python_version and provider_available:
        try:
            pyenv_python = python_bin(Path(_run_stdout([RUNTIME_PROVIDER, "prefix", python_version])))
        except ValueError as exc:
            pyenv_python_error = str(exc)
    return RuntimeStatus(
        provider=RUNTIME_PROVIDER,
        provider_available=provider_available,
        scripts_venv=scripts_venv,
        scripts_python=scripts,
        scripts_python_exists=scripts.exists(),
        configured_version=python_version,
        pyenv_python=pyenv_python,
        pyenv_python_error=pyenv_python_error,
    )
