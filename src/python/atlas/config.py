"""Strict configuration for locally installed automation programs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlas_core._yaml import load_yaml_file

NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
RESERVED_NAMES = {"atlas"}


@dataclass(frozen=True)
class RuntimeConfig:
    """The default interpreter selection for one language."""

    version: str | None = None
    executable: Path | None = None


@dataclass(frozen=True)
class ProgramRuntime:
    """The execution environment associated with one program."""

    type: str
    version: str | None = None
    environment: str | None = None


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
    runtime: dict[str, RuntimeConfig]
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


def _runtime_version(raw: dict[str, Any], label: str) -> str | None:
    version = _optional_string(raw, "version", label)
    if version is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ValueError(f"{label}.version must be an interpreter version or name")
    return version


def _absolute_path(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


def _load_runtime(raw: Any) -> dict[str, RuntimeConfig]:
    if raw is None:
        return {}
    runtime = _mapping(raw, "runtime")
    _only(runtime, {"python", "perl"}, "runtime")
    selections = {}
    for language, selection in runtime.items():
        label = f"runtime.{language}"
        entry = _mapping(selection, label)
        _only(entry, {"version", "executable"}, label)
        version = _runtime_version(entry, label)
        executable_value = _optional_string(entry, "executable", label)
        executable = (
            None if executable_value is None
            else _absolute_path(executable_value, f"{label}.executable")
        )
        if version is None and executable is None:
            raise ValueError(f"{label}.version or {label}.executable is required")
        selections[language] = RuntimeConfig(version=version, executable=executable)
    return selections


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
    elif runtime_type in {"python", "perl"}:
        _only(runtime_raw, {"type", "version", "environment"}, f"{label}.runtime")
        version = _runtime_version(runtime_raw, f"{label}.runtime")
        environment = _optional_string(runtime_raw, "environment", f"{label}.runtime") or name
        validate_name(environment, kind="environment")
        runtime = ProgramRuntime(
            type=runtime_type,
            version=version,
            environment=environment,
        )
    else:
        raise ValueError(f"{label}.runtime.type must be python, perl or native")
    return ProgramConfig(name=name, root=root, runtime=runtime)


def load_config(path: Path) -> AtlasConfig:
    """Load and strictly validate ``config.yml``."""
    raw = _mapping(load_yaml_file(path), "config.yml")
    _only(raw, {"runtime", "programs"}, "config.yml")
    runtime = _load_runtime(raw.get("runtime"))
    programs_raw = _mapping(raw.get("programs", {}), "programs")
    programs: dict[str, ProgramConfig] = {}
    environment_owners: dict[tuple[str, str], str] = {}
    for raw_name, raw_program in programs_raw.items():
        if not isinstance(raw_name, str):
            raise TypeError("program name must be a string")
        name = validate_name(raw_name, kind="program")
        program = _load_program(name, raw_program)
        if program.runtime.environment is not None:
            key = (program.runtime.type, program.runtime.environment)
            previous = environment_owners.get(key)
            if previous is not None:
                raise ValueError(
                    f"environment is assigned to multiple programs: {previous}, {name}"
                )
            environment_owners[key] = name
        programs[name] = program
    return AtlasConfig(path=path, runtime=runtime, programs=programs)
