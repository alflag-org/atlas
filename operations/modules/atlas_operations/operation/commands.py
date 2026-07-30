"""Command boundaries for reviewed operations."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path

from atlas_operations.operation.artifacts import (
    read_artifact_arg,
    write_diag_stderr,
    write_json_stdout,
    write_text_stdout,
)
from atlas_operations.operation.config import (
    ProviderDefinition,
    load_provider_definition,
    load_vm_create_input,
    load_vm_template_create_input,
)
from atlas_operations.operation.errors import (
    InputError,
    OperationError,
    PlanError,
    ProviderError,
    SafetyError,
)
from atlas_operations.operation.evidence import OperationEvidence
from atlas_operations.operation.files import file_digest
from atlas_operations.operation.plan import OperationPlan
from atlas_operations.operation.provider import ProviderQuery, VerifyResult
from atlas_operations.operation.proxmox import ProxmoxProviderClient
from atlas_operations.operation.validate import (
    validate_artifact_data,
    validate_evidence,
    validate_plan,
)
from atlas_operations.operation.vm_create import (
    apply_vm_create,
    build_vm_create_plan,
    rollback_vm_create,
    verify_vm_create,
)
from atlas_operations.operation.vm_template_create import (
    apply_vm_template_create,
    build_vm_template_create_plan,
    rollback_vm_template_create,
    verify_vm_template_create,
)


def proxmox_status_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proxmox-status")
    parser.add_argument("provider")
    args = parser.parse_args(argv)

    def command() -> int:
        definition, _path = _provider_definition(args.provider)
        state = _provider_client(definition).read_state(ProviderQuery(kind="status"))
        write_json_stdout({"provider": state.provider, **state.data})
        return 0

    return _run(command)


def vm_create_plan_main(argv: Sequence[str] | None = None) -> int:
    parser = _plan_parser("vm-create-plan")
    args = parser.parse_args(argv)

    def command() -> int:
        definition, provider_path = _provider_definition(args.provider)
        input_path = _safe_path(args.input)
        operation_input = load_vm_create_input(input_path)
        plan = build_vm_create_plan(
            definition,
            operation_input,
            input_path=str(input_path),
            provider_path=str(provider_path),
            provider=_provider_client(definition),
        )
        validate_plan(plan)
        _validate_sources(plan, provider_path, definition)
        write_json_stdout(plan.as_artifact())
        return 0

    return _run(command)


def vm_template_create_plan_main(argv: Sequence[str] | None = None) -> int:
    parser = _plan_parser("vm-template-create-plan")
    args = parser.parse_args(argv)

    def command() -> int:
        definition, provider_path = _provider_definition(args.provider)
        input_path = _safe_path(args.input)
        operation_input = load_vm_template_create_input(input_path)
        plan = build_vm_template_create_plan(
            definition,
            operation_input,
            input_path=str(input_path),
            provider_path=str(provider_path),
            provider=_provider_client(definition),
        )
        validate_plan(plan)
        _validate_sources(plan, provider_path, definition)
        write_json_stdout(plan.as_artifact())
        return 0

    return _run(command)


def vm_create_apply_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_parser("vm-create-apply", confirm=True)
    args = parser.parse_args(argv)
    return _run(
        lambda: _apply(
            args.provider,
            args.artifact,
            args.confirm,
            "proxmox.vm-create",
            apply_vm_create,
        )
    )


def vm_template_create_apply_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_parser("vm-template-create-apply", confirm=True)
    args = parser.parse_args(argv)
    return _run(
        lambda: _apply(
            args.provider,
            args.artifact,
            args.confirm,
            "proxmox.vm-template-create",
            apply_vm_template_create,
        )
    )


def vm_create_verify_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_parser("vm-create-verify")
    args = parser.parse_args(argv)
    return _run(
        lambda: _verify(
            args.provider,
            args.artifact,
            "proxmox.vm-create",
            verify_vm_create,
        )
    )


def vm_template_create_verify_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_parser("vm-template-create-verify")
    args = parser.parse_args(argv)
    return _run(
        lambda: _verify(
            args.provider,
            args.artifact,
            "proxmox.vm-template-create",
            verify_vm_template_create,
        )
    )


def vm_create_rollback_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_parser("vm-create-rollback", confirm=True)
    args = parser.parse_args(argv)
    return _run(
        lambda: _rollback(
            args.provider,
            args.artifact,
            args.confirm,
            "proxmox.vm-create",
            rollback_vm_create,
        )
    )


def vm_template_create_rollback_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_parser("vm-template-create-rollback", confirm=True)
    args = parser.parse_args(argv)
    return _run(
        lambda: _rollback(
            args.provider,
            args.artifact,
            args.confirm,
            "proxmox.vm-template-create",
            rollback_vm_template_create,
        )
    )


def operation_artifact_validate_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_only_parser("operation-artifact-validate")
    args = parser.parse_args(argv)

    def command() -> int:
        artifact = validate_artifact_data(read_artifact_arg(args.artifact))
        write_json_stdout(
            {
                "apiVersion": artifact.api_version,
                "kind": artifact.kind,
                "valid": True,
            }
        )
        return 0

    return _run(command)


def operation_artifact_inspect_main(argv: Sequence[str] | None = None) -> int:
    parser = _artifact_only_parser("operation-artifact-inspect")
    args = parser.parse_args(argv)

    def command() -> int:
        artifact = validate_artifact_data(read_artifact_arg(args.artifact))
        write_text_stdout(_inspection(artifact))
        return 0

    return _run(command)


def _plan_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("provider")
    parser.add_argument("input")
    return parser


def _artifact_parser(prog: str, *, confirm: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("provider")
    parser.add_argument("artifact", nargs="?", default="-")
    if confirm:
        parser.add_argument("--confirm", required=True)
    return parser


def _artifact_only_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("artifact", nargs="?", default="-")
    return parser


def _run(command: Callable[[], int]) -> int:
    try:
        return command()
    except SafetyError as exc:
        write_diag_stderr(str(exc))
        return 3
    except ProviderError as exc:
        write_diag_stderr(str(exc))
        return 4
    except (InputError, PlanError) as exc:
        write_diag_stderr(str(exc))
        return 2
    except OperationError as exc:
        write_diag_stderr(str(exc))
        return 1


def _safe_path(path: str) -> Path:
    supplied = Path(path)
    if not supplied.is_file() or supplied.is_symlink():
        raise InputError(f"input file not found or unsafe: {supplied}")
    return supplied.resolve()


def _provider_definition(path: str) -> tuple[ProviderDefinition, Path]:
    provider_path = _safe_path(path)
    return load_provider_definition(provider_path), provider_path


def _provider_client(definition: ProviderDefinition) -> ProxmoxProviderClient:
    return ProxmoxProviderClient(definition.connection)


def _artifact(
    artifact_arg: str,
    expected_operation: str,
) -> OperationPlan | OperationEvidence:
    artifact = validate_artifact_data(read_artifact_arg(artifact_arg))
    operation_kind = artifact.metadata.operation_kind
    if operation_kind != expected_operation:
        raise PlanError(
            f"command requires {expected_operation}, artifact contains {operation_kind}"
        )
    return artifact


def _plan_from_artifact(
    artifact: OperationPlan | OperationEvidence,
) -> OperationPlan:
    if isinstance(artifact, OperationPlan):
        return artifact
    return OperationPlan.model_validate(
        artifact.plan.snapshot,
        by_alias=True,
        by_name=False,
    )


def _validate_sources(
    plan: OperationPlan,
    provider_path: Path,
    definition: ProviderDefinition,
) -> None:
    expected_provider_path = Path(plan.source.provider.path)
    if provider_path != expected_provider_path:
        raise PlanError(
            "provider definition path does not match plan source: "
            f"{provider_path} != {expected_provider_path}"
        )
    if file_digest(provider_path) != plan.source.provider.digest:
        raise PlanError("provider definition changed after the plan was created")
    if file_digest(plan.source.input.path) != plan.source.input.digest:
        raise PlanError("operation input changed after the plan was created")
    if (
        plan.safety.requires_confirm != definition.safety.require_confirm
        or plan.safety.max_plan_age_seconds
        != definition.safety.max_plan_age_seconds
        or not plan.safety.requires_rollback
        or not plan.safety.allowed_only_if_preflight_passes
    ):
        raise PlanError("plan safety policy does not match the provider definition")

    if plan.metadata.operation_kind == "proxmox.vm-create":
        operation_input = load_vm_create_input(plan.source.input.path)
        expected_node = operation_input.vm.node
        expected_vmid = operation_input.vm.vmid
        expected_action = "create"
    else:
        operation_input = load_vm_template_create_input(plan.source.input.path)
        expected_node = operation_input.node
        expected_vmid = operation_input.vmid
        expected_action = "create-template"

    if plan.spec != operation_input.to_plan_spec():
        raise PlanError("plan spec does not match the operation input")
    if (
        plan.metadata.site != operation_input.site
        or plan.metadata.target != operation_input.target
        or plan.provider.node != expected_node
    ):
        raise PlanError("plan target does not match the operation input")
    expected_idempotency_key = (
        f"{plan.metadata.operation_kind}:{operation_input.target}:{expected_vmid}"
    )
    if plan.metadata.idempotency_key != expected_idempotency_key:
        raise PlanError("plan idempotency key does not match the operation input")
    expected_changes = [
        {
            "action": expected_action,
            "provider": "proxmox",
            "resource": f"qemu/{expected_vmid}",
            "name": operation_input.target,
        }
    ]
    if plan.changes != expected_changes:
        raise PlanError("plan changes do not match the operation input")


def _apply(
    provider_arg: str,
    artifact_arg: str,
    confirm: str,
    expected_operation: str,
    apply_operation: Callable[..., OperationEvidence],
) -> int:
    artifact = _artifact(artifact_arg, expected_operation)
    if not isinstance(artifact, OperationPlan):
        raise PlanError("apply requires an OperationPlan artifact")
    definition, provider_path = _provider_definition(provider_arg)
    _validate_sources(artifact, provider_path, definition)
    evidence = apply_operation(
        definition.safety,
        plan=artifact,
        provider=_provider_client(definition),
        confirm=confirm,
        progress=write_diag_stderr,
    )
    validate_evidence(evidence)
    write_json_stdout(evidence.as_artifact())
    return 0 if evidence.metadata.result == "success" else 1


def _verify(
    provider_arg: str,
    artifact_arg: str,
    expected_operation: str,
    verify_operation: Callable[..., VerifyResult],
) -> int:
    artifact = _artifact(artifact_arg, expected_operation)
    plan = _plan_from_artifact(artifact)
    definition, provider_path = _provider_definition(provider_arg)
    _validate_sources(plan, provider_path, definition)
    result = verify_operation(plan=plan, provider=_provider_client(definition))
    write_json_stdout(
        {
            "status": result.status,
            "checks": [
                check.model_dump(mode="json", by_alias=True) for check in result.checks
            ],
            "details": result.details,
        }
    )
    return 0 if result.status == "passed" else 1


def _rollback(
    provider_arg: str,
    artifact_arg: str,
    confirm: str,
    expected_operation: str,
    rollback_operation: Callable[..., OperationEvidence],
) -> int:
    artifact = _artifact(artifact_arg, expected_operation)
    if not isinstance(artifact, OperationEvidence):
        raise PlanError("rollback requires an OperationEvidence artifact")
    definition, provider_path = _provider_definition(provider_arg)
    plan = _plan_from_artifact(artifact)
    _validate_sources(plan, provider_path, definition)
    evidence = rollback_operation(
        definition.safety,
        evidence=artifact,
        provider=_provider_client(definition),
        confirm=confirm,
        progress=write_diag_stderr,
    )
    validate_evidence(evidence)
    write_json_stdout(evidence.as_artifact())
    return 0 if evidence.rollback.result == "success" else 1


def _inspection(artifact: OperationPlan | OperationEvidence) -> str:
    if isinstance(artifact, OperationPlan):
        return "\n".join(
            [
                f"kind: {artifact.kind}",
                f"api version: {artifact.api_version}",
                f"plan id: {artifact.metadata.plan_id}",
                f"operation: {artifact.metadata.operation_kind}",
                f"target: {artifact.metadata.target}",
                f"provider: {artifact.provider.name}",
                f"node: {artifact.provider.node}",
                f"preflight: {artifact.preflight.status}",
                f"apply steps: {len(artifact.apply.steps)}",
                f"rollback supported: {str(artifact.rollback.supported).lower()}",
                f"fingerprint: {artifact.metadata.fingerprint}",
            ]
        )
    return "\n".join(
        [
            f"kind: {artifact.kind}",
            f"api version: {artifact.api_version}",
            f"evidence id: {artifact.metadata.evidence_id}",
            f"plan id: {artifact.metadata.plan_id}",
            f"operation: {artifact.metadata.operation_kind}",
            f"target: {artifact.metadata.target}",
            f"result: {artifact.metadata.result}",
            f"provider: {artifact.provider.name}",
            f"created resources: {len(artifact.created_resources)}",
            f"rollback result: {artifact.rollback.result or 'not-run'}",
        ]
    )
