"""Pyenv-backed Python runtime installation and status."""

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
    """Status of the shared Python artifact runtime."""

    provider: str
    provider_available: bool
    artifacts_venv: Path
    runtime_python: Path
    runtime_python_exists: bool
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


def _requirements_candidates(release_root: Path) -> list[Path]:
    return [
        release_root / "requirements.lock",
        release_root / "requirements.txt",
    ]


def _normalize_roots(release_roots: Iterable[Path] | Path | None) -> list[Path] | None:
    if release_roots is None:
        return None
    if isinstance(release_roots, Path):
        return [release_roots]
    return list(release_roots)


def _runtime_requirements(release_roots: Iterable[Path] | None) -> list[str]:
    requirements = ["PyYAML"]
    normalized = _normalize_roots(release_roots)
    if normalized is None:
        return requirements
    for release_root in normalized:
        for candidate in _requirements_candidates(release_root):
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


def _validate_console_script_shebangs(artifacts_venv: Path, temporary_venv: Path) -> None:
    bin_dir = artifacts_venv / "bin"
    expected_python = python_bin(artifacts_venv)
    envs_root = artifacts_venv.parent
    for executable in sorted(bin_dir.iterdir()):
        first_line = _executable_shebang(executable)
        if first_line is None:
            continue
        has_temporary_path = str(temporary_venv) in first_line
        has_other_runtime_path = str(envs_root) in first_line and str(expected_python) not in first_line
        if has_temporary_path or has_other_runtime_path:
            raise ValueError(
                "console script shebang must point to "
                f"{expected_python}: {executable} has {first_line}"
            )


def install_runtime(
    runtime_root: Path,
    python_version: str,
    release_roots: Iterable[Path] | Path | None = None,
    *,
    tmp_dir: Path | None = None,
    python_build_cache_path: Path | None = None,
) -> Path:
    """Atomically replace the shared artifact virtual environment.

    The venv is created in a temporary directory, moved into its final path,
    and populated through the final-path interpreter so generated console
    console scripts keep stable shebangs.
    """
    environment = runtime_root / "python" / "envs" / "scripts"
    environment.parent.mkdir(parents=True, exist_ok=True)
    env = _runtime_install_env(runtime_root, tmp_dir, python_build_cache_path)
    python = _ensure_pyenv_runtime(python_version, env=env)

    temporary = environment.parent / f"scripts.tmp.{os.getpid()}"
    backup = environment.parent / f"scripts.bak.{os.getpid()}"
    remove_path(temporary)
    remove_path(backup)
    _run_checked([str(python), "-m", "venv", str(temporary)], env=env)

    if environment.exists() or environment.is_symlink():
        environment.rename(backup)
    try:
        temporary.rename(environment)
        runtime_python = python_bin(environment)
        _run_checked([str(runtime_python), "-m", "pip", "install", "--upgrade", "pip"], env=env)
        _run_checked(
            [str(runtime_python), "-m", "pip", "install", *_runtime_requirements(release_roots)],
            env=env,
        )
        _validate_console_script_shebangs(environment, temporary)
        _run_checked([str(runtime_python), "-m", "pip", "check"], env=env)
    except Exception:
        remove_path(environment)
        if backup.exists() or backup.is_symlink():
            backup.rename(environment)
        raise
    remove_path(backup)
    return python_bin(environment)


def runtime_status(runtime_root: Path, python_version: str | None = None) -> RuntimeStatus:
    """Return current runtime status without changing it."""
    artifacts_venv = runtime_root / "python" / "envs" / "scripts"
    runtime_python = python_bin(artifacts_venv)
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
        artifacts_venv=artifacts_venv,
        runtime_python=runtime_python,
        runtime_python_exists=runtime_python.exists(),
        configured_version=python_version,
        pyenv_python=pyenv_python,
        pyenv_python_error=pyenv_python_error,
    )
