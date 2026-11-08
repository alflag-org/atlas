from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from atlas.cli import main

from .support import configure_environment, write_native_command, write_python_command


def test_cli_covers_registration_runtime_shim_and_execution(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "program"
    write_python_command(root, "hello", body="print('hello', *(__import__('sys').argv[1:]))\n")
    native_root = tmp_path / "native"
    write_native_command(native_root, "native")
    config = (
        "runtime:\n"
        "  python:\n"
        f"    executable: {sys.executable}\n"
        "programs:\n"
        "  python-tool:\n"
        f"    root: {root}\n"
        "    runtime:\n"
        "      type: python\n"
        "      venv: python-tool\n"
        "  native-tool:\n"
        f"    root: {native_root}\n"
        "    runtime:\n"
        "      type: native\n"
    )
    home, _etc, var = configure_environment(monkeypatch, tmp_path, config)

    assert main(["status"]) == 0
    assert "programs: 2" in capsys.readouterr().out
    assert main(["program", "list", "--verbose"]) == 0
    assert "python-tool" in capsys.readouterr().out
    assert main(["command", "list", "--verbose"]) == 0
    command_output = capsys.readouterr().out
    assert "hello\tpython-tool\tpython" in command_output
    assert "native\tnative-tool\tnative" in command_output
    assert main(["which", "hello"]) == 0
    assert capsys.readouterr().out.strip().endswith("commands/hello.py")

    assert main(["runtime", "install"]) == 0
    assert main(["venv", "create", "python-tool"]) == 0
    assert main(["venv", "list"]) == 0
    assert "ready" in capsys.readouterr().out
    assert main(["runtime", "status"]) == 0
    assert "configured\ttrue" in capsys.readouterr().out

    assert main(["shim", "generate"]) == 0
    assert (home / "shims/hello").is_file()
    capsys.readouterr()
    assert main(["run", "hello", "arg"]) == 0
    capsys.readouterr()
    assert main(["run", "native", "native-arg"]) == 0
    capsys.readouterr()
    assert main(["context"]) == 0
    context = json.loads(capsys.readouterr().out)
    assert context["host"]["id"] == "test-host"
    assert main(["check"]) == 0
    assert "ok:" in capsys.readouterr().out
    assert len((var / "logs/runs.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_cli_rejects_unknown_command_tree() -> None:
    with pytest.raises(SystemExit) as error:
        main(["unknown", "install", "/tmp/program"])
    assert error.value.code == 2


def test_cli_reports_user_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    config = "programs: {}\n"
    configure_environment(monkeypatch, tmp_path, config)
    assert main(["run", "missing"]) == 2
    assert "unknown command" in capsys.readouterr().err
    assert main(["check"]) == 0
    assert "ok:" in capsys.readouterr().out


def test_cli_non_verbose_and_check_failure_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "tool"
    write_python_command(root)
    config = (
        "runtime:\n"
        "  python:\n"
        "    version: '9.9'\n"
        "programs:\n"
        "  tool:\n"
        f"    root: {root}\n"
        "    runtime:\n"
        "      type: python\n"
        "      venv: tool\n"
    )
    configure_environment(monkeypatch, tmp_path, config)
    assert main(["program", "list"]) == 0
    assert main(["command", "list"]) == 0
    assert "sample" in capsys.readouterr().out
    assert main(["check"]) == 2
    assert "runtime unavailable" in capsys.readouterr().err

    (tmp_path / "etc/atlas/host.yml").write_text("bad: true\n", encoding="utf-8")
    assert main(["status"]) == 0
    assert "unavailable" in capsys.readouterr().out

    assert main(["venv", "create", "missing"]) == 2
    assert "unknown program" in capsys.readouterr().err


def test_cli_check_reports_command_and_host_failures(tmp_path: Path, monkeypatch, capsys) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    write_native_command(one, "sample")
    write_native_command(two, "sample")
    config = (
        "programs:\n"
        "  one:\n"
        f"    root: {one}\n"
        "    runtime:\n"
        "      type: native\n"
        "  two:\n"
        f"    root: {two}\n"
        "    runtime:\n"
        "      type: native\n"
    )
    configure_environment(monkeypatch, tmp_path, config)
    assert main(["check"]) == 2
    assert "collision" in capsys.readouterr().err
    (tmp_path / "etc/atlas/host.yml").write_text("bad: true\n", encoding="utf-8")
    assert main(["check"]) == 2
    assert "unknown key" in capsys.readouterr().err
