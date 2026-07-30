from __future__ import annotations

from pathlib import Path

import pytest

import atlas
import atlas_core
from atlas.files import remove_path
from atlas.paths import ensure_dirs, get_paths as get_host_paths
from atlas_core import ArtifactInfo, AtlasContext, AtlasPaths, HostProfile, get_context
from atlas_core.paths import get_paths


def _context_env(tmp_path: Path) -> dict[str, str]:
    etc = tmp_path / "etc"
    etc.mkdir()
    host = etc / "host.yml"
    host.write_text("name: node-1\nsite: lab\n", encoding="utf-8")
    return {
        "ATLAS_HOME": str(tmp_path / "home"),
        "ATLAS_ETC_DIR": str(etc),
        "ATLAS_VAR_DIR": str(tmp_path / "var"),
        "ATLAS_RUNTIME_DIR": str(tmp_path / "runtime"),
        "ATLAS_TMP_DIR": str(tmp_path / "tmp"),
        "ATLAS_RELEASE_NAME": "operations",
        "ATLAS_RELEASE_VERSION": "1.0.0",
        "ATLAS_ARTIFACT_TYPE": "command",
        "ATLAS_ARTIFACT_NAME": "config-show",
        "ATLAS_RELEASE_ROOT": str(tmp_path / "home/releases/operations/1.0.0"),
        "ATLAS_HOST_FILE": str(host),
        "ATLAS_RUN_ID": "run-id",
        "ATLAS_PARENT_RUN_ID": "parent-id",
        "ATLAS_OPERATION_ID": "operation-id",
    }


def test_context_exposes_artifact_and_final_paths(tmp_path: Path) -> None:
    context = get_context(_context_env(tmp_path))

    assert isinstance(context, AtlasContext)
    assert context.host.name == "node-1"
    assert context.artifact == ArtifactInfo(
        name="config-show",
        artifact_type="command",
        release_name="operations",
        version="1.0.0",
        release_root=tmp_path / "home/releases/operations/1.0.0",
        run_id="run-id",
        parent_run_id="parent-id",
        operation_id="operation-id",
    )
    assert context.paths.releases_root == tmp_path / "home/releases"
    assert context.paths.current_root == tmp_path / "home/current"
    assert context.paths.locks == tmp_path / "var/locks"
    value = context.to_dict()
    assert value["artifact"]["parent_run_id"] == "parent-id"
    assert value["paths"]["release_root"].endswith("/operations/1.0.0")
    assert value["host"]["site"] == "lab"


def test_context_treats_empty_parent_as_root(tmp_path: Path) -> None:
    env = _context_env(tmp_path)
    env["ATLAS_PARENT_RUN_ID"] = ""
    assert get_context(env).artifact.parent_run_id is None


@pytest.mark.parametrize(
    "missing",
    [
        "ATLAS_ARTIFACT_NAME",
        "ATLAS_ARTIFACT_TYPE",
        "ATLAS_RELEASE_NAME",
        "ATLAS_RELEASE_VERSION",
        "ATLAS_RELEASE_ROOT",
        "ATLAS_RUN_ID",
        "ATLAS_OPERATION_ID",
    ],
)
def test_context_requires_execution_contract(tmp_path: Path, missing: str) -> None:
    env = _context_env(tmp_path)
    env.pop(missing)
    with pytest.raises(RuntimeError, match=f"{missing} is required"):
        get_context(env)


def test_core_paths_defaults_and_host_override(tmp_path: Path) -> None:
    release = tmp_path / "release"
    paths = get_paths({"ATLAS_RELEASE_ROOT": str(release)})
    assert isinstance(paths, AtlasPaths)
    assert paths.home == Path("/opt/atlas")
    assert paths.etc == Path("/etc/atlas")
    assert paths.var == Path("/var/lib/atlas")
    assert paths.runtime == Path("/opt/atlas/runtime")
    assert paths.tmp == Path("/opt/atlas/tmp")
    assert paths.release_root == release
    assert paths.host_file == Path("/etc/atlas/host.yml")
    with pytest.raises(RuntimeError, match="ATLAS_RELEASE_ROOT is required"):
        get_paths({})

    custom = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "home"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
            "ATLAS_HOST_FILE": str(tmp_path / "host.yml"),
            "ATLAS_RELEASE_ROOT": str(release),
        }
    )
    assert custom.host_file == tmp_path / "host.yml"
    assert all(isinstance(value, str) for value in custom.to_dict().values())


def test_host_side_paths_have_no_scripts_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    etc = tmp_path / "etc"
    var = tmp_path / "var"
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(var))
    monkeypatch.setenv("ATLAS_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ATLAS_TMP_DIR", str(tmp_path / "tmp"))
    paths = get_host_paths()
    assert paths.releases_root == home / "releases"
    assert paths.current_root == home / "current"
    assert paths.artifact_runner == home / "bin/artifact-runner"
    assert paths.runtime_python == tmp_path / "runtime/python/envs/scripts/bin/python"
    assert paths.jobs_dir == etc / "jobs.d"
    assert paths.env_dir == etc / "env"
    ensure_dirs(paths)
    for path in (
        paths.home,
        paths.tmp,
        paths.releases_root,
        paths.current_root,
        paths.logs,
        paths.locks,
        paths.cache,
        paths.shims,
    ):
        assert path.is_dir()


def test_remove_path_handles_file_symlink_directory_and_missing(tmp_path: Path) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    remove_path(file_path)
    assert not file_path.exists()

    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    remove_path(link)
    assert target.exists() and not link.exists()

    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "child").write_text("x", encoding="utf-8")
    remove_path(directory)
    assert not directory.exists()
    remove_path(tmp_path / "missing")


def test_public_api_is_artifact_focused() -> None:
    assert atlas.__all__ == []
    assert atlas_core.__all__ == [
        "ArtifactInfo",
        "AtlasContext",
        "AtlasPaths",
        "HostProfile",
        "get_context",
        "get_host",
        "get_paths",
    ]
    assert HostProfile.__module__ == "atlas_core.host"
