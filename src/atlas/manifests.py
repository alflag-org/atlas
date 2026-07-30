"""Strict ``release.yml`` parsing and command artifact models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .yamlutil import load_yaml_file

SCHEMA = "atlas.release/v1"
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
RUNTIMES = {"python"}
RESERVED_COMMAND_NAMES = {"atlas", "artifact-runner", "script-runner"}


@dataclass(frozen=True)
class CommandArtifact:
    """One public command declared by a release."""

    name: str
    runtime: str
    entrypoint: Path


@dataclass(frozen=True)
class ReleaseManifest:
    """Validated contents of one command-only release manifest."""

    name: str
    commands: dict[str, CommandArtifact]


def validate_name(name: str, *, kind: str = "artifact") -> str:
    """Validate a release or artifact identifier."""
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid {kind} name: {name}")
    if kind == "command" and name in RESERVED_COMMAND_NAMES:
        raise ValueError(f"reserved command name: {name}")
    return name


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    return value


def _reject_unknown(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown key: {unknown[0]}")


def _required_string(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} is required")
    return value.strip()


def _entrypoint(release_root: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise ValueError(f"{label} must be a relative path inside the release")
    path = release_root / relative
    current = path
    while current != release_root:
        if current.is_symlink():
            raise ValueError(f"{label} must not contain a symlink: {value}")
        current = current.parent
    resolved_root = release_root.resolve()
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"{label} escapes the release root")
    if not path.is_file():
        raise ValueError(f"{label} not found: {value}")
    if path.suffix != ".py":
        raise ValueError(f"{label} must end with .py")
    return resolved


def _parse_commands(release_root: Path, raw: Any) -> dict[str, CommandArtifact]:
    entries = _mapping(raw, "commands")
    parsed: dict[str, CommandArtifact] = {}
    for raw_name, raw_entry in entries.items():
        if not isinstance(raw_name, str):
            raise TypeError("command name must be a string")
        name = validate_name(raw_name, kind="command")
        label = f"commands.{name}"
        entry = _mapping(raw_entry, label)
        _reject_unknown(entry, {"runtime", "entrypoint"}, label)
        runtime = _required_string(entry, "runtime", label)
        if runtime not in RUNTIMES:
            raise ValueError(f"{label}.runtime is unsupported: {runtime}")
        parsed[name] = CommandArtifact(
            name=name,
            runtime=runtime,
            entrypoint=_entrypoint(
                release_root,
                _required_string(entry, "entrypoint", label),
                f"{label}.entrypoint",
            ),
        )
    return parsed


def load_manifest(release_root: Path) -> ReleaseManifest:
    """Load and strictly validate ``release.yml`` from ``release_root``."""
    raw = _mapping(load_yaml_file(release_root / "release.yml"), "release.yml")
    _reject_unknown(raw, {"schema", "name", "commands"}, "release.yml")
    schema = _required_string(raw, "schema", "release.yml")
    if schema != SCHEMA:
        raise ValueError(f"unsupported release schema: {schema}")
    name = validate_name(_required_string(raw, "name", "release.yml"), kind="release")
    commands = _parse_commands(release_root, raw.get("commands", {}))
    return ReleaseManifest(name=name, commands=commands)
