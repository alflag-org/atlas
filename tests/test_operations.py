from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path

import atlas_configuration_operations.child as child_module
import atlas_configuration_operations.controller as controller_module
import pytest
from atlas_configuration_operations.config_project import (
    inventory_path,
    playbook_path,
    project_config,
    report_error,
    run_native,
    target_name,
)

from atlas import cli
from atlas.manifests import load_manifest

ROOT = Path(__file__).resolve().parents[1]
CONFIGURATION_OPERATIONS = ROOT / "configuration-operations"
INFRASTRUCTURE_OPERATIONS = ROOT / "infrastructure-operations"
PROVISIONING_FIXTURE = ROOT / "tests/fixtures/provisioning"


def _load_job(name: str):
    namespace = runpy.run_path(
        str(CONFIGURATION_OPERATIONS / "jobs" / f"{name}.py")
    )
    return namespace["main"]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "provisioning"
    (project / "playbooks").mkdir(parents=True)
    (project / "inventories/default").mkdir(parents=True)
    (project / "ansible.cfg").write_text(
        "[defaults]\ninventory=inventory.yml\n",
        encoding="utf-8",
    )
    (project / "playbooks/site.yml").write_text(
        "---\n- hosts: all\n",
        encoding="utf-8",
    )
    (project / "inventories/default/hosts.yml").write_text(
        "all:\n  hosts:\n    fixture:\n",
        encoding="utf-8",
    )
    return project


def test_first_party_manifests_expose_only_domain_controllers() -> None:
    configuration = load_manifest(CONFIGURATION_OPERATIONS)
    infrastructure = load_manifest(INFRASTRUCTURE_OPERATIONS)

    assert configuration.name == "configuration-operations"
    assert list(configuration.commands) == ["atlas-ansible"]
    assert list(configuration.jobs) == [
        "ansible-syntax-check",
        "config-check",
        "config-diff",
        "config-apply",
        "inventory-show",
        "inventory-refresh",
    ]
    assert configuration.jobs["inventory-refresh"].default_timeout_seconds == 300
    service = configuration.services["inventory-refresh"]
    assert service.job == "inventory-refresh"
    assert service.systemd.service.name == "inventory-refresh.service"
    assert service.systemd.timer is not None
    assert service.systemd.timer.name == "inventory-refresh.timer"

    assert infrastructure.name == "infrastructure-operations"
    assert list(infrastructure.commands) == [
        "hostctl",
        "imagectl",
    ]
    assert list(configuration.commands) + list(infrastructure.commands) == [
        "atlas-ansible",
        "hostctl",
        "imagectl",
    ]
    assert not (
        set(configuration.commands)
        | set(infrastructure.commands)
    ) & {
        "config-diff",
        "vm-create-apply",
        "vm-template-create-apply",
        "proxmox-status",
        "operation-artifact-validate",
    }
    assert (CONFIGURATION_OPERATIONS / "VERSION").read_text(encoding="utf-8") == (
        "2.0.0\n"
    )
    assert (INFRASTRUCTURE_OPERATIONS / "VERSION").read_text(encoding="utf-8") == (
        "2.0.0\n"
    )


@pytest.mark.parametrize(
    ("job_name", "argv", "expected_executable", "expected_args"),
    [
        (
            "ansible-syntax-check",
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
def test_configuration_jobs_delegate_exact_argv(
    job_name: str,
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

    monkeypatch.setattr(
        "atlas_configuration_operations.config_project.subprocess.run",
        fake_run,
    )

    assert _load_job(job_name)(argv) == 7
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
    with pytest.raises(ValueError, match="invalid site name"):
        inventory_path(project, "../default")
    with pytest.raises(ValueError, match="inventory not found"):
        inventory_path(project, "default")

    (project / "playbooks").mkdir()
    external_playbook = tmp_path / "site.yml"
    external_playbook.write_text("---\n", encoding="utf-8")
    (project / "playbooks/site.yml").symlink_to(external_playbook)
    with pytest.raises(ValueError, match="playbook not found"):
        playbook_path(project, "site")
    (project / "playbooks/site.yml").unlink()
    (project / "playbooks/site.yml").write_text("---\n", encoding="utf-8")
    assert playbook_path(project, "site") == project / "playbooks/site.yml"

    (project / "inventories/default").mkdir(parents=True)
    external_inventory = tmp_path / "hosts.yml"
    external_inventory.write_text("all:\n", encoding="utf-8")
    (project / "inventories/default/hosts.yml").symlink_to(external_inventory)
    with pytest.raises(ValueError, match="inventory not found"):
        inventory_path(project, "default")
    (project / "inventories/default/hosts.yml").unlink()
    (project / "inventories/default/hosts.yml").write_text(
        "all:\n",
        encoding="utf-8",
    )
    assert inventory_path(project, "default") == (
        project / "inventories/default/hosts.yml"
    )

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

    monkeypatch.setattr(
        "atlas_configuration_operations.config_project.subprocess.run",
        missing,
    )
    assert run_native("ansible-playbook", [], project) == 127
    assert "ansible-playbook command not found" in capsys.readouterr().err
    assert report_error(ValueError("bad input")) == 2
    assert "bad input" in capsys.readouterr().err


def test_configuration_jobs_report_validation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "empty"
    project.mkdir()
    monkeypatch.chdir(project)

    assert _load_job("config-apply")(["../site", "web01"]) == 2
    assert "invalid playbook name" in capsys.readouterr().err
    assert _load_job("inventory-show")([]) == 2
    assert "ansible.cfg not found" in capsys.readouterr().err
    for job_name in ("ansible-syntax-check", "config-check", "config-diff"):
        argv = ["site"] if job_name == "ansible-syntax-check" else ["site", "web01"]
        assert _load_job(job_name)(argv) == 2
        assert "playbook not found" in capsys.readouterr().err


def test_config_apply_rejects_empty_or_missing_target(
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

    monkeypatch.setattr(
        "atlas_configuration_operations.config_project.subprocess.run",
        unexpected,
    )
    main = _load_job("config-apply")
    assert main(["site", ""]) == 2
    assert "target must not be empty" in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        main(["site"])
    assert error.value.code == 2
    assert called is False


def test_atlas_ansible_dispatches_each_private_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_EXECUTABLE", "/atlas")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        controller_module,
        "run_child",
        lambda argv: calls.append(argv) or 7,
    )

    cases = [
        (["check", "site", "web01"], "config-check", ["site", "web01"]),
        (["diff", "site", "web01"], "config-diff", ["site", "web01"]),
        (["apply", "site", "web01"], "config-apply", ["site", "web01"]),
        (["inventory"], "inventory-show", []),
    ]
    for argv, job, child_args in cases:
        assert controller_module.main(argv) == 7
        assert calls[-1] == [
            "/atlas",
            "job",
            "run",
            "configuration-operations",
            job,
            "--",
            *child_args,
        ]

    with pytest.raises(SystemExit) as raised:
        controller_module.main(["apply", "site"])
    assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        controller_module.main(["validate", "site"])
    assert raised.value.code == 2


def test_atlas_ansible_diff_many_orders_deduplicates_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATLAS_EXECUTABLE", "/atlas")
    calls: list[list[str]] = []
    return_codes = iter([3, 0, 5])
    monkeypatch.setattr(
        controller_module,
        "run_child",
        lambda argv: calls.append(argv) or next(return_codes),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("web02\n\nweb03\nweb01\n"))

    assert controller_module.main(["diff-many", "site", "web01", "web02"]) == 3
    assert calls == [
        [
            "/atlas",
            "job",
            "run",
            "configuration-operations",
            "config-diff",
            "--",
            "site",
            "web01",
        ],
        [
            "/atlas",
            "job",
            "run",
            "configuration-operations",
            "config-diff",
            "--",
            "site",
            "web02",
        ],
        [
            "/atlas",
            "job",
            "run",
            "configuration-operations",
            "config-diff",
            "--",
            "site",
            "web03",
        ],
    ]
    assert capsys.readouterr().err.splitlines() == [
        "==> web01 <==",
        "==> web02 <==",
        "==> web03 <==",
    ]


def test_atlas_ansible_diff_many_handles_no_targets_and_terminal_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATLAS_EXECUTABLE", "/atlas")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert controller_module.main(["diff-many", "site"]) == 2
    assert "at least one target" in capsys.readouterr().err

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

        def read(self, *args, **kwargs):
            raise AssertionError("terminal stdin must not be read")

    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "stdin", Terminal("ignored"))
    monkeypatch.setattr(
        controller_module,
        "run_child",
        lambda argv: calls.append(argv) or 0,
    )
    assert controller_module.main(["diff-many", "site", "web01"]) == 0
    assert calls == [
        [
            "/atlas",
            "job",
            "run",
            "configuration-operations",
            "config-diff",
            "--",
            "site",
            "web01",
        ]
    ]


def test_configuration_child_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATLAS_EXECUTABLE", "/custom/atlas")
    assert child_module.atlas_executable() == "/custom/atlas"
    assert child_module.job_argv("config-check", ["site", "web01"]) == [
        "/custom/atlas",
        "job",
        "run",
        "configuration-operations",
        "config-check",
        "--",
        "site",
        "web01",
    ]
    monkeypatch.delenv("ATLAS_EXECUTABLE")
    monkeypatch.setenv("ATLAS_HOME", "/srv/atlas")
    assert child_module.atlas_executable() == "/srv/atlas/bin/atlas"
    monkeypatch.delenv("ATLAS_HOME")
    assert child_module.atlas_executable() == "/opt/atlas/bin/atlas"

    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        returncode = -15

    monkeypatch.setattr(
        child_module.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or Process(),
    )
    assert child_module.run_child(["child", "arg"]) == 143
    assert calls == [(["child", "arg"], {"check": False, "shell": False})]

    monkeypatch.setattr(
        child_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert child_module.run_child(["missing"]) == 127
    assert "missing command not found" in capsys.readouterr().err


def test_atlas_ansible_diff_many_preserves_nested_correlation(
    atlas_paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ansible = atlas_paths.runtime_python.parent / "ansible-playbook"
    fake_ansible.write_text(
        "#!/bin/sh\nprintf 'ansible-playbook:%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_ansible.chmod(0o755)

    assert cli.main(["release", "install", str(CONFIGURATION_OPERATIONS)]) == 0
    monkeypatch.chdir(PROVISIONING_FIXTURE)
    process = subprocess.run(
        [
            str(atlas_paths.shims / "atlas-ansible"),
            "diff-many",
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
        for line in (atlas_paths.logs / "runs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    parent = next(
        record
        for record in records
        if record["artifact"] == "atlas-ansible"
        and record["args"][0] == "diff-many"
    )
    jobs = [record for record in records if record["artifact"] == "config-diff"]
    assert [record["args"] for record in jobs] == [
        ["site", "fixture"],
        ["site", "second"],
    ]
    assert all(record["operation_id"] == parent["operation_id"] for record in jobs)
    assert all(job["parent_run_id"] == parent["run_id"] for job in jobs)


def test_final_release_install_generates_only_controller_shims(
    atlas_paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["release", "install", str(CONFIGURATION_OPERATIONS)]) == 0
    assert cli.main(["release", "install", str(INFRASTRUCTURE_OPERATIONS)]) == 0
    capsys.readouterr()

    assert cli.main(["command", "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "atlas-ansible",
        "hostctl",
        "imagectl",
    ]
    assert sorted(path.name for path in atlas_paths.shims.iterdir()) == [
        "atlas-ansible",
        "hostctl",
        "imagectl",
    ]
    for old_name in (
        "configctl",
        "providerctl",
        "operationctl",
        "config-diff",
        "vm-create-apply",
        "vm-template-create-apply",
        "proxmox-status",
        "operation-artifact-validate",
    ):
        assert not (atlas_paths.shims / old_name).exists()

    for controller in (
        "atlas-ansible",
        "hostctl",
        "imagectl",
    ):
        process = subprocess.run(
            [str(atlas_paths.shims / controller), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert process.returncode == 0
        assert f"usage: {controller}" in process.stdout


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
        "atlas_configuration_operations.config_project.subprocess.run",
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

    assert _load_job("inventory-refresh")(["--site", "missing"]) == 2


def test_configuration_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(
        str(CONFIGURATION_OPERATIONS / "commands/atlas-ansible.py"),
        run_name="configuration_entrypoint_test",
    )
    assert namespace["__name__"] == "configuration_entrypoint_test"
    monkeypatch.setattr(controller_module, "main", lambda: 7)
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(
            str(CONFIGURATION_OPERATIONS / "commands/atlas-ansible.py"),
            run_name="__main__",
        )
    assert raised.value.code == 7

    for job_name in (
        "ansible-syntax-check",
        "config-check",
        "config-diff",
        "config-apply",
        "inventory-show",
        "inventory-refresh",
    ):
        monkeypatch.setattr(sys, "argv", [job_name, "--help"])
        with pytest.raises(SystemExit) as error:
            runpy.run_path(
                str(CONFIGURATION_OPERATIONS / "jobs" / f"{job_name}.py"),
                run_name="__main__",
            )
        assert error.value.code == 0
