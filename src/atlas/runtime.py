from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from .files import remove_path

RUNTIME_PROVIDER = "pyenv"


@dataclass(frozen=True)
class RuntimeStatus:
    provider: str
    provider_available: bool
    scripts_venv: Path
    scripts_python: Path
    scripts_python_exists: bool
    configured_version: str | None = None
    pyenv_python: Path | None = None
    pyenv_python_error: str | None = None


def python_bin(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def pyenv_available() -> bool:
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


def _runtime_requirements(scripts_root: Path | None) -> list[str]:
    base = ["fire", "PyYAML"]
    if scripts_root is None:
        return base
    for candidate in _requirements_candidates(scripts_root):
        if candidate.exists():
            return [*base, "-r", str(candidate)]
    return base


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


def install_runtime(runtime_root: Path, python_version: str, scripts_root: Path | None = None) -> Path:
    scripts = runtime_root / "python" / "envs" / "scripts"
    scripts.parent.mkdir(parents=True, exist_ok=True)
    python = _ensure_pyenv_runtime(python_version)
    remove_path(scripts)
    _run_checked([str(python), "-m", "venv", str(scripts)])

    scripts_py = python_bin(scripts)
    _run_checked([str(scripts_py), "-m", "pip", "install", "--upgrade", "pip"])
    _run_checked([str(scripts_py), "-m", "pip", "install", *_runtime_requirements(scripts_root)])
    return scripts_py


def runtime_status(runtime_root: Path, python_version: str | None = None) -> RuntimeStatus:
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
