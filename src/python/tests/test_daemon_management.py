from __future__ import annotations

import json
import os
import pwd
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from atlas.cli import main
from atlas.daemon.config import load_instance, trusted_file
from atlas.daemon.systemd import Systemd, render_unit


@pytest.fixture
def declaration(tmp_path, monkeypatch):
    real_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        # The test host maps root-owned mount ancestors to the overflow UID.
        if path in (Path("/"), Path("/tmp")):
            values = list(info)
            values[4] = 0
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    directory = tmp_path / "daemons.d"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("ATLAS_ETC_DIR", str(tmp_path))
    data = {
        "schema": "atlas.daemon-instance/v1",
        "name": "sampled",
        "executable": "/bin/true",
        "arguments": [],
        "user": pwd.getpwuid(os.getuid()).pw_name,
        "working_directory": str(tmp_path),
    }
    path = directory / "sampled.yml"
    path.write_text(yaml.safe_dump(data))
    path.chmod(0o600)
    return directory, path, data


def test_unit_keeps_arguments_literal_and_instances_isolated(declaration):
    directory, _, _ = declaration
    first = load_instance(directory, "sampled", check_resources=True)
    second = replace(
        first,
        name="other",
        notify=False,
        arguments=("a b", "$HOME", "%n", '"', "\\", ""),
        environment_files=("/etc/sample%name.env",),
    )
    unit = render_unit(second)
    assert 'ExecStart="/bin/true" "a b" "$$HOME" "%%n" "\\"" "\\\\" ""' in unit
    assert 'EnvironmentFile="/etc/sample%%name.env"' in unit
    assert "Type=exec" in unit and "NotifyAccess=" not in unit
    assert "RuntimeDirectory=atlas/other" in unit
    assert "StateDirectory=atlas/sampled" in render_unit(first)
    assert "NotifyAccess=main" in render_unit(first)


@pytest.mark.parametrize(
    "patch",
    [
        {"schema": "unknown"},
        {"name": "other"},
        {"extra": True},
        {"executable": "relative"},
        {"working_directory": "/trailing "},
        {"executable": "/a/../bin/true"},
        {"arguments": "--option"},
        {"arguments": [1]},
        {"arguments": ["\n[Service]"]},
        {"user": "0"},
        {"user": "no-such-atlas-account"},
        {"notify": "yes"},
        {"restart": "sometimes"},
        {"stop_timeout_seconds": True},
        {"startup_timeout_seconds": float("inf")},
        {"restart_delay_seconds": 0},
        {"environment_files": ["relative"]},
    ],
)
def test_invalid_declarations_cannot_generate_units(declaration, patch):
    directory, path, data = declaration
    path.write_text(yaml.safe_dump(data | patch))
    with pytest.raises((TypeError, ValueError)):
        load_instance(directory, "sampled")


def test_configuration_and_resource_validation(declaration):
    directory, path, data = declaration
    path.write_text("secret: [\n")
    with pytest.raises(ValueError, match="invalid daemon YAML"):
        load_instance(directory, "sampled")
    for invalid in ([], {"schema": "atlas.daemon-instance/v1", "name": "sampled"}):
        path.write_text(yaml.safe_dump(invalid))
        with pytest.raises(ValueError):
            load_instance(directory, "sampled")
    for name in ("../sampled", "A", "", None):
        with pytest.raises(ValueError):
            load_instance(directory, name)
    for patch in ({"executable": "/does-not-exist"}, {"working_directory": str(path)}):
        path.write_text(yaml.safe_dump(data | patch))
        with pytest.raises(ValueError):
            load_instance(directory, "sampled", check_resources=True)
        # Status/stop remain available after application files disappear.
        assert load_instance(directory, "sampled").name == "sampled"
    env = directory / "sample.env"
    env.write_text("SECRET=private\n")
    env.chmod(0o600)
    path.write_text(yaml.safe_dump(data | {"environment_files": [str(env)]}))
    assert load_instance(
        directory, "sampled", check_resources=True
    ).environment_files == (str(env),)
    assert load_instance(directory, "sampled").environment_files == (str(env),)
    env.chmod(0o666)
    with pytest.raises(ValueError, match="owner-controlled"):
        load_instance(directory, "sampled", check_resources=True)


def test_untrusted_configuration_paths(declaration, tmp_path, monkeypatch):
    directory, path, _ = declaration
    path.chmod(0o666)
    with pytest.raises(ValueError, match="owner-controlled"):
        trusted_file(path)
    path.chmod(0o600)
    directory.chmod(0o777)
    with pytest.raises(ValueError, match="owner controlled"):
        trusted_file(path)
    directory.chmod(0o700)
    link = tmp_path / "linked"
    link.symlink_to(directory)
    with pytest.raises(ValueError, match="symlink"):
        trusted_file(link / path.name)
    monkeypatch.setattr("atlas.daemon.config.os.geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ValueError, match="owner controlled"):
        trusted_file(path)


def test_install_reload_rollback_and_remove_guards(declaration, tmp_path, monkeypatch):
    directory, _, _ = declaration
    instance = load_instance(directory, "sampled")
    units = tmp_path / "units"
    units.mkdir(mode=0o700)
    adapter = Systemd(units)
    call = Mock(return_value="")
    monkeypatch.setattr(adapter, "_systemctl", call)
    adapter.install(instance)
    unit = units / instance.unit
    original = unit.read_text()
    assert original == render_unit(instance)
    assert unit.stat().st_mode & 0o777 == 0o644
    assert call.call_args_list == [(("daemon-reload",), {})]
    call.side_effect = [RuntimeError("reload failed"), ""]
    with pytest.raises(RuntimeError, match="reload failed"):
        adapter.install(replace(instance, arguments=("changed",)))
    assert unit.read_text() == original
    call.side_effect = [FileNotFoundError("systemctl unavailable"), ""]
    with pytest.raises(FileNotFoundError):
        adapter.install(replace(instance, arguments=("changed",)))
    assert unit.read_text() == original
    with monkeypatch.context() as context:
        context.setattr(
            "atlas.daemon.systemd.os.replace",
            Mock(side_effect=OSError("interrupted write")),
        )
        with pytest.raises(OSError):
            adapter.install(instance)
    assert unit.read_text() == original
    assert not list(units.glob(".atlas-unit-*"))
    call.side_effect = None
    call.return_value = "ActiveState=active\nUnitFileState=disabled\n"
    with pytest.raises(ValueError, match="stop"):
        adapter.install(instance, remove=True)
    call.return_value = "ActiveState=inactive\nUnitFileState=enabled\n"
    with pytest.raises(ValueError, match="disable"):
        adapter.install(instance, remove=True)
    call.return_value = "ActiveState=inactive\nUnitFileState=disabled\n"
    adapter.install(instance, remove=True)
    assert not unit.exists()
    call.side_effect = [RuntimeError("reload failed"), ""]
    with pytest.raises(RuntimeError):
        adapter.install(instance)
    assert not unit.exists()
    assert not list(units.glob(".atlas-unit-*"))


def test_install_refuses_unmanaged_or_unsafe_targets(
    declaration, tmp_path, monkeypatch
):
    directory, _, _ = declaration
    instance = load_instance(directory, "sampled")
    units = tmp_path / "units"
    units.mkdir(mode=0o700)
    adapter = Systemd(units)
    unit = units / instance.unit
    unit.write_text("unmanaged\n")
    unit.chmod(0o600)
    monkeypatch.setattr(adapter, "_systemctl", Mock())
    with pytest.raises(ValueError, match="unmanaged"):
        adapter.install(instance)
    unit.unlink()
    unit.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="regular file"):
        adapter.install(instance)
    units.chmod(0o777)
    with pytest.raises(ValueError, match="directory"):
        adapter.install(instance)
    units.chmod(0o700)


def test_systemd_actions_and_status_do_not_infer_health(declaration, monkeypatch):
    directory, _, _ = declaration
    instance = load_instance(directory, "sampled")
    adapter = Systemd()
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [],
            0,
            "LoadState=loaded\nActiveState=active\nType=notify\nUnitFileState=enabled\n",
            "",
        )
    )
    monkeypatch.setattr(subprocess, "run", run)
    status = adapter.status(instance)
    assert status == {
        "schema": "atlas.daemon-status/v1",
        "name": "sampled",
        "installed": True,
        "active": "active",
        "enabled": "enabled",
        "ready": True,
        "health": "unknown",
    }
    for action in ("start", "stop", "restart", "enable", "disable"):
        adapter.action(instance, action)
        assert run.call_args.args[0] == [
            "systemctl",
            "--no-ask-password",
            action,
            instance.unit,
        ]
    with pytest.raises(ValueError):
        adapter.action(instance, "arbitrary")
    run.return_value = subprocess.CompletedProcess([], 0, "Type=exec\n", "")
    assert adapter.status(instance)["ready"] is None
    assert adapter.logs(instance, "yesterday") == 0
    assert run.call_args.args[0][-2:] == ["--since", "yesterday"]
    assert adapter.logs(instance, None) == 0
    run.return_value = subprocess.CompletedProcess([], 1, "", "secret provider error")
    with pytest.raises(RuntimeError, match="exit 1") as error:
        adapter.action(instance, "start")
    assert "secret" not in str(error.value)


def test_management_cli_contract(declaration, monkeypatch, capsys):
    _, path, _ = declaration
    adapter = Mock(spec=Systemd)
    adapter.status.return_value = {"schema": "atlas.daemon-status/v1", "ready": True}
    adapter.logs.return_value = 5
    monkeypatch.setattr("atlas.daemon.cli.Systemd", lambda: adapter)
    journal = Mock()
    monkeypatch.setattr("syslog.syslog", journal)
    assert main(["daemon", "list"]) == 0
    assert capsys.readouterr().out == "sampled\n"
    assert main(["daemon", "inspect", "sampled", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "atlas.daemon-instance/v1"
    assert main(["daemon", "validate", "sampled"]) == 0
    assert "valid: True" in capsys.readouterr().out
    for action in ("install", "start", "remove"):
        assert main(["daemon", action, "sampled"]) == 0
        assert json.loads(journal.call_args.args[1])["action"] == action
    assert main(["daemon", "logs", "sampled"]) == 5
    assert main(["daemon", "status", "sampled", "--json"]) == 0
    capsys.readouterr()
    adapter.status.side_effect = RuntimeError("failure")
    assert main(["daemon", "status", "sampled", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "operation_failed"
    path.write_text("schema: bad\n")
    assert main(["daemon", "inspect", "sampled", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_configuration"
    assert main(["daemon", "validate", "sampled"]) == 2
    assert "schema" in capsys.readouterr().err
