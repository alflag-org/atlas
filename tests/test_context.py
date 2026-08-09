from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_core.context import get_context


def _context_env(tmp_path: Path) -> dict[str, str]:
    host = tmp_path / "host.yml"
    host.write_text("name: test-host\nsite: site-a\n", encoding="utf-8")
    return {
        "ATLAS_HOME": str(tmp_path / "opt"),
        "ATLAS_ETC_DIR": str(tmp_path / "etc"),
        "ATLAS_VAR_DIR": str(tmp_path / "var"),
        "ATLAS_RUNTIME_DIR": str(tmp_path / "runtime"),
        "ATLAS_RELEASE_ROOT": str(tmp_path / "opt/releases/basic/2026.05.10-001"),
        "ATLAS_HOST_FILE": str(host),
        "ATLAS_ARTIFACT_NAME": "sample",
        "ATLAS_ARTIFACT_TYPE": "command",
        "ATLAS_RELEASE_NAME": "basic",
        "ATLAS_RELEASE_VERSION": "2026.05.10-001",
        "ATLAS_RUN_ID": "run-id",
        "ATLAS_PARENT_RUN_ID": "parent-id",
        "ATLAS_OPERATION_ID": "operation-id",
    }


def test_get_context_from_env_mapping_returns_host_paths_and_artifact(tmp_path: Path) -> None:
    env = _context_env(tmp_path)

    ctx = get_context(env=env)

    assert ctx.host.name == "test-host"
    assert ctx.host.site == "site-a"
    assert ctx.paths.home == tmp_path / "opt"
    assert ctx.artifact.name == "sample"
    assert ctx.artifact.artifact_type == "command"
    assert ctx.artifact.release_name == "basic"
    assert ctx.artifact.version == "2026.05.10-001"
    assert ctx.artifact.release_root == tmp_path / "opt/releases/basic/2026.05.10-001"
    assert ctx.artifact.run_id == "run-id"
    assert ctx.artifact.parent_run_id == "parent-id"
    assert ctx.artifact.operation_id == "operation-id"
    assert ctx.paths.release_root == ctx.artifact.release_root


@pytest.mark.parametrize(
    "key",
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
def test_missing_required_artifact_environment_fails(tmp_path: Path, key: str) -> None:
    env = _context_env(tmp_path)
    env.pop(key)

    with pytest.raises(RuntimeError, match=f"{key} is required"):
        get_context(env=env)


def test_empty_parent_run_id_represents_a_root_run(tmp_path: Path) -> None:
    env = _context_env(tmp_path)
    env["ATLAS_PARENT_RUN_ID"] = ""

    assert get_context(env=env).artifact.parent_run_id is None


def test_to_dict_is_json_friendly(tmp_path: Path) -> None:
    ctx = get_context(env=_context_env(tmp_path))

    data = ctx.to_dict()

    assert data["host"]["name"] == "test-host"
    assert data["artifact"]["release_root"] == str(ctx.artifact.release_root)
    assert data["artifact"]["parent_run_id"] == "parent-id"
    assert data["paths"]["release_root"] == str(ctx.paths.release_root)
    json.dumps(data)
