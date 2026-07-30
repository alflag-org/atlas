from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from atlas.paths import AtlasPaths

from .support import configure_paths, make_release


@pytest.fixture
def atlas_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AtlasPaths:
    return configure_paths(monkeypatch, tmp_path)


@pytest.fixture
def release_factory(tmp_path: Path) -> Callable[..., Path]:
    counter = 0

    def factory(**kwargs) -> Path:
        nonlocal counter
        counter += 1
        return make_release(tmp_path / f"release-{counter}", **kwargs)

    return factory
