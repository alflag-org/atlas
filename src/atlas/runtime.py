from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import venv


def python_bin(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def install_runtime(runtime_root: Path) -> Path:
    scripts = runtime_root / "python" / "envs" / "scripts"
    scripts.parent.mkdir(parents=True, exist_ok=True)
    venv.create(scripts, with_pip=True, clear=False)

    scripts_py = python_bin(scripts)
    subprocess.run([str(scripts_py), "-m", "pip", "install", "fire", "PyYAML"], check=True)
    return scripts_py


def runtime_status(runtime_root: Path) -> dict[str, str]:
    scripts = python_bin(runtime_root / "python" / "envs" / "scripts")
    return {"scripts": str(scripts)}


def current_python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
