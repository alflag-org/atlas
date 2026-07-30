from __future__ import annotations

import io
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from atlas.manifests import load_manifest
from atlas_operations.config_project import (
    inventory_path,
    playbook_path,
    project_config,
    report_error,
    run_native,
    target_name,
)


OPERATIONS = Path("operations").resolve()


def _load_command(name: str):
    namespace = runpy.run_path(str(OPERATIONS / "commands" / f"{name}.py"))
    return namespace["main"]


def _load_job(name: str):
    namespace = runpy.run_path(str(OPERATIONS / "jobs" / f"{name}.py"))
    return namespace["main"]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "provisioning"
    (project / "playbooks").mkdir(parents=True)
    (project / "inventories/default").mkdir(parents=True)
    (project / "ansible.cfg").write_text("[defaults]\ninventory=inventory.yml\n", encoding="utf-8")
    (project / "playbooks/site.yml").write_text("---\n- hosts: all\n", encoding="utf-8")
    (project / "inventories/default/hosts.yml").write_text(
        "all:\n  hosts:\n    fixture:\n",
        encoding="utf-8",
    )
    return project


def test_operations_release_manifest_is_valid() -> None:
    manifest = load_manifest(OPERATIONS)
    assert manifest.name == "operations"
    assert list(manifest.commands) == [
        "config-validate",
        "config-check",
        "config-diff",
        "config-apply",
        "inventory-show",
        "config-diff-many",
    ]
    assert list(manifest.jobs) == ["inventory-refresh"]
    assert manifest.jobs["inventory-refresh"].default_timeout_seconds == 300
    service = manifest.services["inventory-refresh"]
    assert service.job == "inventory-refresh"
    assert service.systemd.service.name == "inventory-refresh.service"
    assert service.systemd.timer is not None
    assert service.systemd.timer.name == "inventory-refresh.timer"
    assert (OPERATIONS / "requirements.txt").read_text(encoding="utf-8").startswith("ansible-core")


@pytest.mark.parametrize(
    ("command_name", "argv", "expected_executable", "expected_args"),
    [
        (
            "config-validate",
            ["site"],
            "ansible-playbook",
            ["playbooks/site.yml", "--syntax-check"],
        ),
        (
            "config-check",
            ["site", "web01"],
            "ansible-playbook",
            ["playbooks/site.yml", "--limit", "web01", "--check"],
        ),
        (
            "config-diff",
            ["site", "web01"],
            "ansible-playbook",
            ["playbooks/site.yml", "--limit", "web01", "--check", "--diff"],
        ),
        (
            "config-apply",
            ["site", "web01"],
            "ansible-playbook",
            ["playbooks/site.yml", "--limit", "web01"],
        ),
        (
            "inventory-show",
            [],
            "ansible-inventory",
            ["--graph"],
        ),
    ],
)
def test_primitive_commands_delegate_exact_argv(
    command_name: str,
    argv: list[str],
    expected_executable: str,
    expected_args: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        returncode = 7

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr("atlas_operations.config_project.subprocess.run", fake_run)
    main = _load_command(command_name)

    assert main(argv) == 7
    command, kwargs = calls[0]
    assert command == [expected_executable, *expected_args]
    assert kwargs["cwd"] == project
    assert kwargs["check"] is False
    assert kwargs["shell"] is False
    assert kwargs["env"]["ANSIBLE_CONFIG"] == str(project / "ansible.cfg")


def test_project_validation_and_native_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="ansible.cfg not found"):
        project_config(project)
    (project / "ansible.cfg").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid playbook name"):
        playbook_path(project, "../site")
    with pytest.raises(ValueError, match="playbook not found"):
        playbook_path(project, "site")
    with pytest.raises(ValueError, match="invalid site name"):
        inventory_path(project, "../default")
    with pytest.raises(ValueError, match="inventory not found"):
        inventory_path(project, "default")
    with pytest.raises(ValueError, match="target must not be empty"):
        target_name(" ")
    assert target_name("web01:&linux") == "web01:&linux"

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("atlas_operations.config_project.subprocess.run", missing)
    assert run_native("ansible-playbook", [], project) == 127
    assert "command not found" in capsys.readouterr().err
    assert report_error(ValueError("bad input")) == 2
    assert "bad input" in capsys.readouterr().err


def test_primitive_command_reports_project_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    project = tmp_path / "empty"
    project.mkdir()
    monkeypatch.chdir(project)
    main = _load_command("config-apply")
    assert main(["../site", "web01"]) == 2
    assert "invalid playbook name" in capsys.readouterr().err

    main = _load_command("inventory-show")
    assert main([]) == 2
    assert "ansible.cfg not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command_name", "argv"),
    [
        ("config-validate", ["site"]),
        ("config-check", ["site", "web01"]),
        ("config-diff", ["site", "web01"]),
    ],
)
def test_other_primitive_commands_report_missing_playbook(
    command_name: str,
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    project = tmp_path / "empty"
    project.mkdir()
    monkeypatch.chdir(project)
    assert _load_command(command_name)(argv) == 2
    assert "playbook not found" in capsys.readouterr().err


def test_config_diff_many_orders_deduplicates_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    return_codes = iter([3, 0, 5])

    class Process:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(command, **kwargs):
        assert kwargs == {"check": False, "shell": False}
        calls.append(command)
        return Process(next(return_codes))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO("web02\n\nweb03\nweb01\n"))
    main = _load_command("config-diff-many")

    assert main(["site", "web01", "web02"]) == 3
    assert calls == [
        ["config-diff", "site", "web01"],
        ["config-diff", "site", "web02"],
        ["config-diff", "site", "web03"],
    ]
    assert capsys.readouterr().err.splitlines() == [
        "==> web01 <==",
        "==> web02 <==",
        "==> web03 <==",
    ]


def test_config_diff_many_handles_no_targets_and_missing_child(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    main = _load_command("config-diff-many")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["site"]) == 2
    assert "at least one target" in capsys.readouterr().err

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", missing)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["site", "web01", "web02"]) == 127
    errors = capsys.readouterr().err
    assert errors.count("config-diff command not found") == 2


def test_config_diff_many_skips_stdin_for_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

        def read(self, *args, **kwargs):
            raise AssertionError("terminal stdin must not be read")

    calls: list[list[str]] = []

    class Process:
        returncode = 0

    monkeypatch.setattr(sys, "stdin", Terminal("ignored"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or Process(),
    )
    main = _load_command("config-diff-many")
    assert main(["site", "web01"]) == 0
    assert calls == [["config-diff", "site", "web01"]]


def test_inventory_refresh_job_delegates_exact_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        returncode = 0

    monkeypatch.setattr(
        "atlas_operations.config_project.subprocess.run",
        lambda command, **kwargs: calls.append((command, kwargs)) or Process(),
    )

    assert _load_job("inventory-refresh")(["--site", "default"]) == 0
    command, kwargs = calls[0]
    assert command == [
        "ansible-inventory",
        "-i",
        "inventories/default/hosts.yml",
        "--graph",
        "--flush-cache",
    ]
    assert kwargs["cwd"] == project
    assert kwargs["check"] is False
    assert kwargs["shell"] is False


def test_inventory_refresh_job_reports_invalid_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    assert _load_job("inventory-refresh")(["--site", "missing"]) == 2
    assert "inventory not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command_name",
    [
        "config-validate",
        "config-check",
        "config-diff",
        "config-apply",
        "inventory-show",
        "config-diff-many",
    ],
)
def test_operation_script_entrypoints_show_help(
    command_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [command_name, "--help"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(OPERATIONS / "commands" / f"{command_name}.py"),
            run_name="__main__",
        )
    assert error.value.code == 0


def test_operation_job_entrypoint_shows_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["inventory-refresh", "--help"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(OPERATIONS / "jobs/inventory-refresh.py"),
            run_name="__main__",
        )
    assert error.value.code == 0
