from __future__ import annotations

from pathlib import Path

import pytest

from atlas.scripts import discover_commands


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('x')\n", encoding="utf-8")


def test_discovery_names(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    _touch(commands / "sample.py")
    _touch(commands / "group" / "nested-sample.py")
    found = discover_commands(commands)
    assert [c.name for c in found] == ["group-nested-sample", "sample"]


def test_discovery_conflict(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    _touch(commands / "foo" / "bar.py")
    _touch(commands / "foo-bar.py")
    with pytest.raises(ValueError, match="conflict"):
        discover_commands(commands)


def test_discovery_rejects_invalid_stem(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    _touch(commands / "foo_bar.py")
    with pytest.raises(ValueError, match="invalid"):
        discover_commands(commands)


def test_discovery_rejects_invalid_segment(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    _touch(commands / "Foo" / "bar.py")
    with pytest.raises(ValueError, match="invalid"):
        discover_commands(commands)


def test_discovery_rejects_reserved_name(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    _touch(commands / "atlas.py")
    with pytest.raises(ValueError, match="reserved"):
        discover_commands(commands)
