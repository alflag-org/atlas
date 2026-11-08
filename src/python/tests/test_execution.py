from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from atlas.catalog import command_index
from atlas.config import AtlasConfig, ProgramConfig, ProgramRuntime, RuntimeConfig
from atlas.environments import create_environment
from atlas.execution import execute
from atlas.paths import ensure_dirs, get_paths

from .support import install_python_sdk, write_native_command, write_python_command


def prepare(tmp_path: Path, *, native: bool = False) -> tuple[Path, object, object]:
    home = tmp_path / "opt"
    etc = tmp_path / "etc"
    var = tmp_path / "var"
    paths = get_paths({
        "ATLAS_HOME": str(home),
        "ATLAS_ETC_DIR": str(etc),
        "ATLAS_VAR_DIR": str(var),
        "ATLAS_RUNTIMES_DIR": str(home / "runtimes"),
        "ATLAS_ENVIRONMENTS_DIR": str(home / "environments"),
    })
    ensure_dirs(paths)
    paths.host_file.parent.mkdir(parents=True, exist_ok=True)
    paths.host_file.write_text("version: 1\nhost:\n  id: exec-host\n  site: lab\n", encoding="utf-8")
    root = tmp_path / ("native" if native else "python")
    if native:
        write_native_command(root, body="#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
        runtime = ProgramRuntime("native")
    else:
        write_python_command(
            root,
            body=(
                "from atlas_core import get_context\n"
                "import json, os, sys\n"
                "context = get_context()\n"
                "print(json.dumps({'host': context.host.id, 'program': context.program.name, 'command': context.command.name, 'args': sys.argv[1:], 'context_exists': os.path.exists(os.environ['ATLAS_CONTEXT_FILE'])}))\n"
                "if sys.argv[1:] == ['fail']:\n"
                "    raise SystemExit(7)\n"
            ),
        )
        runtime = ProgramRuntime(
            "python",
            version=f"{sys.version_info.major}.{sys.version_info.minor}",
            environment="python",
        )
    program = ProgramConfig("tool", root, runtime)
    config = AtlasConfig(
        tmp_path / "config.yml",
        {"python": RuntimeConfig(f"{sys.version_info.major}.{sys.version_info.minor}", Path(sys.executable))},
        {"tool": program},
    )
    return paths, config, command_index(config)["sample" if not native else "native"]


def test_python_execution_records_context_and_exit_code(tmp_path: Path) -> None:
    paths, config, command = prepare(tmp_path)
    install_python_sdk(create_environment(paths, config, config.programs["tool"]))

    assert execute(paths, config, command, ["one", "two"]) == 0

    record = json.loads(paths.run_log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["host_id"] == "exec-host"
    assert record["program"] == "tool"
    assert record["command"] == "sample"
    assert record["exit_code"] == 0
    assert record["duration_ms"] >= 0
    assert list(paths.context_dir.iterdir()) == []


def test_native_execution_does_not_use_python(tmp_path: Path) -> None:
    paths, config, command = prepare(tmp_path, native=True)

    assert execute(paths, config, command, ["hello", "native"]) == 0
    record = json.loads(paths.run_log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["command_type"] == "native"


def test_child_failure_and_timeout_are_recorded(tmp_path: Path, monkeypatch) -> None:
    paths, config, command = prepare(tmp_path)
    install_python_sdk(create_environment(paths, config, config.programs["tool"]))
    assert execute(paths, config, command, ["fail"]) == 7

    slow = command.path
    slow.write_text(
        "import time\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    assert execute(paths, config, command, [], timeout_seconds=1) == 124
    records = [json.loads(line) for line in paths.run_log.read_text(encoding="utf-8").splitlines()]
    assert records[-2]["exit_code"] == 7
    assert records[-1]["timed_out"] is True
    monkeypatch.setenv("ATLAS_RUN_ID", "parent")
    monkeypatch.setenv("ATLAS_OPERATION_ID", "operation")
    slow.write_text("print('done')\n", encoding="utf-8")
    assert execute(paths, config, command, []) == 0
    last = json.loads(paths.run_log.read_text(encoding="utf-8").splitlines()[-1])
    assert last["parent_run_id"] == "parent"
    assert last["operation_id"] == "operation"


def test_execution_validates_cwd_timeout_and_log_path(tmp_path: Path) -> None:
    paths, config, command = prepare(tmp_path)
    install_python_sdk(create_environment(paths, config, config.programs["tool"]))
    with pytest.raises(ValueError, match="working directory"):
        execute(paths, config, command, [], cwd=tmp_path / "missing")
    with pytest.raises(ValueError, match="positive integer"):
        execute(paths, config, command, [], timeout_seconds=0)
    paths.logs.rmdir()
    paths.logs.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="logs path"):
        execute(paths, config, command, [])


@pytest.mark.parametrize("file_available", [True, False])
def test_application_logging_preserves_output_status_and_run_record(tmp_path, capfd, file_available):
    paths, config, command = prepare(tmp_path)
    install_python_sdk(create_environment(paths, config, config.programs["tool"]))
    log_path = tmp_path / ("app.log" if file_available else "missing/app.log")
    command.path.write_text(
        "import sys\n"
        "from atlas_core.logging import configure_logging, get_logger\n"
        f"configure_logging('hermes', path={str(log_path)!r})\n"
        "print('result-json')\n"
        "print('raw-stderr', file=sys.stderr)\n"
        "get_logger('hermes.dns').info('application-diagnostic')\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    assert execute(paths, config, command, ["private-argument"]) == 7
    output = capfd.readouterr()
    assert output.out == "result-json\n"
    assert output.err.count("application-diagnostic") == 1
    assert "raw-stderr" in output.err
    record_text = paths.run_log.read_text()
    record = json.loads(record_text)
    assert record["exit_code"] == 7
    assert record["program"] == "tool"
    assert f'run_id={record["run_id"]}' in output.err
    assert f'operation_id={record["operation_id"]}' in output.err
    assert "host_id=exec-host" in output.err
    assert all(value not in record_text for value in (
        "result-json", "raw-stderr", "application-diagnostic", "private-argument",
    ))
    if file_available:
        content = log_path.read_text()
        assert content.count("application-diagnostic") == 1
        assert "raw-stderr" not in content and "result-json" not in content
    else:
        assert "file output unavailable" in output.err
        assert not log_path.parent.exists()
