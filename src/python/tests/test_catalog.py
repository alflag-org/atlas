from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from atlas.catalog import _walk_files, command_index, discover_commands
from atlas.config import load_config

from .support import write_native_command, write_python_command


def config_for(*programs: tuple[str, Path, str]):
    body = "programs:\n"
    for name, root, runtime in programs:
        body += (
            f"  {name}:\n"
            f"    root: {root}\n"
            "    runtime:\n"
            f"      type: {runtime}\n"
        )
    return load_config_from_text(body)


def load_config_from_text(text: str):
    path = Path("/tmp") / f"atlas-test-{uuid.uuid4().hex}.yml"
    path.write_text(text, encoding="utf-8")
    try:
        return load_config(path)
    finally:
        path.unlink()


def test_discovers_python_nested_and_native_commands(tmp_path: Path) -> None:
    root = tmp_path / "tool"
    commands = root / "commands"
    commands.mkdir(parents=True)
    nested = commands / "group/nested"
    nested.parent.mkdir(parents=True)
    nested.write_text("#!/bin/sh\nprintf nested\n", encoding="utf-8")
    nested.chmod(0o755)
    native = write_native_command(root, "pve")
    command_native = commands / "sample"
    command_native.write_text("#!/bin/sh\nprintf sample\n", encoding="utf-8")
    command_native.chmod(0o755)
    nested_native = commands / "group/nested-command"
    nested_native.write_text("#!/bin/sh\nprintf nested-command\n", encoding="utf-8")
    nested_native.chmod(0o755)
    program = config_for(("tool", root, "native")).programs["tool"]

    commands = discover_commands(program)

    assert [(item.name, item.type) for item in commands] == [
        ("group-nested", "native"),
        ("group-nested-command", "native"),
        ("pve", "native"),
        ("sample", "native"),
    ]
    assert commands[2].path == native


def test_python_program_marks_py_files_as_python(tmp_path: Path) -> None:
    root = tmp_path / "tool"
    path = write_python_command(root)
    program = config_for(("tool", root, "python")).programs["tool"]

    commands = discover_commands(program)

    assert commands[0].name == "sample"
    assert commands[0].type == "python"
    assert commands[0].relative_path == "commands/sample.py"
    assert commands[0].path == path


def test_native_bin_discovers_only_executables(tmp_path: Path) -> None:
    root = tmp_path / "tool"
    executable = write_native_command(root, "run")
    (root / "bin/ignored").write_text("no", encoding="utf-8")
    program = config_for(("tool", root, "native")).programs["tool"]

    assert [command.path for command in discover_commands(program)] == [executable]


def test_command_index_rejects_cross_program_collision(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_python_command(one)
    write_python_command(two)
    config = config_for(("one", one, "python"), ("two", two, "python"))

    with pytest.raises(ValueError, match="collision: sample"):
        command_index(config)


def test_rejects_invalid_files_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "tool"
    commands = root / "commands"
    commands.mkdir(parents=True)
    (commands / "bad_name.py").write_text("x", encoding="utf-8")
    program = config_for(("tool", root, "python")).programs["tool"]
    with pytest.raises(ValueError, match="invalid command segment"):
        discover_commands(program)

    (commands / "bad_name.py").unlink()
    target = commands / "real.py"
    target.write_text("x", encoding="utf-8")
    (commands / "link.py").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        discover_commands(program)


def test_rejects_missing_root_and_directory_file(tmp_path: Path) -> None:
    missing = config_for(("tool", tmp_path / "missing", "native")).programs["tool"]
    with pytest.raises(ValueError, match="program root not found"):
        discover_commands(missing)

    root = tmp_path / "tool"
    root.mkdir()
    (root / "commands").write_text("not a directory", encoding="utf-8")
    program = config_for(("tool", root, "python")).programs["tool"]
    with pytest.raises(ValueError, match="commands directory must be a directory"):
        discover_commands(program)


def test_rejects_symlinked_directory_and_special_file(tmp_path: Path) -> None:
    root = tmp_path / "tool"
    commands = root / "commands"
    commands.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (commands / "linked").symlink_to(target, target_is_directory=True)
    program = config_for(("tool", root, "python")).programs["tool"]
    with pytest.raises(ValueError, match="symlink"):
        discover_commands(program)

    (commands / "linked").unlink()
    fifo = commands / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        discover_commands(program)


def test_walk_files_handles_missing_directory(tmp_path: Path) -> None:
    assert _walk_files(tmp_path / "missing") == []


def test_walk_files_rejects_symlinked_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "commands"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        _walk_files(link)


def test_native_program_without_bin_directory(tmp_path: Path) -> None:
    root = tmp_path / "tool"
    root.mkdir()
    program = config_for(("tool", root, "native")).programs["tool"]
    assert discover_commands(program) == []


def test_rejects_command_collision_inside_program(tmp_path: Path) -> None:
    root = tmp_path / "tool"
    write_python_command(root, "foo")
    nested = root / "commands/foo.py"
    nested.write_text("print('duplicate')\n", encoding="utf-8")
    (root / "commands/bar.py").write_text("print('bar')\n", encoding="utf-8")
    # The duplicate is the same relative path replacement, so use a nested path
    # that flattens to the same name instead.
    nested.unlink()
    (root / "commands/foo/bar.py").parent.mkdir(parents=True)
    (root / "commands/foo/bar.py").write_text("print('duplicate')\n", encoding="utf-8")
    (root / "commands/foo-bar.py").write_text("print('duplicate')\n", encoding="utf-8")
    program = config_for(("tool", root, "python")).programs["tool"]

    with pytest.raises(ValueError, match="collision in program"):
        discover_commands(program)
