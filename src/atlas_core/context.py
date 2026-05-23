from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from .host import HostProfile, get_host
from .paths import AtlasPaths, get_paths


@dataclass(frozen=True)
class ScriptInfo:
    name: str
    release_name: str
    version: str
    release_root: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "release_name": self.release_name,
            "version": self.version,
            "release_root": str(self.release_root),
        }


@dataclass(frozen=True)
class AtlasContext:
    host: HostProfile
    paths: AtlasPaths
    script: ScriptInfo

    def to_dict(self) -> dict[str, dict[str, object]]:
        return {
            "host": self.host.to_dict(),
            "paths": self.paths.to_dict(),
            "script": self.script.to_dict(),
        }


def get_context(env: Mapping[str, str] | None = None) -> AtlasContext:
    read_env = os.environ if env is None else env
    script_name = read_env.get("ATLAS_SCRIPT_NAME")
    if not script_name:
        raise RuntimeError("ATLAS_SCRIPT_NAME is required")
    script_release_name = read_env.get("ATLAS_SCRIPT_RELEASE_NAME", "")
    script_version = read_env.get("ATLAS_SCRIPT_VERSION", "")
    script_release_root = Path(read_env.get("ATLAS_SCRIPTS_DIR", ""))
    return AtlasContext(
        host=get_host(),
        paths=get_paths(),
        script=ScriptInfo(
            name=script_name,
            release_name=script_release_name,
            version=script_version,
            release_root=script_release_root,
        ),
    )
