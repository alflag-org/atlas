from __future__ import annotations

from pathlib import Path

import pytest

from atlas_core.host import HostProfile, get_host


def _write_host(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_host_yml_returns_host_profile(tmp_path: Path) -> None:
    path = _write_host(
        tmp_path / "host.yml",
        """name: n1
site: tokyo
zone: mgmt
role: dns
environment: home
runtime_kind: lxc
tags:
  - sample
""",
    )

    host = get_host(path)

    assert host == HostProfile(
        name="n1",
        site="tokyo",
        zone="mgmt",
        role="dns",
        environment="home",
        runtime_kind="lxc",
        tags=("sample",),
    )


def test_name_is_required(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "site: tokyo\n")

    with pytest.raises(ValueError, match="name is required"):
        get_host(path)


def test_empty_name_fails(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: ''\n")

    with pytest.raises(ValueError, match="name is required"):
        get_host(path)


def test_non_string_name_fails(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: 1\n")

    with pytest.raises(ValueError, match="name is required"):
        get_host(path)


def test_non_mapping_yaml_fails(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "[]\n")

    with pytest.raises(ValueError, match="mapping"):
        get_host(path)


@pytest.mark.parametrize("field", ["site", "zone", "role", "environment", "runtime_kind"])
def test_optional_scalar_fields_must_be_strings_if_present(tmp_path: Path, field: str) -> None:
    path = _write_host(tmp_path / "host.yml", f"name: n1\n{field}: 1\n")

    with pytest.raises(ValueError, match=field):
        get_host(path)


def test_tags_absent_returns_empty_tuple(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: n1\n")

    assert get_host(path).tags == ()


def test_tags_null_returns_empty_tuple(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: n1\ntags:\n")

    assert get_host(path).tags == ()


def test_tags_list_of_strings_returns_tuple(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: n1\ntags: [a, b]\n")

    assert get_host(path).tags == ("a", "b")


def test_tags_non_list_fails(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: n1\ntags: tag\n")

    with pytest.raises(ValueError, match=r"list\[str\]"):
        get_host(path)


def test_tags_list_containing_non_string_fails(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: n1\ntags: [a, 1]\n")

    with pytest.raises(ValueError, match=r"list\[str\]"):
        get_host(path)


def test_has_tag_works(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: n1\ntags: [a]\n")
    host = get_host(path)

    assert host.has_tag("a") is True
    assert host.has_tag("missing") is False


def test_to_dict_returns_json_friendly_values(tmp_path: Path) -> None:
    path = _write_host(tmp_path / "host.yml", "name: n1\nsite: tokyo\ntags: [a]\n")

    assert get_host(path).to_dict() == {
        "name": "n1",
        "site": "tokyo",
        "zone": "",
        "role": "",
        "environment": "",
        "runtime_kind": "",
        "tags": ["a"],
    }


def test_get_host_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        get_host(tmp_path / "missing.yml")
