"""Interpreter selection and manager failures without interpreter downloads."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas import runtime
from atlas.config import AtlasConfig, ProgramConfig, ProgramRuntime, RuntimeConfig
from atlas.paths import get_paths


@pytest.mark.parametrize("language,manager", [("python", "pyenv"), ("perl", "plenv")])
def test_manager_selection_and_install(language, manager, tmp_path, monkeypatch):
    binary = tmp_path / "installed/bin" / language
    calls = []
    monkeypatch.setattr(runtime.shutil, "which", lambda name: f"/tools/{name}")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "install":
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            return SimpleNamespace(stdout="")
        if not binary.exists():
            raise subprocess.CalledProcessError(1, argv)
        return SimpleNamespace(stdout=str(binary.parent.parent) + "\n")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    selected = RuntimeConfig("5.40.0" if language == "perl" else "3.13")
    with pytest.raises(FileNotFoundError, match="configure"):
        runtime.resolve_runtime(language, selected)
    assert not any(argv[1] == "install" for argv, _ in calls)
    assert runtime.resolve_runtime(language, selected, install=True) == binary
    assert [f"/tools/{manager}", "install", selected.version] in [
        argv for argv, _ in calls
    ]
    assert runtime.resolve_runtime(language, selected) == binary


def test_resolution_and_install_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="required"):
        runtime.resolve_runtime("python", RuntimeConfig())
    for install in (False, True):
        with pytest.raises(FileNotFoundError, match="not found"):
            runtime.resolve_runtime("perl", RuntimeConfig("5.40.0"), install=install)
    with pytest.raises(FileNotFoundError):
        runtime.resolve_runtime("perl", RuntimeConfig(executable=tmp_path / "missing"))
    file = tmp_path / "not-executable"
    file.touch()
    with pytest.raises(PermissionError):
        runtime.resolve_runtime("perl", RuntimeConfig(executable=file))
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/tools/plenv")
    monkeypatch.setattr(
        runtime.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="")
    )
    with pytest.raises(FileNotFoundError):
        runtime.resolve_runtime("perl", RuntimeConfig("5.40.0"), install=True)

    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, a[0])

    monkeypatch.setattr(runtime.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="could not install"):
        runtime.resolve_runtime("perl", RuntimeConfig("5.40.0"), install=True)


def test_defaults_overrides_links_and_status(tmp_path, monkeypatch):
    paths = get_paths({"ATLAS_HOME": str(tmp_path)})
    selected = RuntimeConfig("3.14", Path(sys.executable))
    item = ProgramConfig("tool", tmp_path, ProgramRuntime("python", environment="tool"))
    native = ProgramConfig("native", tmp_path, ProgramRuntime("native"))
    cfg = AtlasConfig(
        tmp_path / "config.yml", {"python": selected}, {"tool": item, "native": native}
    )
    assert runtime.selection(cfg, item) == selected
    assert runtime.runtime_selections(cfg) == [("python", selected)]
    target = runtime.install_configured_runtimes(paths, cfg)[0]
    assert target == paths.runtime("python", "3.14")
    assert target.resolve() == Path(sys.executable).resolve()
    assert runtime.install_configured_runtimes(paths, cfg) == [target]
    target.unlink()
    target.symlink_to(tmp_path / "wrong")
    assert runtime.ensure_runtime(paths, "python", selected) == target
    target.unlink()
    target.touch()
    with pytest.raises(ValueError, match="not a symlink"):
        runtime.ensure_runtime(paths, "python", selected)
    assert runtime.runtime_status(paths, cfg)[0].available
    override = ProgramConfig(
        "other", tmp_path, ProgramRuntime("python", "3.13", "other")
    )
    assert runtime.selection(cfg, override) == RuntimeConfig("3.13")
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    cfg = AtlasConfig(tmp_path / "config.yml", {"perl": RuntimeConfig("5.40.0")}, {})
    assert not runtime.runtime_status(paths, cfg)[0].available
    with pytest.raises(ValueError, match="not configured"):
        runtime.runtime_selections(AtlasConfig(cfg.path, {}, {"tool": item}))
