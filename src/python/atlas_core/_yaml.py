"""Strict YAML file helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class _StrictLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml_file(path: Path) -> Any:
    """Load strict YAML, rejecting duplicate keys."""
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        # _StrictLoader derives from SafeLoader and only rejects duplicate keys.
        return yaml.load(fh, Loader=_StrictLoader)  # noqa: S506
