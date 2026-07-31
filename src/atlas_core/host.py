"""Host profile loading for scripts executed by Atlas."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class HostProfile:
    """Host metadata exposed to Atlas scripts."""

    name: str
    site: str = ""
    zone: str = ""
    role: str = ""
    environment: str = ""
    runtime_kind: str = ""
    tags: tuple[str, ...] = ()

    def has_tag(self, tag: str) -> bool:
        """Return whether the host profile contains ``tag``."""
        return tag in self.tags

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation of the host profile."""
        return {
            "name": self.name,
            "site": self.site,
            "zone": self.zone,
            "role": self.role,
            "environment": self.environment,
            "runtime_kind": self.runtime_kind,
            "tags": list(self.tags),
        }


def _require_optional_string(raw: dict[str, object], key: str) -> str:
    if key not in raw:
        return ""
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError(f"host.yml {key} must be a string")
    return value


def get_host(path: str | Path | None = None) -> HostProfile:
    """Load and validate a host profile.

    Args:
        path: Optional path to ``host.yml``. When omitted, ``ATLAS_HOST_FILE``
            is used, falling back to ``/etc/atlas/host.yml``.

    Raises:
        FileNotFoundError: If the host profile file is missing.
        TypeError: If the YAML structure or an optional field type is invalid.
        ValueError: If the required host name or tags are invalid.
    """
    resolved = Path(path) if path is not None else Path(os.environ.get("ATLAS_HOST_FILE", "/etc/atlas/host.yml"))
    if not resolved.exists():
        raise FileNotFoundError(f"host profile not found: {resolved}")
    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise TypeError("host.yml must be a mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("host.yml name is required and must be a non-empty string")

    tags = raw.get("tags", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("host.yml tags must be a list[str]")

    return HostProfile(
        name=name,
        site=_require_optional_string(raw, "site"),
        zone=_require_optional_string(raw, "zone"),
        role=_require_optional_string(raw, "role"),
        environment=_require_optional_string(raw, "environment"),
        runtime_kind=_require_optional_string(raw, "runtime_kind"),
        tags=tuple(tags),
    )
