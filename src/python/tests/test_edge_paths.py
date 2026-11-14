from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

import atlas.execution as execution_module
from atlas.catalog import CommandRef
from atlas.config import AtlasConfig, ProgramConfig, ProgramRuntime
from atlas.context import child_environment, execution_context, write_context
from atlas.execution import (
    _append_run_log,
    _execution_signals,
    _exit_code,
    execute,
)
from atlas.paths import ensure_dirs, get_paths
from atlas_core._yaml import load_yaml_file


def native_command(tmp_path: Path) -> tuple[object, object, object]:
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "home"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    ensure_dirs(paths)
    paths.host_file.parent.mkdir(parents=True, exist_ok=True)
    paths.host_file.write_text("version: 1\nhost:\n  id: host\n", encoding="utf-8")
    root = tmp_path / "tool"
    path = root / "bin/run"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    program = ProgramConfig("tool", root, ProgramRuntime("native"))
    return paths, program, CommandRef("run", program, path, "native")


def test_paths_reject_traversal_and_bad_directories(tmp_path: Path) -> None:
    paths = get_paths(
        {
            "ATLAS_HOME": str(tmp_path / "home"),
            "ATLAS_ETC_DIR": str(tmp_path / "etc"),
            "ATLAS_VAR_DIR": str(tmp_path / "var"),
        }
    )
    with pytest.raises(ValueError, match="invalid runtime"):
        paths.runtime("python", "../escape")
    with pytest.raises(ValueError, match="invalid environment"):
        paths.environment("python", "../escape")
    paths.home.mkdir(parents=True)
    (paths.runtimes).write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="Atlas path"):
        ensure_dirs(paths)


def test_yaml_loader_rejects_missing_and_duplicate(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="file not found"):
        load_yaml_file(tmp_path / "missing.yml")
    path = tmp_path / "duplicate.yml"
    path.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_yaml_file(path)


def test_context_payload_python_runtime_and_regular_file_guard(tmp_path: Path, monkeypatch) -> None:
    paths, program, command = native_command(tmp_path)
    python_program = ProgramConfig(
        "python-tool",
        program.root,
        ProgramRuntime("python", version="3.13", environment="python-tool"),
    )
    payload = execution_context(
        paths,
        CommandRef("run", python_program, command.path, "python"),
        run_id="run",
        parent_run_id="parent",
        operation_id="operation",
        working_directory=tmp_path,
    )
    assert payload["program"]["runtime"] == {
        "type": "python",
        "version": "3.13",
        "environment": str(paths.environment("python", "python-tool")),
    }
    modules = program.root / "modules"
    modules.mkdir(parents=True)
    environment = child_environment(
        paths,
        CommandRef("run", python_program, command.path, "python"),
        payload,
        context_file=tmp_path / "context.json",
        run_id="run",
        parent_run_id="parent",
        operation_id="operation",
        interpreter=tmp_path / "python",
        environment_path=paths.environment("python", "python-tool"),
    )
    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(modules)

    target = tmp_path / "context.json"
    original = execution_module.stat.S_ISREG
    monkeypatch.setattr(execution_module.stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(ValueError, match="regular file"):
        write_context(target, {})
    monkeypatch.setattr(execution_module.stat, "S_ISREG", original)


def test_append_log_regular_file_guard(tmp_path: Path, monkeypatch) -> None:
    paths, _program, _command = native_command(tmp_path)
    original = execution_module.stat.S_ISREG
    monkeypatch.setattr(execution_module.stat, "S_ISREG", lambda _mode: False)
    with pytest.raises(ValueError, match="run log"):
        _append_run_log(paths, {"x": 1})
    monkeypatch.setattr(execution_module.stat, "S_ISREG", original)


class FakeProcess:
    pid = 100

    def __init__(self, waits: list[object]) -> None:
        self.waits = waits

    def wait(self, timeout: float | None = None) -> int:
        value = self.waits.pop(0)
        if isinstance(value, BaseException):
            raise value
        return int(value)


def test_signal_exit_status_conversion():
    assert _exit_code(-signal.SIGTERM) == 128 + signal.SIGTERM
    assert _exit_code(7) == 7


def test_execution_signals_defer_until_child_is_owned():
    previous = signal.getsignal(signal.SIGTERM)
    with _execution_signals() as received:
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        handler(signal.SIGINT, None)
        assert received == [signal.SIGTERM, signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == previous


def test_execution_spawn_and_signal_error_paths(tmp_path: Path, monkeypatch) -> None:
    paths, _program, command = native_command(tmp_path)
    original_popen = execution_module.subprocess.Popen

    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(execution_module.subprocess, "Popen", missing)
    assert execute(paths, AtlasConfig(paths.config_file, {}, {}), command, []) == 127
    assert json.loads(paths.run_log.read_text(encoding="utf-8").splitlines()[-1])["exit_code"] == 127

    def denied(*_args, **_kwargs):
        raise PermissionError

    monkeypatch.setattr(execution_module.subprocess, "Popen", denied)
    assert execute(paths, AtlasConfig(paths.config_file, {}, {}), command, []) == 126

    class InterruptingProcess(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise KeyboardInterrupt

    monkeypatch.setattr(execution_module.subprocess, "Popen", lambda *_args, **_kwargs: InterruptingProcess([]))
    monkeypatch.setattr(execution_module, "stop_tree", lambda *_args: None)
    assert execute(paths, AtlasConfig(paths.config_file, {}, {}), command, []) == 130

    command.path.write_text("#!/bin/sh\nrm \"$ATLAS_CONTEXT_FILE\"\n", encoding="utf-8")
    command.path.chmod(0o755)
    monkeypatch.setattr(execution_module.subprocess, "Popen", original_popen)
    assert execute(paths, AtlasConfig(paths.config_file, {}, {}), command, []) == 0
