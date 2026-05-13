from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

RUNTIME_PROVIDER = "pyenv"


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


def install_runtime(runtime_root: Path, python_version: str) -> Path:
    scripts = runtime_root / "python" / "envs" / "scripts"
    scripts.parent.mkdir(parents=True, exist_ok=True)
    python = _ensure_pyenv_runtime(python_version)
    _run_checked([str(python), "-m", "venv", str(scripts)])

    scripts_py = python_bin(scripts)
    _run_checked([str(scripts_py), "-m", "pip", "install", "fire", "PyYAML"])
    return scripts_py


def runtime_status(runtime_root: Path, python_version: str | None = None) -> dict[str, str]:
    scripts = python_bin(runtime_root / "python" / "envs" / "scripts")
    status = {
        "scripts": str(scripts),
        "provider": RUNTIME_PROVIDER,
        "provider_available": str(pyenv_available()).lower(),
    }
    if python_version:
        status["configured_version"] = python_version
        status["python"] = f"pyenv:{python_version}"
    return status
