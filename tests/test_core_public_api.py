from __future__ import annotations

import atlas_core
from atlas_core import AtlasContext, AtlasPaths, HostProfile, ScriptInfo
from atlas_core import get_context, get_host, get_paths


def test_public_imports() -> None:
    assert AtlasContext is not None
    assert AtlasPaths is not None
    assert HostProfile is not None
    assert ScriptInfo is not None
    assert get_context is not None
    assert get_host is not None
    assert get_paths is not None


def test_public_exports_are_stable() -> None:
    assert atlas_core.__all__ == [
        "AtlasContext",
        "AtlasPaths",
        "HostProfile",
        "ScriptInfo",
        "get_context",
        "get_host",
        "get_paths",
    ]
