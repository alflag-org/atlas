"""YAML file helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml_file(path: Path) -> Any:
    """Load a YAML file, raising ``FileNotFoundError`` if it is missing."""
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def dump_yaml_file(path: Path, data: Any) -> None:
    """Write data as YAML, creating the parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)
