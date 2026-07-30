from __future__ import annotations

import atlas_core
from atlas_core import (
    ArtifactInfo,
    AtlasContext,
    AtlasPaths,
    HostProfile,
    get_context,
    get_host,
    get_paths,
)


def test_public_imports() -> None:
    assert AtlasContext is not None
    assert AtlasPaths is not None
    assert HostProfile is not None
    assert ArtifactInfo is not None
    assert get_context is not None
    assert get_host is not None
    assert get_paths is not None


def test_public_exports_are_stable() -> None:
    assert atlas_core.__all__ == [
        "ArtifactInfo",
        "AtlasContext",
        "AtlasPaths",
        "HostProfile",
        "get_context",
        "get_host",
        "get_paths",
    ]
