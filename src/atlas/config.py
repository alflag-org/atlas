from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .yamlutil import load_yaml_file


@dataclass(frozen=True)
class RuntimeConfig:
    python_version: str


@dataclass(frozen=True)
class ScriptsConfig:
    source: str
    auto_update: bool = False


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

    return AtlasConfig(
        path=path,
        runtime=RuntimeConfig(python_version=py_ver),
        scripts=ScriptsConfig(source=source, auto_update=auto_update),
    )
