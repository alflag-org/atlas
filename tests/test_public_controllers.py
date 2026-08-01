from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import atlas_infrastructure_operations.child as child_module
import atlas_infrastructure_operations.imagectl as imagectl_module
import atlas_infrastructure_operations.operation_status as operation_status_module
import atlas_infrastructure_operations.operationctl as operationctl_module
import atlas_infrastructure_operations.providerctl as providerctl_module
import pytest
from atlas_host_operations.errors import RegistryConflictError
from atlas_host_operations.models import RegistryOperation
from atlas_operations.operation import commands as operation_commands

from .test_operation_support import write_operation_inputs

ROOT = Path(__file__).resolve().parents[1]
INFRASTRUCTURE_OPERATIONS = ROOT / "infrastructure-operations"


def test_imagectl_dispatches_private_jobs_and_rejects_missing_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        imagectl_module,
        "run_job",
        lambda job, args: calls.append((job, args)) or 7,
    )

    assert imagectl_module.main(["plan", "provider.yml", "image.yml"]) == 7
    assert calls[-1] == (
        "vm-template-create-plan",
        ["provider.yml", "image.yml"],
    )
    assert (
        imagectl_module.main(
            ["apply", "provider.yml", "plan.json", "--confirm", "plan-1"]
        )
        == 7
    )
    assert calls[-1] == (
        "vm-template-create-apply",
        ["provider.yml", "plan.json", "--confirm", "plan-1"],
    )
    assert imagectl_module.main(["verify", "provider.yml"]) == 7
    assert calls[-1] == ("vm-template-create-verify", ["provider.yml", "-"])
    assert (
        imagectl_module.main(
            ["rollback", "provider.yml", "--confirm", "plan-1"]
        )
        == 7
    )
    assert calls[-1] == (
        "vm-template-create-rollback",
        ["provider.yml", "-", "--confirm", "plan-1"],
    )

    assert imagectl_module.main(["status", "op-1"]) == 2
    assert "requires durable image operation state" in capsys.readouterr().err
    assert imagectl_module.main(["resume", "op-1", "--confirm", "plan-1"]) == 2
    assert "requires durable image operation state" in capsys.readouterr().err
    with pytest.raises(SystemExit) as raised:
        imagectl_module.main(["apply", "provider.yml"])
    assert raised.value.code == 2


def test_providerctl_dispatches_read_only_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        providerctl_module,
        "run_job",
        lambda job, args: calls.append((job, args)) or 4,
    )
    assert providerctl_module.main(["validate", "provider.yml"]) == 4
    assert providerctl_module.main(["status", "provider.yml"]) == 4
    assert calls == [
        ("provider-validate", ["provider.yml"]),
        ("proxmox-status", ["provider.yml"]),
    ]


def test_operationctl_dispatches_artifact_and_registry_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        operationctl_module,
        "run_job",
        lambda job, args: calls.append((job, args)) or 5,
    )
    assert operationctl_module.main(["validate"]) == 5
    assert operationctl_module.main(["inspect", "plan.json"]) == 5
    assert operationctl_module.main(["status", "op-1"]) == 5
    assert calls == [
        ("operation-artifact-validate", ["-"]),
        ("operation-artifact-inspect", ["plan.json"]),
        ("operation-status", ["op-1"]),
    ]


def test_infrastructure_child_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_run_child = child_module.run_child
    monkeypatch.setenv("ATLAS_EXECUTABLE", "/custom/atlas")
    assert child_module.atlas_executable() == "/custom/atlas"
    assert child_module.job_argv("provider-validate", ["provider.yml"]) == [
        "/custom/atlas",
        "job",
        "run",
        "infrastructure-operations",
        "provider-validate",
        "--",
        "provider.yml",
    ]
    monkeypatch.delenv("ATLAS_EXECUTABLE")
    monkeypatch.setenv("ATLAS_HOME", "/srv/atlas")
    assert child_module.atlas_executable() == "/srv/atlas/bin/atlas"
    monkeypatch.delenv("ATLAS_HOME")
    assert child_module.atlas_executable() == "/opt/atlas/bin/atlas"

    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        returncode = -2

    monkeypatch.setattr(
        child_module.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or Process(),
    )
    assert child_module.run_child(["child"]) == 130
    assert calls == [(["child"], {"check": False, "shell": False})]

    monkeypatch.setattr(child_module, "run_child", lambda argv: len(argv))
    assert child_module.run_job("operation-status", ["op-1"]) == 7

    monkeypatch.setattr(
        child_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert original_run_child(["missing"]) == 127
    assert "missing command not found" in capsys.readouterr().err


def test_provider_validate_job_uses_strict_definition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, *_rest = write_operation_inputs(tmp_path)
    assert operation_commands.provider_validate_main([str(provider_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": "atlas.provider/v1",
        "provider": "proxmox",
        "valid": True,
    }
    assert operation_commands.provider_validate_main([str(tmp_path / "missing")]) == 2
    assert "not found or unsafe" in capsys.readouterr().err


def test_operation_status_reads_registry_profile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ATLAS_REGISTRY_PROFILE", raising=False)
    assert operation_status_module.main(["op-1"]) == 2
    assert "ATLAS_REGISTRY_PROFILE is required" in capsys.readouterr().err

    operation = RegistryOperation(id="op-1", status="running", revision=1)

    class Client:
        def get_operation(self, operation_id: str):
            assert operation_id == "op-1"
            return operation

    monkeypatch.setenv("ATLAS_REGISTRY_PROFILE", "/registry.yml")
    monkeypatch.setattr(
        operation_status_module.HTTPRegistryClient,
        "from_profile",
        lambda profile: Client(),
    )
    assert operation_status_module.main(["op-1"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "id": "op-1",
        "plan": {},
        "resources": [],
        "revision": 1,
        "status": "running",
        "steps": [],
    }

    class MissingClient:
        def get_operation(self, operation_id: str):
            return None

    monkeypatch.setattr(
        operation_status_module.HTTPRegistryClient,
        "from_profile",
        lambda profile: MissingClient(),
    )
    assert operation_status_module.main(["op-missing"]) == 2
    assert "operation not found" in capsys.readouterr().err

    class ConflictingClient:
        def get_operation(self, operation_id: str):
            raise RegistryConflictError("revision conflict")

    monkeypatch.setattr(
        operation_status_module.HTTPRegistryClient,
        "from_profile",
        lambda profile: ConflictingClient(),
    )
    assert operation_status_module.main(["op-1"]) == 5
    assert "revision conflict" in capsys.readouterr().err


def test_infrastructure_controller_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = {
        "imagectl": imagectl_module,
        "providerctl": providerctl_module,
        "operationctl": operationctl_module,
    }
    for index, (command, module) in enumerate(modules.items(), start=1):
        namespace = runpy.run_path(
            str(INFRASTRUCTURE_OPERATIONS / "commands" / f"{command}.py"),
            run_name="infrastructure_entrypoint_test",
        )
        assert namespace["__name__"] == "infrastructure_entrypoint_test"
        monkeypatch.setattr(module, "main", lambda code=index: code)
        with pytest.raises(SystemExit) as raised:
            runpy.run_path(
                str(INFRASTRUCTURE_OPERATIONS / "commands" / f"{command}.py"),
                run_name="__main__",
            )
        assert raised.value.code == index


@pytest.mark.parametrize(
    "job_name",
    [
        "provider-validate",
        "proxmox-status",
        "vm-create-plan",
        "vm-create-apply",
        "vm-create-verify",
        "vm-create-rollback",
        "vm-template-create-plan",
        "vm-template-create-apply",
        "vm-template-create-verify",
        "vm-template-create-rollback",
        "operation-artifact-validate",
        "operation-artifact-inspect",
        "operation-status",
    ],
)
def test_infrastructure_diagnostic_job_entrypoints_show_help(
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [job_name, "--help"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(
            str(INFRASTRUCTURE_OPERATIONS / "jobs" / f"{job_name}.py"),
            run_name="__main__",
        )
    assert raised.value.code == 0


def test_public_controllers_do_not_import_operation_implementations() -> None:
    for path in (
        INFRASTRUCTURE_OPERATIONS
        / "modules/atlas_infrastructure_operations/imagectl.py",
        INFRASTRUCTURE_OPERATIONS
        / "modules/atlas_infrastructure_operations/providerctl.py",
        INFRASTRUCTURE_OPERATIONS
        / "modules/atlas_infrastructure_operations/operationctl.py",
        ROOT
        / "configuration-operations/modules/atlas_configuration_operations/controller.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "atlas_operations.operation" not in source
        assert "shell=True" not in source
