"""Strict configuration for locally installed automation programs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .yamlutil import load_yaml_file

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
RESERVED_NAMES = {"atlas"}


@dataclass(frozen=True)
class RuntimeConfig:
    """The optional default Python runtime selected by Atlas."""

    python_version: str | None = None
    executable: Path | None = None


@dataclass(frozen=True)
class ProgramRuntime:
    """The execution environment associated with one program."""

    type: str
    python_version: str | None = None
    venv: str | None = None


@dataclass(frozen=True)
class ProgramConfig:
    """A locally installed automation program."""

    name: str
    root: Path
    runtime: ProgramRuntime


@dataclass(frozen=True)
class AtlasConfig:
    """Validated Atlas configuration."""

    path: Path
    runtime: RuntimeConfig
    programs: dict[str, ProgramConfig]


def validate_name(name: str, *, kind: str = "name") -> str:
    """Validate an Atlas identifier."""
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid {kind} name: {name}")
    if name in RESERVED_NAMES:
        raise ValueError(f"reserved {kind} name: {name}")
    return name


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    return value


def _only(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown key: {unknown[0]}")


def _required_string(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} is required")
    return value.strip()


def _optional_string(raw: dict[str, Any], key: str, label: str) -> str | None:
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _absolute_path(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


def _load_runtime(raw: Any) -> RuntimeConfig:
    if raw is None:
        return RuntimeConfig()
    runtime = _mapping(raw, "runtime")
    _only(runtime, {"python"}, "runtime")
    if "python" not in runtime:
        return RuntimeConfig()
    python_raw = runtime["python"]
    python = _mapping(python_raw, "runtime.python")
    _only(python, {"version", "executable"}, "runtime.python")
    version = _optional_string(python, "version", "runtime.python")
    executable_value = _optional_string(python, "executable", "runtime.python")
    executable = (
        None
        if executable_value is None
        else _absolute_path(executable_value, "runtime.python.executable")
    )
    if version is None and executable is None:
        raise ValueError("runtime.python.version or runtime.python.executable is required")
    return RuntimeConfig(python_version=version, executable=executable)


def _load_program(name: str, raw: Any) -> ProgramConfig:
    label = f"programs.{name}"
    entry = _mapping(raw, label)
    _only(entry, {"root", "runtime"}, label)
    root = _absolute_path(_required_string(entry, "root", label), f"{label}.root")
    runtime_raw = _mapping(entry.get("runtime"), f"{label}.runtime")
    runtime_type = _required_string(runtime_raw, "type", f"{label}.runtime")
    if runtime_type == "native":
        _only(runtime_raw, {"type"}, f"{label}.runtime")
        runtime = ProgramRuntime(type="native")
    elif runtime_type == "python":
        _only(runtime_raw, {"type", "python", "venv"}, f"{label}.runtime")
        version = _optional_string(runtime_raw, "python", f"{label}.runtime")
        venv = _optional_string(runtime_raw, "venv", f"{label}.runtime") or name
        validate_name(venv, kind="venv")
        runtime = ProgramRuntime(
            type="python",
            python_version=version,
            venv=venv,
        )
    else:
        raise ValueError(f"{label}.runtime.type must be python or native")
    return ProgramConfig(name=name, root=root, runtime=runtime)


def load_config(path: Path) -> AtlasConfig:
    """Load and strictly validate ``config.yml``."""
    raw = _mapping(load_yaml_file(path), "config.yml")
    _only(raw, {"runtime", "programs"}, "config.yml")
    runtime = _load_runtime(raw.get("runtime"))
    programs_raw = _mapping(raw.get("programs", {}), "programs")
    programs: dict[str, ProgramConfig] = {}
    venv_owners: dict[str, str] = {}
    for raw_name, raw_program in programs_raw.items():
        if not isinstance(raw_name, str):
            raise TypeError("program name must be a string")
        name = validate_name(raw_name, kind="program")
        program = _load_program(name, raw_program)
        if program.runtime.venv is not None:
            previous = venv_owners.get(program.runtime.venv)
            if previous is not None:
                raise ValueError(
                    f"venv is assigned to multiple programs: {previous}, {name}"
                )
            venv_owners[program.runtime.venv] = name
        programs[name] = program
    return AtlasConfig(path=path, runtime=runtime, programs=programs)
