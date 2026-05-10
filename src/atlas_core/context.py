from __future__ import annotations

from dataclasses import dataclass
import os

from .host import HostProfile, get_host
from .paths import AtlasPaths, get_paths


@dataclass(frozen=True)
class ScriptInfo:
    name: str
    version: str


@dataclass(frozen=True)
class AtlasContext:
    host: HostProfile
    paths: AtlasPaths
    script: ScriptInfo


def get_context() -> AtlasContext:
    script_name = os.environ.get("ATLAS_SCRIPT_NAME")
    if not script_name:
        raise RuntimeError("ATLAS_SCRIPT_NAME is required")
    script_version = os.environ.get("ATLAS_SCRIPT_VERSION", "")
    return AtlasContext(
        host=get_host(),
        paths=get_paths(),
        script=ScriptInfo(name=script_name, version=script_version),
    )
