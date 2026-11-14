from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.catalog import CommandRef
from atlas.config import ProgramConfig, ProgramRuntime
from atlas.context import execution_context, write_context
from atlas.paths import get_paths
from atlas_core import get_context


def test_get_context_reads_json_context(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "opt"
    paths = get_paths({
        "ATLAS_HOME": str(home),
        "ATLAS_ETC_DIR": str(tmp_path / "etc"),
        "ATLAS_VAR_DIR": str(tmp_path / "var"),
    })
    host_file = paths.host_file
    host_file.parent.mkdir(parents=True)
    host_file.write_text("version: 1\nhost:\n  id: host\n", encoding="utf-8")
    program = ProgramConfig("tool", tmp_path / "tool", ProgramRuntime("native"))
    command = CommandRef("run", program, program.root / "bin/run", "native")
    payload = execution_context(
        paths,
        command,
        run_id="run",
        parent_run_id=None,
        operation_id="op",
        working_directory=tmp_path,
    )
    context_file = tmp_path / "context.json"
    write_context(context_file, payload)
    monkeypatch.setenv("ATLAS_CONTEXT_FILE", str(context_file))
    monkeypatch.setenv("ATLAS_HOME", str(home))
    monkeypatch.setenv("ATLAS_ETC_DIR", str(paths.etc))
    monkeypatch.setenv("ATLAS_VAR_DIR", str(paths.var))

    context = get_context()

    assert context.host.id == "host"
    assert context.program.name == "tool"
    assert context.command.name == "run"
    assert context.execution.parent_run_id is None
    assert context.to_dict()["paths"]["home"] == str(home)
    json.dumps(context.to_dict())


def test_get_context_reads_environment_fallback(tmp_path: Path, monkeypatch) -> None:
    host = tmp_path / "host.yml"
    host.write_text("version: 1\nhost:\n  id: env-host\n", encoding="utf-8")
    env = {
        "ATLAS_HOME": str(tmp_path / "home"),
        "ATLAS_ETC_DIR": str(tmp_path / "etc"),
        "ATLAS_VAR_DIR": str(tmp_path / "var"),
        "ATLAS_HOST_FILE": str(host),
        "ATLAS_PROGRAM_NAME": "tool",
        "ATLAS_PROGRAM_ROOT": str(tmp_path / "tool"),
        "ATLAS_COMMAND_NAME": "run",
        "ATLAS_COMMAND_PATH": str(tmp_path / "tool/run"),
        "ATLAS_RUNTIME_TYPE": "native",
        "ATLAS_RUN_ID": "run",
        "ATLAS_OPERATION_ID": "op",
    }
    monkeypatch.delenv("ATLAS_CONTEXT_FILE", raising=False)

    context = get_context(env)

    assert context.host.id == "env-host"
    assert context.command.type == "native"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": 2},
        {"version": 1, "host": []},
        {"version": 1, "host": {"id": "x"}, "program": {}, "command": {}, "execution": {}},
        {"version": 1, "host": {"id": "x"}, "program": {}, "command": {}, "execution": []},
    ],
)
def test_context_rejects_invalid_json(tmp_path: Path, monkeypatch, payload: object) -> None:
    path = tmp_path / "context.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("ATLAS_CONTEXT_FILE", str(path))
    with pytest.raises((RuntimeError, TypeError, ValueError)):
        get_context()


def test_context_file_must_exist_and_write_rejects_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_CONTEXT_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="context file not found"):
        get_context()
    directory = tmp_path / "context"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        write_context(directory, {})


def test_context_rejects_missing_fields_and_non_object(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "context.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "host": {"id": "host"},
                "program": [],
                "command": {},
                "execution": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ATLAS_CONTEXT_FILE", str(path))
    with pytest.raises(TypeError, match="program and command"):
        get_context()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "host": {"id": "host"},
                "program": {"runtime": []},
                "command": {},
                "execution": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="runtime"):
        get_context()
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="object"):
        get_context()

    monkeypatch.delenv("ATLAS_CONTEXT_FILE")
    with pytest.raises(RuntimeError, match="ATLAS_PROGRAM_NAME"):
        get_context({})

    valid = {
        "version": 1,
        "host": {"id": "host"},
        "program": {
            "name": "tool",
            "root": str(tmp_path),
            "runtime": {"type": "native"},
        },
        "command": {"name": "run", "path": str(tmp_path / "run"), "type": "native"},
        "execution": {"run_id": "run", "operation_id": "op"},
    }
    path.write_text(json.dumps(valid), encoding="utf-8")
    monkeypatch.setenv("ATLAS_CONTEXT_FILE", str(path))
    with pytest.raises(RuntimeError, match="working_directory"):
        get_context()
