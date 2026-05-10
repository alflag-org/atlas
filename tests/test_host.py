from __future__ import annotations

from pathlib import Path

import pytest

from atlas_core.host import get_host


def test_get_host_reads_host_profile(tmp_path: Path) -> None:
    p = tmp_path / "host.yml"
    p.write_text(
        """name: n1
site: tokyo
zone: mgmt
role: dns
environment: home
runtime_kind: lxc
tags:
  - sample
""",
        encoding="utf-8",
    )
    host = get_host(str(p))
    assert host.name == "n1"
    assert host.site == "tokyo"
    assert host.tags == ["sample"]


def test_get_host_applies_defaults(tmp_path: Path) -> None:
    p = tmp_path / "host.yml"
    p.write_text("name: n1\n", encoding="utf-8")
    host = get_host(str(p))
    assert host.zone == ""
    assert host.tags == []


def test_get_host_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        get_host(str(tmp_path / "missing.yml"))
