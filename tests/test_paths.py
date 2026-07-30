from __future__ import annotations

from pathlib import Path

import pytest

from atlas_core.paths import get_paths


def test_get_paths_from_env_returns_expected_paths(tmp_path: Path) -> None:
    env = {
        "ATLAS_HOME": str(tmp_path / "opt/atlas"),
        "ATLAS_ETC_DIR": str(tmp_path / "etc/atlas"),
        "ATLAS_VAR_DIR": str(tmp_path / "var/lib/atlas"),
        "ATLAS_RUNTIME_DIR": str(tmp_path / "runtime"),
        "ATLAS_TMP_DIR": str(tmp_path / "atlas-tmp"),
        "ATLAS_RELEASE_ROOT": str(tmp_path / "opt/atlas/releases/basic/1.0.0"),
    }

    paths = get_paths(env=env)

    assert paths.home == tmp_path / "opt/atlas"
    assert paths.etc == tmp_path / "etc/atlas"
    assert paths.var == tmp_path / "var/lib/atlas"
    assert paths.runtime == tmp_path / "runtime"
    assert paths.tmp == tmp_path / "atlas-tmp"
    assert paths.releases_root == tmp_path / "opt/atlas/releases"
    assert paths.current_root == tmp_path / "opt/atlas/current"
    assert paths.release_root == tmp_path / "opt/atlas/releases/basic/1.0.0"
    assert paths.logs == tmp_path / "var/lib/atlas/logs"
    assert paths.locks == tmp_path / "var/lib/atlas/locks"
    assert paths.cache == tmp_path / "var/lib/atlas/cache"


def test_tmp_defaults_under_atlas_home(tmp_path: Path) -> None:
    home = tmp_path / "opt/atlas"

    assert get_paths(
        env={"ATLAS_HOME": str(home), "ATLAS_RELEASE_ROOT": str(tmp_path / "release")}
    ).tmp == home / "tmp"


def test_config_and_host_files_default_under_etc(tmp_path: Path) -> None:
    etc = tmp_path / "etc/atlas"
    paths = get_paths(
        env={"ATLAS_ETC_DIR": str(etc), "ATLAS_RELEASE_ROOT": str(tmp_path / "release")}
    )

    assert paths.config_file == etc / "config.yml"
    assert paths.host_file == etc / "host.yml"


def test_host_file_can_read_atlas_host_file(tmp_path: Path) -> None:
    host_file = tmp_path / "custom-host.yml"
    paths = get_paths(
        env={
            "ATLAS_HOST_FILE": str(host_file),
            "ATLAS_RELEASE_ROOT": str(tmp_path / "release"),
        }
    )

    assert paths.host_file == host_file


def test_release_root_reads_final_environment_variable(tmp_path: Path) -> None:
    release_root = tmp_path / "release"

    assert get_paths(env={"ATLAS_RELEASE_ROOT": str(release_root)}).release_root == release_root


def test_release_root_is_required() -> None:
    with pytest.raises(RuntimeError, match="ATLAS_RELEASE_ROOT is required"):
        get_paths(env={})


def test_to_dict_converts_path_values_to_strings(tmp_path: Path) -> None:
    paths = get_paths(
        env={
            "ATLAS_HOME": str(tmp_path / "home"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
            "ATLAS_RELEASE_ROOT": str(tmp_path / "release"),
        }
    )

    data = paths.to_dict()

    assert data["home"] == str(tmp_path / "home")
    assert data["tmp"] == str(tmp_path / "home/tmp")
    assert data["releases_root"] == str(tmp_path / "home/releases")
    assert data["current_root"] == str(tmp_path / "home/current")
    assert data["release_root"] == str(tmp_path / "release")
    assert all(isinstance(value, str) for value in data.values())
