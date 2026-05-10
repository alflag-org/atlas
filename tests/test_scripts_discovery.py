from __future__ import annotations

from pathlib import Path

import pytest

from atlas.scripts import discover_commands, install_release


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


def test_install_rejects_release_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    commands = source / "commands"
    modules = source / "modules"
    commands.mkdir(parents=True)
    modules.mkdir(parents=True)
    (source / "VERSION").write_text("2026.05.10-001\n", encoding="utf-8")
    _touch(commands / "sample.py")
    target = source / "target.txt"
    target.write_text("x", encoding="utf-8")
    (modules / "bad-link.py").symlink_to(target)

    with pytest.raises(ValueError, match="symlink is not allowed"):
        install_release(source, tmp_path / "releases", tmp_path / "current")


def test_install_overwrites_same_version_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    commands = source / "commands"
    commands.mkdir(parents=True)
    (source / "VERSION").write_text("2026.05.10-001\n", encoding="utf-8")
    (commands / "sample.py").write_text("print('v1')\n", encoding="utf-8")

    releases = tmp_path / "releases"
    current = tmp_path / "current"
    install_release(source, releases, current)
    target = releases / "2026.05.10-001"
    assert target.exists()
    assert current.resolve() == target
    assert (target / "commands/sample.py").read_text(encoding="utf-8") == "print('v1')\n"

    (commands / "sample.py").write_text("print('v2')\n", encoding="utf-8")
    install_release(source, releases, current)
    assert current.resolve() == target
    assert (target / "commands/sample.py").read_text(encoding="utf-8") == "print('v2')\n"
