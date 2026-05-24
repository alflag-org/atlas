from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_core.context import get_context


def _context_env(tmp_path: Path) -> dict[str, str]:
    host = tmp_path / "host.yml"
    host.write_text("name: test-host\nsite: kng01\n", encoding="utf-8")
    return {
        "ATLAS_HOME": str(tmp_path / "opt"),
        "ATLAS_ETC_DIR": str(tmp_path / "etc"),
        "ATLAS_VAR_DIR": str(tmp_path / "var"),
        "ATLAS_RUNTIME_DIR": str(tmp_path / "runtime"),
        "ATLAS_SCRIPTS_CURRENT_DIR": str(tmp_path / "scripts/current"),
        "ATLAS_SCRIPTS_DIR": str(tmp_path / "scripts/releases/basic/2026.05.10-001"),
        "ATLAS_HOST_FILE": str(host),
        "ATLAS_SCRIPT_NAME": "sample",
        "ATLAS_SCRIPT_RELEASE_NAME": "basic-scripts",
        "ATLAS_SCRIPT_VERSION": "2026.05.10-001",
    }


def test_get_context_from_env_mapping_returns_host_paths_and_script(tmp_path: Path) -> None:
    env = _context_env(tmp_path)

    ctx = get_context(env=env)

    assert ctx.host.name == "test-host"
    assert ctx.host.site == "kng01"
    assert ctx.paths.home == tmp_path / "opt"
    assert ctx.script.name == "sample"
    assert ctx.script.release_name == "basic-scripts"
    assert ctx.script.version == "2026.05.10-001"
    assert ctx.script.release_root == tmp_path / "scripts/releases/basic/2026.05.10-001"
    assert ctx.paths.script_release_root == ctx.script.release_root


@pytest.mark.parametrize(
    "key",
    ["ATLAS_SCRIPT_NAME", "ATLAS_SCRIPT_RELEASE_NAME", "ATLAS_SCRIPTS_DIR"],
)
def test_missing_required_script_environment_fails(tmp_path: Path, key: str) -> None:
    env = _context_env(tmp_path)
    env.pop(key)

    with pytest.raises(RuntimeError, match=f"{key} is required"):
        get_context(env=env)


def test_script_version_defaults_to_empty_string(tmp_path: Path) -> None:
    env = _context_env(tmp_path)
    env.pop("ATLAS_SCRIPT_VERSION")

    assert get_context(env=env).script.version == ""


def test_to_dict_is_json_friendly(tmp_path: Path) -> None:
    ctx = get_context(env=_context_env(tmp_path))

    data = ctx.to_dict()

    assert data["host"]["name"] == "test-host"
    assert data["script"]["release_root"] == str(ctx.script.release_root)
    assert data["paths"]["script_release_root"] == str(ctx.paths.script_release_root)
    json.dumps(data)
