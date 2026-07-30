"""Configuration model and loader for ``config.yml``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .manifests import validate_name
from .yamlutil import load_yaml_file


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime configuration for the scripts Python environment."""

    python_version: str


@dataclass(frozen=True)
class RegistryEntry:
    """A named source alias used by scripts release configuration."""

    source: str


@dataclass(frozen=True)
class ScriptReleaseConfig:
    """Configuration for one named scripts release."""

    source: str
    enabled: bool = True


@dataclass(frozen=True)
class ScriptsConfig:
    """Top-level scripts configuration, including legacy and multi-release forms."""

    source: str | None = None
    auto_update: bool = False
    registries: dict[str, RegistryEntry] = field(default_factory=dict)
    releases: dict[str, ScriptReleaseConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class AtlasConfig:
    """Validated Atlas host configuration."""

    path: Path
    runtime: RuntimeConfig
    scripts: ScriptsConfig


def load_config(path: Path) -> AtlasConfig:
    """Load and validate an Atlas ``config.yml`` file."""
    raw = load_yaml_file(path)
    if not isinstance(raw, dict):
        raise TypeError("config.yml must be a mapping")
    runtime_raw = raw.get("runtime")
    if not isinstance(runtime_raw, dict):
        raise TypeError("runtime section is required")
    py_raw = runtime_raw.get("python")
    if not isinstance(py_raw, dict):
        raise TypeError("runtime.python section is required")
    py_ver = str(py_raw.get("version", "")).strip()
    if not py_ver:
        raise ValueError("runtime.python.version is required")
    scripts_raw = raw.get("scripts")
    if not isinstance(scripts_raw, dict):
        raise TypeError("scripts section is required")
    source_raw = scripts_raw.get("source")
    source = None if source_raw is None else str(source_raw).strip() or None
    auto_update = bool(scripts_raw.get("auto_update", False))
    registries_raw = scripts_raw.get("registries", {})
    if registries_raw is None:
        registries_raw = {}
    if not isinstance(registries_raw, dict):
        raise TypeError("scripts.registries must be a mapping")
    registries: dict[str, RegistryEntry] = {}
    for alias, entry_raw in registries_raw.items():
        alias_name = str(alias).strip()
        if not alias_name:
            raise ValueError("scripts.registries alias must not be empty")
        if isinstance(entry_raw, str):
            entry_source = entry_raw.strip()
        elif isinstance(entry_raw, dict):
            entry_source = str(entry_raw.get("source", "")).strip()
        else:
            raise TypeError(f"scripts.registries.{alias_name} must be a mapping or string")
        if not entry_source:
            raise ValueError(f"scripts.registries.{alias_name}.source is required")
        registries[alias_name] = RegistryEntry(source=entry_source)

    releases_raw = scripts_raw.get("releases")
    releases: dict[str, ScriptReleaseConfig] = {}
    if releases_raw is not None:
        if not isinstance(releases_raw, dict):
            raise TypeError("scripts.releases must be a mapping")
        for release_name, release_raw in releases_raw.items():
            name = validate_name(str(release_name).strip(), kind="release")
            if isinstance(release_raw, str):
                release_source = release_raw.strip()
                enabled = True
            elif isinstance(release_raw, dict):
                release_source = str(release_raw.get("source", "")).strip()
                enabled = bool(release_raw.get("enabled", True))
            else:
                raise TypeError(f"scripts.releases.{name} must be a mapping or string")
            if not release_source:
                raise ValueError(f"scripts.releases.{name}.source is required")
            releases[name] = ScriptReleaseConfig(source=release_source, enabled=enabled)
    else:
        if not source:
            raise ValueError("scripts.source is required")
        releases["default"] = ScriptReleaseConfig(source=source)

    return AtlasConfig(
        path=path,
        runtime=RuntimeConfig(python_version=py_ver),
        scripts=ScriptsConfig(source=source, auto_update=auto_update, registries=registries, releases=releases),
    )
