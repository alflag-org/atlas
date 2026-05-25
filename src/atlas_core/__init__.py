"""Stable runtime API for scripts executed by Atlas.

Scripts installed into Atlas should import from this package instead of
using host-side implementation modules under :mod:`atlas`.
"""

from .context import AtlasContext, ScriptInfo, get_context
from .host import HostProfile, get_host
from .paths import AtlasPaths, get_paths

__all__ = [
    "AtlasContext",
    "AtlasPaths",
    "HostProfile",
    "ScriptInfo",
    "get_context",
    "get_host",
    "get_paths",
]
