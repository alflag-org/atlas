"""Pyenv-backed Python runtime installation and status."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

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


@dataclass(frozen=True)
class RuntimeCandidate:
    """A fully populated runtime generation waiting for publication."""

    root: Path
    python: Path


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


def _requirements_file(release_root: Path) -> Path | None:
    for candidate in _requirements_candidates(release_root):
        if candidate.is_file():
            return candidate
    return None


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
        candidate = _requirements_file(release_root)
        if candidate is not None:
            requirements.extend(["-r", str(candidate)])
    return requirements


def _site_packages(python: Path, environment: dict[str, str]) -> Path:
    venv_root = python.parent.parent
    candidates = sorted((venv_root / "lib").glob("python*/site-packages"))
    if len(candidates) == 1:
        return candidates[0]
    path = _run_stdout(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        env=environment,
    )
    if not path:
        raise ValueError(f"runtime site-packages path is unavailable: {python}")
    return Path(path)


def _install_atlas_core(python: Path, environment: dict[str, str]) -> None:
    source = Path(__file__).resolve().parents[1] / "atlas_core"
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Atlas core support package is unavailable: {source}")
    destination = _site_packages(python, environment) / "atlas_core"
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"runtime support package destination already exists: {destination}")
    shutil.copytree(source, destination)


def _runtime_generations(runtime_root: Path) -> tuple[Path, Path]:
    environments = runtime_root / "python" / "envs"
    generations = environments / "generations"
    generations.mkdir(parents=True, exist_ok=True)
    return environments, generations


@contextmanager
def _prepare_runtime(
    runtime_root: Path,
    python_version: str,
    release_roots: Iterable[Path] | Path | None,
    *,
    tmp_dir: Path | None = None,
    python_build_cache_path: Path | None = None,
    base_python: Path | None = None,
    upgrade_pip: bool = False,
) -> Iterator[RuntimeCandidate]:
    """Build one clean, dependency-complete runtime generation."""
    _, generations = _runtime_generations(runtime_root)
    candidate_root = generations / f"scripts.{uuid4().hex}"
    remove_path(candidate_root)
    environment = _runtime_install_env(runtime_root, tmp_dir, python_build_cache_path)
    environment = {
        key: value
        for key, value in environment.items()
        if key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "PIP_PREFIX",
            "PIP_TARGET",
            "PIP_USER",
        }
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    python = base_python or _ensure_pyenv_runtime(python_version, env=environment)
    try:
        _run_checked([str(python), "-m", "venv", str(candidate_root)], env=environment)
        candidate_python = python_bin(candidate_root)
        _install_atlas_core(candidate_python, environment)
        if upgrade_pip:
            _run_checked(
                [
                    str(candidate_python),
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "pip",
                ],
                env=environment,
            )
        _run_checked(
            [
                str(candidate_python),
                "-m",
                "pip",
                "install",
                *_runtime_requirements(release_roots),
            ],
            env=environment,
        )
        _run_checked([str(candidate_python), "-m", "pip", "check"], env=environment)
        yield RuntimeCandidate(root=candidate_root, python=candidate_python)
    except BaseException:
        remove_path(candidate_root)
        raise


def _runtime_link_target(active: Path, generations: Path) -> tuple[Path, bool] | None:
    if not active.exists() and not active.is_symlink():
        return None
    if active.is_symlink():
        raw = os.readlink(active)
        if any(part == ".." for part in Path(raw).parts):
            raise ValueError(f"runtime link contains path traversal: {active}")
        target = (active.parent / raw).resolve()
        if target.parent != generations.resolve() or target.is_symlink() or not target.is_dir():
            raise ValueError(f"runtime link target is not a generation: {active}")
        return target, False
    if not active.is_dir():
        raise ValueError(f"runtime environment must be a directory or symlink: {active}")
    return active, True


def _replace_runtime_link(active: Path, target: Path) -> None:
    temporary = active.parent / f".{active.name}.tmp.{uuid4().hex}"
    remove_path(temporary)
    try:
        temporary.symlink_to(target, target_is_directory=True)
        temporary.replace(active)
    finally:
        remove_path(temporary)


@contextmanager
def activate_runtime(runtime_root: Path, candidate: RuntimeCandidate) -> Iterator[Path]:
    """Atomically publish a candidate generation and restore it on failure."""
    environments, generations = _runtime_generations(runtime_root)
    active = environments / "scripts"
    previous = _runtime_link_target(active, generations)
    previous_root: Path | None = None
    previous_was_directory = False
    if previous is not None:
        previous_root, previous_was_directory = previous
        if previous_was_directory:
            legacy = generations / f"legacy.{uuid4().hex}"
            active.rename(legacy)
            previous_root = legacy
    try:
        _replace_runtime_link(active, Path("generations") / candidate.root.name)
        yield python_bin(active)
    except BaseException:
        remove_path(active)
        if previous_root is not None:
            if previous_was_directory:
                previous_root.rename(active)
            else:
                _replace_runtime_link(active, Path("generations") / previous_root.name)
        raise
    else:
        if previous_root is not None:
            remove_path(previous_root)


@contextmanager
def prepared_runtime(
    runtime_root: Path,
    python_version: str,
    release_roots: Iterable[Path] | Path | None,
    *,
    tmp_dir: Path | None = None,
    python_build_cache_path: Path | None = None,
    base_python: Path | None = None,
    upgrade_pip: bool = False,
) -> Iterator[RuntimeCandidate]:
    """Build a candidate runtime without changing the active generation."""
    with _prepare_runtime(
        runtime_root,
        python_version,
        release_roots,
        tmp_dir=tmp_dir,
        python_build_cache_path=python_build_cache_path,
        base_python=base_python,
        upgrade_pip=upgrade_pip,
    ) as candidate:
        yield candidate


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


def _validate_console_script_shebangs(
    runtime_generation: Path,
    runtime_python: Path,
) -> None:
    bin_dir = runtime_generation / "bin"
    envs_root = runtime_generation.parent.parent
    for executable in sorted(bin_dir.iterdir()):
        first_line = _executable_shebang(executable)
        if first_line is None:
            continue
        has_other_runtime_path = str(envs_root) in first_line and str(runtime_python) not in first_line
        if has_other_runtime_path:
            raise ValueError(
                "console script shebang must point to "
                f"{runtime_python}: {executable} has {first_line}"
            )


def install_runtime(
    runtime_root: Path,
    python_version: str,
    release_roots: Iterable[Path] | Path | None = None,
    *,
    tmp_dir: Path | None = None,
    python_build_cache_path: Path | None = None,
) -> Path:
    """Build and atomically publish the shared artifact runtime generation."""
    with prepared_runtime(
        runtime_root,
        python_version,
        release_roots,
        tmp_dir=tmp_dir,
        python_build_cache_path=python_build_cache_path,
        upgrade_pip=True,
    ) as candidate:
        _validate_console_script_shebangs(candidate.root, candidate.python)
        with activate_runtime(runtime_root, candidate) as runtime_python:
            return runtime_python


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
