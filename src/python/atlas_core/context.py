"""Small, language-neutral execution context API."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .host import HostProfile, get_host, parse_host
from .paths import AtlasPaths, get_paths


@dataclass(frozen=True)
class ProgramInfo:
    """Program identity exposed to a child."""

    name: str
    root: Path
    runtime_type: str
    python_version: str | None = None
    venv: Path | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly program representation."""
        runtime: dict[str, object] = {"type": self.runtime_type}
        if self.python_version is not None:
            runtime["python"] = self.python_version
        if self.venv is not None:
            runtime["venv"] = str(self.venv)
        return {"name": self.name, "root": str(self.root), "runtime": runtime}


@dataclass(frozen=True)
class CommandInfo:
    """Command identity exposed to a child."""

    name: str
    path: Path
    type: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-friendly command representation."""
        return {"name": self.name, "path": str(self.path), "type": self.type}


@dataclass(frozen=True)
class ExecutionInfo:
    """Run identifiers exposed to a child."""

    run_id: str
    parent_run_id: str | None
    operation_id: str

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-friendly execution representation."""
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class AtlasContext:
    """Host, paths, program, command, and execution identity."""

    host: HostProfile
    paths: AtlasPaths
    program: ProgramInfo
    command: CommandInfo
    execution: ExecutionInfo
    working_directory: Path

    def to_dict(self) -> dict[str, object]:
        """Return the complete context as JSON-compatible data."""
        return {
            "version": 1,
            "host": self.host.to_dict(),
            "paths": self.paths.to_dict(),
            "program": self.program.to_dict(),
            "command": self.command.to_dict(),
            "execution": self.execution.to_dict(),
            "working_directory": str(self.working_directory),
        }


def _required(mapping: Mapping[str, Any], key: str) -> Any:
    value = mapping.get(key)
    if value is None or value == "":
        raise RuntimeError(f"context field is required: {key}")
    return value


def _from_payload(payload: Mapping[str, Any], env: Mapping[str, str]) -> AtlasContext:
    if payload.get("version") != 1:
        raise RuntimeError("context version must be 1")
    host = parse_host({"version": 1, "host": _required(payload, "host")})
    program_raw = _required(payload, "program")
    command_raw = _required(payload, "command")
    execution_raw = _required(payload, "execution")
    if not isinstance(program_raw, dict) or not isinstance(command_raw, dict):
        raise TypeError("context program and command must be mappings")
    if not isinstance(execution_raw, dict):
        raise TypeError("context execution must be a mapping")
    runtime_raw = _required(program_raw, "runtime")
    if not isinstance(runtime_raw, dict):
        raise TypeError("context runtime must be a mapping")
    venv_value = runtime_raw.get("venv")
    return AtlasContext(
        host=host,
        paths=get_paths(env),
        program=ProgramInfo(
            name=str(_required(program_raw, "name")),
            root=Path(str(_required(program_raw, "root"))),
            runtime_type=str(_required(runtime_raw, "type")),
            python_version=(
                None if runtime_raw.get("python") is None else str(runtime_raw["python"])
            ),
            venv=None if venv_value is None else Path(str(venv_value)),
        ),
        command=CommandInfo(
            name=str(_required(command_raw, "name")),
            path=Path(str(_required(command_raw, "path"))),
            type=str(_required(command_raw, "type")),
        ),
        execution=ExecutionInfo(
            run_id=str(_required(execution_raw, "run_id")),
            parent_run_id=(
                None
                if not execution_raw.get("parent_run_id")
                else str(execution_raw["parent_run_id"])
            ),
            operation_id=str(_required(execution_raw, "operation_id")),
        ),
        working_directory=Path(str(_required(payload, "working_directory"))),
    )


def get_context(env: Mapping[str, str] | None = None) -> AtlasContext:
    """Read the current child context from ``ATLAS_CONTEXT_FILE``."""
    read_env = os.environ if env is None else env
    context_file = read_env.get("ATLAS_CONTEXT_FILE")
    if context_file:
        path = Path(context_file)
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"context file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise RuntimeError("context file must contain an object")
        return _from_payload(payload, read_env)

    required_keys = (
        "ATLAS_PROGRAM_NAME",
        "ATLAS_PROGRAM_ROOT",
        "ATLAS_COMMAND_NAME",
        "ATLAS_COMMAND_PATH",
        "ATLAS_RUNTIME_TYPE",
        "ATLAS_RUN_ID",
        "ATLAS_OPERATION_ID",
    )
    for key in required_keys:
        if not read_env.get(key):
            raise RuntimeError(f"{key} is required")
    paths = get_paths(read_env)
    parent = read_env.get("ATLAS_PARENT_RUN_ID") or None
    return AtlasContext(
        host=get_host(paths.host_file),
        paths=paths,
        program=ProgramInfo(
            name=read_env["ATLAS_PROGRAM_NAME"],
            root=Path(read_env["ATLAS_PROGRAM_ROOT"]),
            runtime_type=read_env["ATLAS_RUNTIME_TYPE"],
            python_version=read_env.get("ATLAS_PYTHON_VERSION"),
            venv=(Path(read_env["ATLAS_VENV"]) if read_env.get("ATLAS_VENV") else None),
        ),
        command=CommandInfo(
            name=read_env["ATLAS_COMMAND_NAME"],
            path=Path(read_env["ATLAS_COMMAND_PATH"]),
            type=read_env["ATLAS_RUNTIME_TYPE"],
        ),
        execution=ExecutionInfo(
            run_id=read_env["ATLAS_RUN_ID"],
            parent_run_id=parent,
            operation_id=read_env["ATLAS_OPERATION_ID"],
        ),
        working_directory=Path(read_env.get("PWD", os.getcwd())),
    )
