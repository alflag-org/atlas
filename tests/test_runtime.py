from __future__ import annotations

import sys
from pathlib import Path

import pytest

from atlas.config import AtlasConfig, ProgramConfig, ProgramRuntime, RuntimeConfig
from atlas.paths import get_paths
from atlas.runtime import (
    create_venv,
    ensure_python_runtime,
    install_configured_runtimes,
    resolve_python,
    runtime_status,
    runtime_versions,
    venv_python,
)


def program(root: Path, name: str = "tool", *, version: str | None = None, runtime_type: str = "python") -> ProgramConfig:
    return ProgramConfig(
        name=name,
        root=root,
        runtime=ProgramRuntime(type=runtime_type, python_version=version, venv=name if runtime_type == "python" else None),
    )


def config(tmp_path: Path, *programs: ProgramConfig, version: str | None = None) -> AtlasConfig:
    return AtlasConfig(
        path=tmp_path / "config.yml",
        runtime=RuntimeConfig(python_version=version or f"{sys.version_info.major}.{sys.version_info.minor}", executable=Path(sys.executable)),
        programs={item.name: item for item in programs},
    )


def test_resolve_python_uses_configured_executable(tmp_path: Path) -> None:
    assert resolve_python("ignored", executable=Path(sys.executable)) == Path(sys.executable).resolve()
    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_python("9.9", executable=tmp_path / "missing")


def test_runtime_links_and_status(tmp_path: Path) -> None:
    paths = get_paths({
        "ATLAS_HOME": str(tmp_path / "opt"),
        "ATLAS_ETC_DIR": str(tmp_path / "etc"),
        "ATLAS_VAR_DIR": str(tmp_path / "var"),
    })
    item = program(tmp_path / "program")
    cfg = config(tmp_path, item)

    selected = ensure_python_runtime(paths, cfg, item)
    assert selected.is_symlink()
    assert selected.resolve() == Path(sys.executable).resolve()
    assert runtime_status(paths, cfg)[0].available is True
    assert runtime_versions(cfg) == [(cfg.runtime.python_version, Path(sys.executable))]
    assert install_configured_runtimes(paths, cfg) == [selected]


def test_create_and_reuse_dedicated_venv(tmp_path: Path) -> None:
    paths = get_paths({
        "ATLAS_HOME": str(tmp_path / "opt"),
        "ATLAS_ETC_DIR": str(tmp_path / "etc"),
        "ATLAS_VAR_DIR": str(tmp_path / "var"),
    })
    item = program(tmp_path / "program")
    cfg = config(tmp_path, item)

    first = create_venv(paths, cfg, item)
    second = create_venv(paths, cfg, item)

    assert first == second
    assert venv_python(paths, item).exists()


def test_runtime_rejects_native_and_unconfigured_python(tmp_path: Path) -> None:
    paths = get_paths({"ATLAS_HOME": str(tmp_path / "opt")})
    native = program(tmp_path / "native", runtime_type="native")
    cfg = config(tmp_path, native)
    with pytest.raises(ValueError, match="does not use Python"):
        create_venv(paths, cfg, native)

    no_version = program(tmp_path / "python", version=None)
    no_version_cfg = AtlasConfig(tmp_path / "config.yml", RuntimeConfig(), {"python": no_version})
    with pytest.raises(ValueError, match="not configured"):
        ensure_python_runtime(paths, no_version_cfg, no_version)


def test_venv_path_errors_and_runtime_failure(tmp_path: Path) -> None:
    paths = get_paths({"ATLAS_HOME": str(tmp_path / "opt")})
    item = program(tmp_path / "program")
    cfg = config(tmp_path, item)
    paths.venvs.mkdir(parents=True)
    (paths.venvs / item.name).write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="venv path must be a directory"):
        create_venv(paths, cfg, item)

    bad_cfg = AtlasConfig(tmp_path / "config.yml", RuntimeConfig(python_version="9.9"), {"tool": item})
    assert runtime_status(paths, bad_cfg)[0].available is False
