from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from atlas_operations.config_project import (
    playbook_path,
    project_config,
    report_error,
    run_native,
    target_name,
)

from atlas import cli
from atlas.manifests import load_manifest

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "operations"
PROVISIONING_FIXTURE = ROOT / "tests/fixtures/provisioning"


def _load_command(name: str):
    namespace = runpy.run_path(str(OPERATIONS / "commands" / f"{name}.py"))
    return namespace["main"]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "provisioning"
    (project / "playbooks").mkdir(parents=True)
    (project / "ansible.cfg").write_text(
        "[defaults]\ninventory=inventory.yml\n",
        encoding="utf-8",
    )
    (project / "playbooks/site.yml").write_text(
        "---\n- hosts: all\n",
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
    assert manifest.jobs == {}
    assert (OPERATIONS / "VERSION").read_text(encoding="utf-8") == "1.0.0\n"
    assert (OPERATIONS / "requirements.txt").read_text(encoding="utf-8").startswith(
        "ansible-core"
    )


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

    assert _load_command(command_name)(argv) == 7
    command, kwargs = calls[0]
    assert command == [expected_executable, *expected_args]
    assert kwargs["cwd"] == project
    assert kwargs["check"] is False
    assert kwargs["shell"] is False
    assert kwargs["env"]["ANSIBLE_CONFIG"] == str(project / "ansible.cfg")
    assert "capture_output" not in kwargs


def test_project_validation_rejects_missing_and_symlinked_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match=r"ansible\.cfg not found"):
        project_config(project)

    external_config = tmp_path / "ansible.cfg"
    external_config.write_text("", encoding="utf-8")
    (project / "ansible.cfg").symlink_to(external_config)
    with pytest.raises(ValueError, match=r"ansible\.cfg not found"):
        project_config(project)
    (project / "ansible.cfg").unlink()
    (project / "ansible.cfg").write_text("", encoding="utf-8")
    assert project_config(project) == project / "ansible.cfg"

    with pytest.raises(ValueError, match="invalid playbook name"):
        playbook_path(project, "../site")
    with pytest.raises(ValueError, match="playbook not found"):
        playbook_path(project, "site")

    (project / "playbooks").mkdir()
    external_playbook = tmp_path / "site.yml"
    external_playbook.write_text("---\n", encoding="utf-8")
    (project / "playbooks/site.yml").symlink_to(external_playbook)
    with pytest.raises(ValueError, match="playbook not found"):
        playbook_path(project, "site")
    (project / "playbooks/site.yml").unlink()
    (project / "playbooks/site.yml").write_text("---\n", encoding="utf-8")
    assert playbook_path(project, "site") == project / "playbooks/site.yml"

    with pytest.raises(ValueError, match="target must not be empty"):
        target_name(" ")
    assert target_name("web01:&linux") == "web01:&linux"


def test_native_missing_and_validation_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr("atlas_operations.config_project.subprocess.run", missing)
    assert run_native("ansible-playbook", [], project) == 127
    assert "ansible-playbook command not found" in capsys.readouterr().err
    assert report_error(ValueError("bad input")) == 2
    assert "bad input" in capsys.readouterr().err


def test_primitive_commands_report_validation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "empty"
    project.mkdir()
    monkeypatch.chdir(project)

    assert _load_command("config-apply")(["../site", "web01"]) == 2
    assert "invalid playbook name" in capsys.readouterr().err

    assert _load_command("inventory-show")([]) == 2
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
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "empty"
    project.mkdir()
    monkeypatch.chdir(project)

    assert _load_command(command_name)(argv) == 2
    assert "playbook not found" in capsys.readouterr().err


def test_config_apply_requires_nonempty_target_before_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Ansible must not run")

    monkeypatch.setattr("atlas_operations.config_project.subprocess.run", unexpected)
    main = _load_command("config-apply")

    assert main(["site", ""]) == 2
    assert "target must not be empty" in capsys.readouterr().err
    assert called is False
    with pytest.raises(SystemExit) as error:
        main(["site"])
    assert error.value.code == 2
    assert called is False


def test_config_diff_many_orders_deduplicates_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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

    assert _load_command("config-diff-many")(["site", "web01", "web02"]) == 3
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
    capsys: pytest.CaptureFixture[str],
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


def test_config_diff_many_skips_terminal_stdin(
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

    assert _load_command("config-diff-many")(["site", "web01"]) == 0
    assert calls == [["config-diff", "site", "web01"]]


def test_config_diff_many_runs_through_shims_with_nested_correlation(
    atlas_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ansible = atlas_paths.scripts_python.parent / "ansible-playbook"
    fake_ansible.write_text(
        "#!/bin/sh\n"
        "printf 'ansible-playbook:%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_ansible.chmod(0o755)

    assert cli.main(["scripts", "install", str(OPERATIONS)]) == 0
    monkeypatch.chdir(PROVISIONING_FIXTURE)
    process = subprocess.run(
        [
            str(atlas_paths.shims / "config-diff-many"),
            "site",
            "fixture",
        ],
        input="fixture\nsecond\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    assert process.stdout.splitlines() == [
        "ansible-playbook:playbooks/site.yml --limit fixture --check --diff",
        "ansible-playbook:playbooks/site.yml --limit second --check --diff",
    ]
    records = [
        json.loads(line)
        for line in (atlas_paths.logs / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    parent = next(record for record in records if record["artifact"] == "config-diff-many")
    children = [record for record in records if record["artifact"] == "config-diff"]
    assert parent["parent_run_id"] is None
    assert parent["operation_id"] == parent["run_id"]
    assert parent["cwd"] == str(PROVISIONING_FIXTURE)
    assert [child["args"] for child in children] == [
        ["site", "fixture"],
        ["site", "second"],
    ]
    assert all(child["parent_run_id"] == parent["run_id"] for child in children)
    assert all(child["operation_id"] == parent["operation_id"] for child in children)
    assert all(child["cwd"] == str(PROVISIONING_FIXTURE) for child in children)


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
