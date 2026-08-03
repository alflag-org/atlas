from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from atlas_operations.operation import commands
from atlas_operations.operation.errors import OperationError, ProviderError
from atlas_operations.operation.fingerprint import set_fingerprint
from atlas_operations.operation.plan import CheckResult, OperationPlan

from .test_operation_support import FakeProvider, write_operation_inputs


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _mutated_plan(data: dict, update) -> dict:
    changed = copy.deepcopy(data)
    update(changed)
    changed["metadata"].pop("fingerprint", None)
    return set_fingerprint(OperationPlan.model_validate(changed)).as_artifact()


def _plan_with_command(
    main,
    provider_path: Path,
    input_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict, Path]:
    assert main([str(provider_path), str(input_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    path = input_path.with_suffix(".plan.json")
    return plan, _write_json(path, plan)


def test_plan_commands_use_explicit_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, template_input, *_rest = write_operation_inputs(tmp_path)
    fake = FakeProvider()
    monkeypatch.setattr(commands, "_provider_client", lambda definition: fake)
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )

    vm_plan, _vm_plan_path = _plan_with_command(
        commands.vm_create_plan_main,
        provider_path,
        vm_input,
        capsys,
    )
    assert vm_plan["metadata"]["operationKind"] == "proxmox.vm-create"
    assert vm_plan["source"]["provider"]["path"] == str(provider_path)

    template_plan, _template_plan_path = _plan_with_command(
        commands.vm_template_create_plan_main,
        provider_path,
        template_input,
        capsys,
    )
    assert (
        template_plan["metadata"]["operationKind"]
        == "proxmox.vm-template-create"
    )


def test_vm_command_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, _template_input, *_rest = write_operation_inputs(tmp_path)
    fake = FakeProvider()
    monkeypatch.setattr(commands, "_provider_client", lambda definition: fake)
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )
    plan, plan_path = _plan_with_command(
        commands.vm_create_plan_main,
        provider_path,
        vm_input,
        capsys,
    )
    plan_id = plan["metadata"]["planId"]

    assert (
        commands.vm_create_apply_main(
            [str(provider_path), str(plan_path), "--confirm", plan_id]
        )
        == 0
    )
    streams = capsys.readouterr()
    evidence = json.loads(streams.out)
    assert evidence["metadata"]["result"] == "success"
    assert "apply: clone-template" in streams.err
    evidence_path = _write_json(tmp_path / "vm-evidence.json", evidence)

    assert commands.vm_create_verify_main([str(provider_path), str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    assert (
        commands.vm_create_verify_main([str(provider_path), str(evidence_path)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "passed"

    assert (
        commands.vm_create_rollback_main(
            [str(provider_path), str(evidence_path), "--confirm", plan_id]
        )
        == 0
    )
    streams = capsys.readouterr()
    rolled_back = json.loads(streams.out)
    assert rolled_back["rollback"]["result"] == "success"
    assert "rollback: verify-created-resource" in streams.err


def test_template_command_lifecycle_and_nonzero_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, _vm_input, template_input, *_rest = write_operation_inputs(tmp_path)
    fake = FakeProvider()
    monkeypatch.setattr(commands, "_provider_client", lambda definition: fake)
    plan, plan_path = _plan_with_command(
        commands.vm_template_create_plan_main,
        provider_path,
        template_input,
        capsys,
    )
    plan_id = plan["metadata"]["planId"]

    assert (
        commands.vm_template_create_apply_main(
            [str(provider_path), str(plan_path), "--confirm", plan_id]
        )
        == 0
    )
    evidence = json.loads(capsys.readouterr().out)
    evidence_path = _write_json(tmp_path / "template-evidence.json", evidence)
    assert (
        commands.vm_template_create_verify_main(
            [str(provider_path), str(evidence_path)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    assert (
        commands.vm_template_create_rollback_main(
            [str(provider_path), str(evidence_path), "--confirm", plan_id]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["rollback"]["result"] == "success"

    failed = FakeProvider(verify_status="failed")
    monkeypatch.setattr(commands, "_provider_client", lambda definition: failed)
    assert (
        commands.vm_template_create_verify_main(
            [str(provider_path), str(plan_path)]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_apply_rejects_evidence_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, _template_input, *_rest = write_operation_inputs(tmp_path)
    monkeypatch.setattr(commands, "_provider_client", lambda definition: FakeProvider())
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )
    plan, plan_path = _plan_with_command(
        commands.vm_create_plan_main,
        provider_path,
        vm_input,
        capsys,
    )

    assert (
        commands.vm_create_apply_main(
            [
                str(provider_path),
                str(plan_path),
                "--confirm",
                plan["metadata"]["planId"],
            ]
        )
        == 0
    )
    evidence = json.loads(capsys.readouterr().out)
    evidence_path = _write_json(tmp_path / "evidence.json", evidence)
    assert (
        commands.vm_create_apply_main(
            [
                str(provider_path),
                str(evidence_path),
                "--confirm",
                plan["metadata"]["planId"],
            ]
        )
        == 2
    )
    assert "OperationPlan" in capsys.readouterr().err
def test_commands_reject_wrong_artifact_boundary_and_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, template_input, *_rest = write_operation_inputs(tmp_path)
    monkeypatch.setattr(commands, "_provider_client", lambda definition: FakeProvider())
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )
    vm_plan, vm_plan_path = _plan_with_command(
        commands.vm_create_plan_main,
        provider_path,
        vm_input,
        capsys,
    )
    _template_plan, template_plan_path = _plan_with_command(
        commands.vm_template_create_plan_main,
        provider_path,
        template_input,
        capsys,
    )

    assert (
        commands.vm_create_apply_main(
            [str(provider_path), str(vm_plan_path), "--confirm", "wrong"]
        )
        == 3
    )
    assert "--confirm" in capsys.readouterr().err

    assert (
        commands.vm_create_verify_main(
            [str(provider_path), str(template_plan_path)]
        )
        == 2
    )
    assert "command requires" in capsys.readouterr().err

    assert (
        commands.vm_create_rollback_main(
            [
                str(provider_path),
                str(vm_plan_path),
                "--confirm",
                vm_plan["metadata"]["planId"],
            ]
        )
        == 2
    )
    assert "OperationEvidence" in capsys.readouterr().err

    assert (
        commands.vm_create_apply_main(
            [
                str(provider_path),
                str(template_plan_path),
                "--confirm",
                vm_plan["metadata"]["planId"],
            ]
        )
        == 2
    )
    assert "command requires" in capsys.readouterr().err


def test_commands_bind_provider_and_input_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, _template_input, *_rest = write_operation_inputs(tmp_path)
    monkeypatch.setattr(commands, "_provider_client", lambda definition: FakeProvider())
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )
    plan, plan_path = _plan_with_command(
        commands.vm_create_plan_main,
        provider_path,
        vm_input,
        capsys,
    )
    argv = [str(provider_path), str(plan_path), "--confirm", plan["metadata"]["planId"]]

    other_provider = tmp_path / "other-provider.yml"
    other_provider.write_bytes(provider_path.read_bytes())
    assert commands.vm_create_apply_main([str(other_provider), *argv[1:]]) == 2
    assert "path does not match" in capsys.readouterr().err

    original_provider = provider_path.read_text(encoding="utf-8")
    provider_path.write_text(f"{original_provider}\n", encoding="utf-8")
    assert commands.vm_create_apply_main(argv) == 2
    assert "provider definition changed" in capsys.readouterr().err
    provider_path.write_text(original_provider, encoding="utf-8")

    vm_input.write_text(f"{vm_input.read_text(encoding='utf-8')}\n", encoding="utf-8")
    assert commands.vm_create_apply_main(argv) == 2
    assert "operation input changed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (
            lambda data: data["safety"].update(maxPlanAgeSeconds=60),
            "safety policy",
        ),
        (
            lambda data: data["spec"].update(memoryMb=4096),
            "spec does not match",
        ),
        (
            lambda data: data["metadata"].update(target="other"),
            "target does not match",
        ),
        (
            lambda data: data["metadata"].update(idempotencyKey="other"),
            "idempotency key",
        ),
        (
            lambda data: data.update(changes=[]),
            "changes do not match",
        ),
    ],
)
def test_commands_rederive_plan_contract_from_bound_input(
    update,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, _template_input, *_rest = write_operation_inputs(tmp_path)
    monkeypatch.setattr(commands, "_provider_client", lambda definition: FakeProvider())
    monkeypatch.setattr(
        "atlas_operations.operation.vm_create._ping_check",
        lambda ip: CheckResult(name="network.ip.unused", status="passed", message=ip),
    )
    plan, _plan_path = _plan_with_command(
        commands.vm_create_plan_main,
        provider_path,
        vm_input,
        capsys,
    )
    changed = _mutated_plan(plan, update)
    changed_path = _write_json(tmp_path / "changed-plan.json", changed)

    assert commands.vm_create_verify_main([str(provider_path), str(changed_path)]) == 2
    assert message in capsys.readouterr().err


def test_command_exit_codes_distinguish_input_provider_and_operation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, _template_input, *_rest = write_operation_inputs(tmp_path)
    assert (
        commands.vm_create_plan_main([str(tmp_path / "missing.yml"), str(vm_input)])
        == 2
    )
    assert "not found or unsafe" in capsys.readouterr().err

    monkeypatch.setattr(
        commands,
        "_provider_client",
        lambda definition: (_ for _ in ()).throw(
            ProviderError("provider unavailable")
        ),
    )
    assert commands.vm_create_plan_main([str(provider_path), str(vm_input)]) == 4
    assert "provider unavailable" in capsys.readouterr().err

    monkeypatch.setattr(
        commands,
        "_provider_definition",
        lambda path: (_ for _ in ()).throw(OperationError("operation failed")),
    )
    assert commands.vm_create_plan_main([str(provider_path), str(vm_input)]) == 1
    assert "operation failed" in capsys.readouterr().err


def test_command_rejects_symlinked_explicit_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_path, vm_input, _template_input, *_rest = write_operation_inputs(tmp_path)
    link = tmp_path / "provider-link.yml"
    link.symlink_to(provider_path)
    assert commands.vm_create_plan_main([str(link), str(vm_input)]) == 2
    assert "unsafe" in capsys.readouterr().err


def test_provider_client_factory_receives_validated_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_path, _vm_input, _template_input, definition, *_rest = (
        write_operation_inputs(tmp_path)
    )
    monkeypatch.setattr(
        commands,
        "ProxmoxProviderClient",
        lambda connection: ("client", connection),
    )
    assert commands._provider_client(definition) == ("client", definition.connection)
    assert provider_path.is_file()
