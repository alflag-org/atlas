"""Stable runtime API for release artifacts executed by Atlas.

Artifacts installed into Atlas should import from this package instead of
using host-side implementation modules under :mod:`atlas`.
"""

from .context import ArtifactInfo, AtlasContext, get_context
from .host import HostProfile, get_host
from .paths import AtlasPaths, get_paths

__all__ = [
    "ArtifactInfo",
    "AtlasContext",
    "AtlasPaths",
    "HostProfile",
    "get_context",
    "get_host",
    "get_paths",
]
