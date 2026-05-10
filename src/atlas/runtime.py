from __future__ import annotations

from pathlib import Path
import subprocess
import venv


def python_bin(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def install_runtime(runtime_root: Path) -> tuple[Path, Path]:
    core = runtime_root / "python" / "envs" / "core"
    scripts = runtime_root / "python" / "envs" / "scripts"
    core.parent.mkdir(parents=True, exist_ok=True)
    venv.create(core, with_pip=True, clear=False)
    venv.create(scripts, with_pip=True, clear=False)

    scripts_py = python_bin(scripts)
    subprocess.run([str(scripts_py), "-m", "pip", "install", "fire", "PyYAML"], check=True)
    return python_bin(core), scripts_py


def runtime_status(runtime_root: Path) -> dict[str, str]:
    core = python_bin(runtime_root / "python" / "envs" / "core")
    scripts = python_bin(runtime_root / "python" / "envs" / "scripts")
    return {"core": str(core), "scripts": str(scripts)}
