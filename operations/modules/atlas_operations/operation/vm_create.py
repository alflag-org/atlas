from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network

from atlas_operations.operation.config import (
    ProviderDefinition,
    SafetyConfig,
    VmCreateInput,
)
from atlas_operations.operation.errors import ProviderError
from atlas_operations.operation.evidence import (
    EvidenceMetadata,
    EvidencePlanSnapshot,
    EvidenceProvider,
    EvidenceResource,
    EvidenceRollback,
    EvidenceStep,
    EvidenceVerify,
    OperationEvidence,
)
from atlas_operations.operation.files import file_digest
from atlas_operations.operation.fingerprint import set_fingerprint, validate_fingerprint
from atlas_operations.operation.plan import (
    CheckResult,
    OperationPhase,
    OperationPlan,
    OperationStep,
    PlanMetadata,
    PlanSource,
    PreflightResult,
    ProviderReference,
    RollbackPhase,
    SafetyPolicy,
    SourceReference,
)
from atlas_operations.operation.provider import ProviderClient, VerifyResult
from atlas_operations.operation.safety import SafetyGate


def build_vm_create_plan(
    provider_definition: ProviderDefinition,
    operation_input: VmCreateInput,
    *,
    input_path: str,
    provider_path: str,
    provider: ProviderClient,
) -> OperationPlan:
    plan = _initial_plan(
        provider_definition,
        operation_input,
        input_path=input_path,
        provider_path=provider_path,
    )
    local = _local_preflight(plan)
    provider_preflight = provider.preflight(plan)
    plan = _with_preflight(plan, _merge_preflight(local, provider_preflight))
    return set_fingerprint(plan)


def apply_vm_create(
    safety: SafetyConfig,
    *,
    plan: OperationPlan,
    provider: ProviderClient,
    confirm: str | None,
    progress: Callable[[str], None] | None = None,
) -> OperationEvidence:
    validate_fingerprint(plan)
    preflight = rerun_preflight(plan, provider)
    SafetyGate(safety).validate_apply(
        plan,
        confirm=confirm,
        preflight_passed=preflight.status == "passed",
    )
    started = datetime.now(UTC)
    steps: list[EvidenceStep] = []
    created_resources: list[EvidenceResource] = []
    result = "success"
    for step in plan.apply.steps:
        if progress:
            progress(f"apply: {step.id}")
        step_started = datetime.now(UTC)
        step_result = provider.apply_step(step, plan)
        step_finished = datetime.now(UTC)
        steps.append(
            EvidenceStep(
                id=step_result.id,
                status=step_result.status,
                taskId=step_result.task_id,
                startedAt=step_started,
                finishedAt=step_finished,
                message=step_result.message,
                details=step_result.details,
            )
        )
        if step_result.status == "success" and step.id == "clone-template":
            created_resources.append(_vm_resource(plan, created_by_step=step.id))
        if step_result.status == "success" and step.id == "set-ownership":
            created_resources = [
                _mark_ownership_written(resource) for resource in created_resources
            ]
        if step_result.status != "success":
            result = "failed"
            break
    verify_result = provider.verify(plan) if result == "success" else None
    if verify_result and verify_result.status != "passed":
        result = "failed"
    evidence = OperationEvidence(
        apiVersion="atlas.operation/v1",
        kind="OperationEvidence",
        metadata=EvidenceMetadata(
            evidenceId=_new_id("evidence"),
            planId=plan.metadata.plan_id,
            createdAt=started,
            operationKind=plan.metadata.operation_kind,
            target=plan.metadata.target,
            result=result,
        ),
        plan=_evidence_plan(plan),
        provider=EvidenceProvider(
            name=plan.provider.name,
            node=plan.provider.node,
            vmid=int(plan.spec["vmid"]),
        ),
        createdResources=created_resources,
        steps=steps,
        verify=_evidence_verify(verify_result) if verify_result else None,
        rollback=EvidenceRollback(
            supported=plan.rollback.supported and _has_created_vm_resource(created_resources)
        ),
    )
    return evidence


def verify_vm_create(
    *,
    plan: OperationPlan,
    provider: ProviderClient,
) -> VerifyResult:
    validate_fingerprint(plan)
    return provider.verify(plan)


def rollback_vm_create(
    safety: SafetyConfig,
    *,
    evidence: OperationEvidence,
    provider: ProviderClient,
    confirm: str | None,
    progress: Callable[[str], None] | None = None,
) -> OperationEvidence:
    plan = OperationPlan.model_validate(
        evidence.plan.snapshot,
        by_alias=True,
        by_name=False,
    )
    validate_fingerprint(plan)
    SafetyGate(safety).validate_rollback(
        plan,
        evidence,
        confirm=confirm,
    )
    result = "success"
    rollback_steps: list[EvidenceStep] = []
    for step in plan.rollback.steps:
        if progress:
            progress(f"rollback: {step.id}")
        step_started = datetime.now(UTC)
        step = _rollback_step_with_evidence(step, evidence, plan)
        step_result = provider.rollback_step(step, plan)
        step_finished = datetime.now(UTC)
        rollback_steps.append(
            EvidenceStep(
                id=step_result.id,
                status=step_result.status,
                taskId=step_result.task_id,
                startedAt=step_started,
                finishedAt=step_finished,
                message=step_result.message,
                details=step_result.details,
            )
        )
        if step_result.status != "success":
            result = "failed"
            break
    updated = OperationEvidence(
        apiVersion=evidence.api_version,
        kind=evidence.kind,
        metadata=EvidenceMetadata(
            evidenceId=_new_id("rollback"),
            planId=evidence.metadata.plan_id,
            createdAt=datetime.now(UTC),
            operationKind=evidence.metadata.operation_kind,
            target=evidence.metadata.target,
            result=result,
        ),
        plan=evidence.plan,
        provider=evidence.provider,
        createdResources=evidence.created_resources,
        steps=[*evidence.steps, *rollback_steps],
        verify=evidence.verify,
        rollback=EvidenceRollback(supported=True, result=result),
    )
    return updated


def rerun_preflight(
    plan: OperationPlan,
    provider: ProviderClient,
) -> VerifyResult:
    local = _local_preflight(plan)
    provider_preflight = provider.preflight(plan)
    return _merge_preflight(local, provider_preflight)


def _initial_plan(
    provider_definition: ProviderDefinition,
    operation_input: VmCreateInput,
    *,
    input_path: str,
    provider_path: str,
) -> OperationPlan:
    spec = operation_input.to_plan_spec()
    plan_id = _new_id("plan")
    plan = OperationPlan(
        apiVersion="atlas.operation/v1",
        kind="OperationPlan",
        metadata=PlanMetadata(
            planId=plan_id,
            createdAt=datetime.now(UTC),
            operationKind="proxmox.vm-create",
            target=operation_input.target,
            site=operation_input.site,
            risk="medium",
            idempotencyKey=(
                f"proxmox.vm-create:{operation_input.target}:{operation_input.vm.vmid}"
            ),
        ),
        source=PlanSource(
            input=SourceReference(
                path=input_path,
                digest=file_digest(input_path),
            ),
            provider=SourceReference(
                path=provider_path,
                digest=file_digest(provider_path),
            ),
        ),
        safety=SafetyPolicy(
            requiresConfirm=provider_definition.safety.require_confirm,
            requiresRollback=True,
            maxPlanAgeSeconds=provider_definition.safety.max_plan_age_seconds,
            allowedOnlyIfPreflightPasses=True,
        ),
        provider=ProviderReference(
            name="proxmox",
            mode="live",
            node=operation_input.vm.node,
        ),
        spec=spec,
        preflight=PreflightResult(status="failed"),
        changes=[
            {
                "action": "create",
                "provider": "proxmox",
                "resource": f"qemu/{operation_input.vm.vmid}",
                "name": operation_input.vm.name,
            }
        ],
        apply=OperationPhase(steps=_apply_steps()),
        verify=OperationPhase(steps=_verify_steps()),
        rollback=RollbackPhase(supported=True, steps=_rollback_steps()),
    )
    return plan


def _with_preflight(plan: OperationPlan, preflight: VerifyResult) -> OperationPlan:
    data = plan.as_artifact(exclude_none=True)
    data["preflight"] = {
        "status": preflight.status,
        "checks": [check.model_dump(mode="json", by_alias=True) for check in preflight.checks],
    }
    data["metadata"].pop("fingerprint", None)
    return OperationPlan.model_validate(data)


def _merge_preflight(local: VerifyResult, provider: VerifyResult) -> VerifyResult:
    checks = [*local.checks, *provider.checks]
    status = "failed" if any(check.status == "failed" for check in checks) else "passed"
    return VerifyResult(status=status, checks=checks)


def _local_preflight(plan: OperationPlan) -> VerifyResult:
    spec = plan.spec
    policy = spec["policy"]
    network = spec["network"]
    checks: list[CheckResult] = []
    checks.append(
        _check(
            "input.create-allowed",
            bool(policy["createAllowed"]),
            str(policy["createAllowed"]),
        )
    )
    checks.append(
        _check(
            "input.vm-name",
            str(spec["name"]) == plan.metadata.target,
            f"{spec['name']} == {plan.metadata.target}",
        )
    )
    checks.append(
        _check("input.hostname-policy", _valid_hostname(str(spec["name"])), str(spec["name"]))
    )
    checks.append(
        _network_cidr_check(
            str(network["ip"]),
            int(network["prefix"]),
            str(network["gateway"]),
        )
    )
    checks.append(_ping_check(str(network["ip"])))
    checks.append(
        _check(
            "rollback.delete-allowed",
            bool(policy["rollbackDeleteAllowed"]),
            str(policy["rollbackDeleteAllowed"]),
        )
    )
    status = "failed" if any(check.status == "failed" for check in checks) else "passed"
    return VerifyResult(status=status, checks=checks)


def _apply_steps() -> list[OperationStep]:
    return [
        OperationStep(
            id="clone-template",
            description="Clone cloud-init template",
            provider="proxmox",
            action="clone-template",
        ),
        OperationStep(
            id="set-ownership",
            description="Write Atlas ownership marker",
            provider="proxmox",
            action="set-ownership",
        ),
        OperationStep(
            id="set-tags", description="Set Proxmox tags", provider="proxmox", action="set-tags"
        ),
        OperationStep(
            id="set-resources",
            description="Set CPU and memory",
            provider="proxmox",
            action="set-resources",
        ),
        OperationStep(
            id="resize-disk",
            description="Resize primary disk",
            provider="proxmox",
            action="resize-disk",
        ),
        OperationStep(
            id="set-network",
            description="Set network bridge and VLAN",
            provider="proxmox",
            action="set-network",
        ),
        OperationStep(
            id="set-cloud-init",
            description="Set cloud-init user and static IPv4",
            provider="proxmox",
            action="set-cloud-init",
        ),
        OperationStep(
            id="set-agent",
            description="Enable qemu guest agent",
            provider="proxmox",
            action="set-agent",
        ),
        OperationStep(id="start-vm", description="Start VM", provider="proxmox", action="start-vm"),
        OperationStep(
            id="wait-guest-agent",
            description="Wait for guest agent",
            provider="proxmox",
            action="wait-guest-agent",
        ),
        OperationStep(
            id="wait-ssh",
            description="Wait for SSH TCP reachability",
            provider="proxmox",
            action="wait-ssh",
        ),
    ]


def _verify_steps() -> list[OperationStep]:
    return [
        OperationStep(
            id="verify-vm",
            description="Verify VM existence and metadata",
            provider="proxmox",
            action="verify-vm",
        ),
        OperationStep(
            id="verify-guest-agent",
            description="Verify qemu guest agent",
            provider="proxmox",
            action="verify-guest-agent",
        ),
        OperationStep(
            id="verify-ssh",
            description="Verify SSH TCP reachability",
            provider="proxmox",
            action="verify-ssh",
        ),
    ]


def _rollback_steps() -> list[OperationStep]:
    return [
        OperationStep(
            id="verify-created-resource",
            description="Verify VM matches plan evidence",
            provider="proxmox",
            action="verify-created-resource",
        ),
        OperationStep(
            id="stop-vm", description="Stop VM if running", provider="proxmox", action="stop-vm"
        ),
        OperationStep(
            id="delete-vm",
            description="Delete VM with purge",
            provider="proxmox",
            action="delete-vm",
        ),
        OperationStep(
            id="verify-deleted",
            description="Verify VMID no longer exists",
            provider="proxmox",
            action="verify-deleted",
        ),
    ]


def _evidence_verify(result: VerifyResult) -> EvidenceVerify:
    return EvidenceVerify(status=result.status, checks=result.checks)


def _evidence_plan(plan: OperationPlan) -> EvidencePlanSnapshot:
    fingerprint = plan.metadata.fingerprint
    if not fingerprint:
        raise ProviderError("plan fingerprint is missing")
    return EvidencePlanSnapshot(
        fingerprint=fingerprint, snapshot=plan.as_artifact(exclude_none=True)
    )


def _vm_resource(plan: OperationPlan, *, created_by_step: str) -> EvidenceResource:
    return EvidenceResource(
        provider=plan.provider.name,
        type="proxmox.qemu",
        id=f"qemu/{int(plan.spec['vmid'])}",
        name=str(plan.spec["name"]),
        node=plan.provider.node,
        vmid=int(plan.spec["vmid"]),
        createdByStep=created_by_step,
        ownershipMarkerWritten=False,
    )


def _mark_ownership_written(resource: EvidenceResource) -> EvidenceResource:
    data = resource.model_dump(mode="json", by_alias=True)
    data["ownershipMarkerWritten"] = True
    return EvidenceResource.model_validate(data)


def _rollback_step_with_evidence(
    step: OperationStep,
    evidence: OperationEvidence,
    plan: OperationPlan,
) -> OperationStep:
    if step.action != "verify-created-resource":
        return step
    resource = _created_resource(evidence, int(plan.spec["vmid"]))
    data = step.model_dump(mode="json", by_alias=True)
    data["params"] = {
        **data.get("params", {}),
        "evidenceResourceId": resource.id,
        "evidenceResourceType": resource.type,
        "evidenceResourceName": resource.name,
        "evidenceResourceNode": resource.node,
        "evidenceResourceVmid": resource.vmid,
        "evidenceCreatedByStep": resource.created_by_step,
        "evidenceOwnershipMarkerWritten": resource.ownership_marker_written,
    }
    return OperationStep.model_validate(data)


def _created_resource(evidence: OperationEvidence, vmid: int) -> EvidenceResource:
    for resource in evidence.created_resources:
        if resource.vmid == vmid and resource.type in {"proxmox.qemu", "proxmox.qemu-template"}:
            return resource
    raise ProviderError("rollback evidence does not include the created resource")


def _has_created_vm_resource(resources: list[EvidenceResource]) -> bool:
    return any(
        resource.type == "proxmox.qemu" and resource.vmid is not None for resource in resources
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _valid_hostname(value: str) -> bool:
    if len(value) > 63 or not value:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    return value[0].isalnum() and value[-1].isalnum() and set(value) <= allowed


def _network_cidr_check(ip: str, prefix: int, gateway: str) -> CheckResult:
    try:
        network = ip_network(f"{gateway}/{prefix}", strict=False)
        passed = ip_address(ip) in network and ip_address(gateway) in network
        return _check("network.ip.cidr", passed, str(network))
    except ValueError as exc:
        return CheckResult(name="network.ip.cidr", status="failed", message=str(exc))


def _ping_check(ip: str) -> CheckResult:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return CheckResult(
            name="network.ip.reachable", status="warning", message="ping unavailable"
        )
    return _check("network.ip.unused", result.returncode != 0, ip)


def _check(name: str, passed: bool, message: str) -> CheckResult:
    return CheckResult(name=name, status="passed" if passed else "failed", message=message)
