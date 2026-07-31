"""pyenv-backed scripts runtime installation and status checks."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

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


def _run_stdout(cmd: list[str], env: dict[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
    except FileNotFoundError as exc:
        raise ValueError(f"{cmd[0]} command is required for atlas runtime install") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{cmd[0]} command failed: {' '.join(cmd)}") from exc
    return proc.stdout.strip()


def _run_checked(cmd: list[str], env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(cmd, check=True, env=env)
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


def _runtime_install_env(
    runtime_root: Path,
    tmp_dir: Path | None,
    python_build_cache_path: Path | None,
) -> dict[str, str]:
    default_tmp_dir = runtime_root.parent / "tmp"
    default_build_cache = (
        Path(os.environ.get("ATLAS_VAR_DIR", str(runtime_root.parent / "var"))) / "cache" / "python-build"
    )
    env = os.environ.copy()
    env.setdefault("TMPDIR", str(tmp_dir or default_tmp_dir))
    env.setdefault("PYTHON_BUILD_CACHE_PATH", str(python_build_cache_path or default_build_cache))
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["PYTHON_BUILD_CACHE_PATH"]).mkdir(parents=True, exist_ok=True)
    return env


def _ensure_pyenv_runtime(version: str, env: dict[str, str] | None = None) -> Path:
    if not pyenv_available():
        raise ValueError("pyenv command is required for atlas runtime install")
    _run_checked([RUNTIME_PROVIDER, "install", "-s", version], env=env)
    prefix = _run_stdout([RUNTIME_PROVIDER, "prefix", version], env=env)
    if not prefix:
        raise ValueError(f"pyenv did not return an install prefix for Python {version}")
    python = python_bin(Path(prefix))
    if not python.exists():
        raise ValueError(f"pyenv Python executable not found: {python}")
    return python


def _executable_shebang(path: Path) -> str | None:
    if not path.is_file() or not path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return None
    with path.open("rb") as fh:
        first_line = fh.readline(2048)
    if not first_line.startswith(b"#!"):
        return None
    return first_line.decode("utf-8").rstrip("\r\n")


def _validate_console_script_shebangs(scripts_venv: Path, tmp_scripts: Path) -> None:
    bin_dir = scripts_venv / "bin"
    expected_python = python_bin(scripts_venv)
    envs_root = scripts_venv.parent
    for executable in sorted(bin_dir.iterdir()):
        first_line = _executable_shebang(executable)
        if first_line is None:
            continue
        has_tmp_path = "scripts.tmp." in first_line or str(tmp_scripts) in first_line
        has_non_final_runtime_path = str(envs_root) in first_line and str(expected_python) not in first_line
        if has_tmp_path or has_non_final_runtime_path:
            raise ValueError(
                "console script shebang must point to "
                f"{expected_python}: {executable} has {first_line}"
            )


def install_runtime(
    runtime_root: Path,
    python_version: str,
    scripts_roots: Iterable[Path] | Path | None = None,
    *,
    tmp_dir: Path | None = None,
    python_build_cache_path: Path | None = None,
) -> Path:
    """Install or replace the scripts runtime venv.

    The venv is created in a temporary directory, moved into its final path,
    and populated through the final-path interpreter so generated console
    scripts keep stable shebangs.
    """
    scripts = runtime_root / "python" / "envs" / "scripts"
    scripts.parent.mkdir(parents=True, exist_ok=True)
    env = _runtime_install_env(runtime_root, tmp_dir, python_build_cache_path)
    python = _ensure_pyenv_runtime(python_version, env=env)

    tmp_scripts = scripts.parent / f"scripts.tmp.{os.getpid()}"
    backup_scripts = scripts.parent / f"scripts.bak.{os.getpid()}"
    remove_path(tmp_scripts)
    remove_path(backup_scripts)
    _run_checked([str(python), "-m", "venv", str(tmp_scripts)], env=env)

    if scripts.exists() or scripts.is_symlink():
        scripts.rename(backup_scripts)
    try:
        tmp_scripts.rename(scripts)
        scripts_py = python_bin(scripts)
        _run_checked([str(scripts_py), "-m", "pip", "install", "--upgrade", "pip"], env=env)
        _run_checked([str(scripts_py), "-m", "pip", "install", *_runtime_requirements(scripts_roots)], env=env)
        _validate_console_script_shebangs(scripts, tmp_scripts)
        _run_checked([str(scripts_py), "-m", "pip", "check"], env=env)
    except Exception:
        remove_path(scripts)
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
