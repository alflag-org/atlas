"""Strict ``jobs.d`` instance configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifests import validate_name
from .yamlutil import load_yaml_file

SCHEMA = "atlas.job-instance/v1"


@dataclass(frozen=True)
class JobInstance:
    """One named, non-interactive job invocation."""

    name: str
    release: str
    job: str
    user: str
    working_directory: Path
    arguments: tuple[str, ...]
    environment_files: tuple[Path, ...]
    timeout_seconds: int | None
    lock: str


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    return value


def _string(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} is required")
    return value.strip()


def _jobs_directory(jobs_dir: Path) -> None:
    if jobs_dir.is_symlink() or (jobs_dir.exists() and not jobs_dir.is_dir()):
        raise ValueError(f"jobs directory must be a directory: {jobs_dir}")


def load_job_instance(jobs_dir: Path, name: str) -> JobInstance:
    """Load one instance by its lowercase-hyphen name."""
    validate_name(name, kind="job instance")
    _jobs_directory(jobs_dir)
    path = jobs_dir / f"{name}.yml"
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"job instance file not found: {path}")
    raw = _mapping(load_yaml_file(path), f"job instance {name}")
    allowed = {
        "schema",
        "release",
        "job",
        "user",
        "working_directory",
        "arguments",
        "environment_files",
        "timeout_seconds",
        "lock",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"job instance {name} has unknown key: {unknown[0]}")
    schema = _string(raw, "schema", f"job instance {name}")
    if schema != SCHEMA:
        raise ValueError(f"unsupported job instance schema: {schema}")
    release = validate_name(
        _string(raw, "release", f"job instance {name}"),
        kind="release",
    )
    job = validate_name(_string(raw, "job", f"job instance {name}"), kind="job")
    user = _string(raw, "user", f"job instance {name}")
    working_directory = Path(_string(raw, "working_directory", f"job instance {name}"))
    if not working_directory.is_absolute():
        raise ValueError(f"job instance {name}.working_directory must be absolute")

    arguments = raw.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
        raise ValueError(f"job instance {name}.arguments must be a list[str]")
    environment_files = raw.get("environment_files", [])
    if not isinstance(environment_files, list) or not all(
        isinstance(item, str) for item in environment_files
    ):
        raise ValueError(f"job instance {name}.environment_files must be a list[str]")
    environment_paths = tuple(Path(item) for item in environment_files)
    if any(not item.is_absolute() for item in environment_paths):
        raise ValueError(f"job instance {name}.environment_files must contain absolute paths")

    timeout = raw.get("timeout_seconds")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        raise ValueError(f"job instance {name}.timeout_seconds must be a positive integer")
    lock_value = raw.get("lock", name)
    if not isinstance(lock_value, str):
        raise TypeError(f"job instance {name}.lock must be a string")
    lock = validate_name(lock_value, kind="lock")
    return JobInstance(
        name=name,
        release=release,
        job=job,
        user=user,
        working_directory=working_directory,
        arguments=tuple(arguments),
        environment_files=environment_paths,
        timeout_seconds=timeout,
        lock=lock,
    )


def list_job_instances(jobs_dir: Path) -> list[JobInstance]:
    """Load all ``*.yml`` job instances in name order."""
    _jobs_directory(jobs_dir)
    if not jobs_dir.exists():
        return []
    return [load_job_instance(jobs_dir, path.stem) for path in sorted(jobs_dir.glob("*.yml"))]
