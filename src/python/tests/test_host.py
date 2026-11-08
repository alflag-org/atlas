from __future__ import annotations

from pathlib import Path

import pytest

from atlas_core.host import get_host, parse_host


def test_loads_versioned_host_profile(tmp_path: Path) -> None:
    path = tmp_path / "host.yml"
    path.write_text(
        "version: 1\nhost:\n  id: control01\n  role: control\n  site: west\n",
        encoding="utf-8",
    )

    host = get_host(path)

    assert host.id == "control01"
    assert host.to_dict()["site"] == "west"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "host.yml must be a mapping"),
        ({"version": 2, "host": {"id": "x"}}, "version must be 1"),
        ({"version": 1}, "host must be a mapping"),
        ({"version": 1, "host": {}}, "host.id is required"),
        ({"version": 1, "host": {"id": "x", "tags": []}}, "unknown key"),
        ({"version": 1, "host": {"id": "x", "role": 1}}, "role must be a string"),
        ({"version": 1, "host": {"id": "x"}, "secret": "bad"}, "unknown key"),
    ],
)
def test_rejects_invalid_host(document: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        parse_host(document)


def test_missing_host_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="host profile not found"):
        get_host(tmp_path / "missing.yml")


def test_duplicate_host_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "host.yml"
    path.write_text("version: 1\nhost:\n  id: one\n  id: two\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML key"):
        get_host(path)
