from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .yamlutil import load_yaml_file


@dataclass(frozen=True)
class RuntimeConfig:
    python_version: str


@dataclass(frozen=True)
class RegistryEntry:
    source: str


@dataclass(frozen=True)
class ScriptsConfig:
    source: str
    auto_update: bool = False
    registries: dict[str, RegistryEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class AtlasConfig:
    path: Path
    runtime: RuntimeConfig
    scripts: ScriptsConfig


def load_config(path: Path) -> AtlasConfig:
    raw = load_yaml_file(path)
    if not isinstance(raw, dict):
        raise ValueError("config.yml must be a mapping")
    runtime_raw = raw.get("runtime")
    if not isinstance(runtime_raw, dict):
        raise ValueError("runtime section is required")
    py_raw = runtime_raw.get("python")
    if not isinstance(py_raw, dict):
        raise ValueError("runtime.python section is required")
    py_ver = str(py_raw.get("version", "")).strip()
    if not py_ver:
        raise ValueError("runtime.python.version is required")
    scripts_raw = raw.get("scripts")
    if not isinstance(scripts_raw, dict):
        raise ValueError("scripts section is required")
    source = str(scripts_raw.get("source", "")).strip()
    if not source:
        raise ValueError("scripts.source is required")
    auto_update = bool(scripts_raw.get("auto_update", False))
    registries_raw = scripts_raw.get("registries", {})
    if registries_raw is None:
        registries_raw = {}
    if not isinstance(registries_raw, dict):
        raise ValueError("scripts.registries must be a mapping")
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
            raise ValueError(f"scripts.registries.{alias_name} must be a mapping or string")
        if not entry_source:
            raise ValueError(f"scripts.registries.{alias_name}.source is required")
        registries[alias_name] = RegistryEntry(source=entry_source)

    return AtlasConfig(
        path=path,
        runtime=RuntimeConfig(python_version=py_ver),
        scripts=ScriptsConfig(source=source, auto_update=auto_update, registries=registries),
    )
