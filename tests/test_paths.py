from __future__ import annotations

from pathlib import Path

from atlas_core.paths import get_paths


def test_get_paths_from_env_returns_expected_paths(tmp_path: Path) -> None:
    env = {
        "ATLAS_HOME": str(tmp_path / "opt/atlas"),
        "ATLAS_ETC_DIR": str(tmp_path / "etc/atlas"),
        "ATLAS_VAR_DIR": str(tmp_path / "var/lib/atlas"),
        "ATLAS_RUNTIME_DIR": str(tmp_path / "runtime"),
        "ATLAS_SCRIPTS_CURRENT_DIR": str(tmp_path / "scripts/current"),
        "ATLAS_SCRIPTS_DIR": str(tmp_path / "scripts/releases/basic/1.0.0"),
    }

    paths = get_paths(env=env)

    assert paths.home == tmp_path / "opt/atlas"
    assert paths.etc == tmp_path / "etc/atlas"
    assert paths.var == tmp_path / "var/lib/atlas"
    assert paths.runtime == tmp_path / "runtime"
    assert paths.scripts_root == tmp_path / "opt/atlas/scripts"
    assert paths.scripts_current_root == tmp_path / "scripts/current"
    assert paths.script_release_root == tmp_path / "scripts/releases/basic/1.0.0"
    assert paths.logs == tmp_path / "var/lib/atlas/logs"
    assert paths.cache == tmp_path / "var/lib/atlas/cache"


def test_config_and_host_files_default_under_etc(tmp_path: Path) -> None:
    etc = tmp_path / "etc/atlas"
    paths = get_paths(env={"ATLAS_ETC_DIR": str(etc)})

    assert paths.config_file == etc / "config.yml"
    assert paths.host_file == etc / "host.yml"


def test_host_file_can_read_atlas_host_file(tmp_path: Path) -> None:
    host_file = tmp_path / "custom-host.yml"
    paths = get_paths(env={"ATLAS_HOST_FILE": str(host_file)})

    assert paths.host_file == host_file


def test_script_release_root_reads_atlas_scripts_dir(tmp_path: Path) -> None:
    release_root = tmp_path / "release"

    assert get_paths(env={"ATLAS_SCRIPTS_DIR": str(release_root)}).script_release_root == release_root


def test_script_release_root_defaults_to_current_root_for_compatibility(tmp_path: Path) -> None:
    current_root = tmp_path / "current"

    assert get_paths(env={"ATLAS_SCRIPTS_CURRENT_DIR": str(current_root)}).script_release_root == current_root


def test_scripts_compatibility_property_returns_script_release_root(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    paths = get_paths(env={"ATLAS_SCRIPTS_DIR": str(release_root)})

    assert paths.scripts == paths.script_release_root


def test_to_dict_converts_path_values_to_strings(tmp_path: Path) -> None:
    paths = get_paths(
        env={
            "ATLAS_HOME": str(tmp_path / "home"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
            "ATLAS_SCRIPTS_DIR": str(tmp_path / "release"),
        }
    )

    data = paths.to_dict()

    assert data["home"] == str(tmp_path / "home")
    assert data["script_release_root"] == str(tmp_path / "release")
    assert data["scripts"] == str(tmp_path / "release")
    assert all(isinstance(value, str) for value in data.values())
