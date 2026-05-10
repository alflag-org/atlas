from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

import yaml


@dataclass(frozen=True)
class HostProfile:
    name: str = "unknown"
    site: str = ""
    zone: str = ""
    role: str = ""
    environment: str = ""
    runtime_kind: str = ""
    tags: list[str] = field(default_factory=list)


def get_host(path: str | None = None) -> HostProfile:
    resolved = Path(path or os.environ.get("ATLAS_HOST_FILE", "/etc/atlas/host.yml"))
    if not resolved.exists():
        raise FileNotFoundError(f"host profile not found: {resolved}")
    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError("host.yml must be a mapping")
    tags = raw.get("tags", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        raise ValueError("host.yml tags must be a list")
    return HostProfile(
        name=str(raw.get("name", "unknown")),
        site=str(raw.get("site", "")),
        zone=str(raw.get("zone", "")),
        role=str(raw.get("role", "")),
        environment=str(raw.get("environment", "")),
        runtime_kind=str(raw.get("runtime_kind", "")),
        tags=[str(x) for x in tags],
    )
