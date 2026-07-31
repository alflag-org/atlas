"""Strict host configuration model for ``config.yml``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .manifests import validate_name
from .yamlutil import load_yaml_file


@dataclass(frozen=True)
class RuntimeConfig:
    """Configuration for the shared Python artifact runtime."""

    python_version: str


@dataclass(frozen=True)
class ReleaseConfig:
    """Source configuration for one Atlas release."""

    source: str
    enabled: bool = True


@dataclass(frozen=True)
class AtlasConfig:
    """Validated Atlas host configuration."""

    path: Path
    runtime: RuntimeConfig
    releases: dict[str, ReleaseConfig]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    return value


def _only(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown key: {unknown[0]}")


def load_config(path: Path) -> AtlasConfig:
    """Load and validate Atlas host configuration."""
    raw = _mapping(load_yaml_file(path), "config.yml")
    _only(raw, {"runtime", "releases"}, "config.yml")
    runtime = _mapping(raw.get("runtime"), "runtime")
    _only(runtime, {"python"}, "runtime")
    python = _mapping(runtime.get("python"), "runtime.python")
    _only(python, {"version"}, "runtime.python")
    version = python.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("runtime.python.version is required")

    releases_raw = _mapping(raw.get("releases"), "releases")
    releases: dict[str, ReleaseConfig] = {}
    for raw_name, raw_entry in releases_raw.items():
        if not isinstance(raw_name, str):
            raise TypeError("release name must be a string")
        name = validate_name(raw_name, kind="release")
        entry = _mapping(raw_entry, f"releases.{name}")
        _only(entry, {"source", "enabled"}, f"releases.{name}")
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"releases.{name}.source is required")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise TypeError(f"releases.{name}.enabled must be a boolean")
        releases[name] = ReleaseConfig(source=source.strip(), enabled=enabled)
    return AtlasConfig(
        path=path,
        runtime=RuntimeConfig(python_version=version.strip()),
        releases=releases,
    )
