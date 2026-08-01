"""Strict, read-only input helpers for operation files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from atlas_operations.operation.errors import InputError, PlanError


def input_file(path: str | Path) -> Path:
    """Resolve a required regular input file without following a final symlink."""
    resolved = Path(path)
    if not resolved.is_file() or resolved.is_symlink():
        raise InputError(f"input file not found or unsafe: {resolved}")
    return resolved


def read_yaml(path: str | Path) -> dict[str, Any]:
    """Read one required YAML mapping."""
    source = input_file(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise InputError(f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{source} must contain a YAML mapping")
    return data


def read_json(path: str | Path) -> dict[str, Any]:
    """Read one required JSON object."""
    source = input_file(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError(f"{source} must contain a JSON object")
    return data
