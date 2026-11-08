"""Strict host identity loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HostProfile:
    """Identity of the host on which Atlas is running."""

    id: str
    role: str = ""
    site: str = ""

    def to_dict(self) -> dict[str, str]:
        """Return the profile in its file representation."""
        return {"id": self.id, "role": self.role, "site": self.site}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    return value


def _optional_string(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise TypeError(f"{label}.{key} must be a string")
    return value


def parse_host(raw: Any) -> HostProfile:
    """Validate the parsed versioned host document."""
    document = _mapping(raw, "host.yml")
    unknown = sorted(set(document) - {"version", "host"})
    if unknown:
        raise ValueError(f"host.yml has unknown key: {unknown[0]}")
    if document.get("version") != 1:
        raise ValueError("host.yml version must be 1")
    host = _mapping(document.get("host"), "host")
    unknown_host = sorted(set(host) - {"id", "role", "site"})
    if unknown_host:
        raise ValueError(f"host has unknown key: {unknown_host[0]}")
    host_id = host.get("id")
    if not isinstance(host_id, str) or not host_id.strip():
        raise ValueError("host.id is required and must be a non-empty string")
    return HostProfile(
        id=host_id.strip(),
        role=_optional_string(host, "role", "host"),
        site=_optional_string(host, "site", "host"),
    )


def get_host(path: str | Path | None = None) -> HostProfile:
    """Load ``/etc/atlas/host.yml`` or an explicitly supplied file."""
    resolved = Path(path) if path is not None else Path(
        os.environ.get("ATLAS_HOST_FILE", "/etc/atlas/host.yml")
    )
    if not resolved.exists():
        raise FileNotFoundError(f"host profile not found: {resolved}")
    from ._yaml import load_yaml_file

    return parse_host(load_yaml_file(resolved))
