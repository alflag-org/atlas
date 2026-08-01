from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from atlas_operations.operation.errors import PlanError
from atlas_operations.operation.evidence import OperationEvidence
from atlas_operations.operation.fingerprint import validate_fingerprint
from atlas_operations.operation.plan import OperationPlan
from atlas_operations.operation.plan_io import load_plan
from atlas_operations.operation.safety import IMPLEMENTED_OPERATION_KINDS

_PROVIDER_BY_OPERATION = {
    "proxmox.vm-create": "proxmox",
    "proxmox.vm-template-create": "proxmox",
}
_PHASE_STEPS_BY_OPERATION = {
    "proxmox.vm-create": {
        "apply": (
            ("clone-template", "clone-template"),
            ("set-ownership", "set-ownership"),
            ("set-tags", "set-tags"),
            ("set-resources", "set-resources"),
            ("resize-disk", "resize-disk"),
            ("set-network", "set-network"),
            ("set-cloud-init", "set-cloud-init"),
            ("set-agent", "set-agent"),
            ("start-vm", "start-vm"),
            ("wait-guest-agent", "wait-guest-agent"),
            ("wait-ssh", "wait-ssh"),
        ),
        "verify": (
            ("verify-vm", "verify-vm"),
            ("verify-guest-agent", "verify-guest-agent"),
            ("verify-ssh", "verify-ssh"),
        ),
        "rollback": (
            ("verify-created-resource", "verify-created-resource"),
            ("stop-vm", "stop-vm"),
            ("delete-vm", "delete-vm"),
            ("verify-deleted", "verify-deleted"),
        ),
    },
    "proxmox.vm-template-create": {
        "apply": (
            ("download-image", "download-image"),
            ("verify-image-checksum", "verify-image-checksum"),
            ("create-vm", "template-create-vm"),
            ("import-disk", "template-import-disk"),
            ("attach-disk", "template-attach-disk"),
            ("add-cloud-init", "template-add-cloud-init"),
            ("configure-boot", "template-configure-boot"),
            ("configure-serial", "template-configure-serial"),
            ("enable-agent", "template-enable-agent"),
            ("set-tags", "set-tags"),
            ("set-ownership", "set-ownership"),
            ("convert-template", "template-convert"),
        ),
        "verify": (
            ("verify-template", "verify-template"),
            ("verify-cloud-init", "verify-cloud-init"),
            ("verify-agent", "verify-agent"),
        ),
        "rollback": (
            ("verify-created-template", "verify-created-template"),
            ("delete-template", "delete-vm"),
            ("verify-deleted", "verify-deleted"),
        ),
    },
}


def validate_plan_file(path: str | Path) -> OperationPlan:
    plan = load_plan(path)
    validate_plan(plan)
    return plan


def validate_artifact_data(data: dict[str, Any]) -> OperationPlan | OperationEvidence:
    kind = data.get("kind")
    try:
        if kind == "OperationPlan":
            plan = OperationPlan.model_validate(
                data,
                by_alias=True,
                by_name=False,
            )
            validate_plan(plan)
            return plan
        if kind == "OperationEvidence":
            evidence = OperationEvidence.model_validate(
                data,
                by_alias=True,
                by_name=False,
            )
            validate_evidence(evidence)
            return evidence
    except ValidationError as exc:
        raise PlanError(f"invalid {kind or 'operation'} artifact: {exc}") from exc
    raise PlanError(f"unsupported artifact kind: {kind}")


def validate_plan(plan: OperationPlan) -> None:
    operation_kind = plan.metadata.operation_kind
    if operation_kind not in IMPLEMENTED_OPERATION_KINDS:
        raise PlanError(f"operation is not implemented: {operation_kind}")
    provider = _PROVIDER_BY_OPERATION[operation_kind]
    if plan.provider.name != provider:
        raise PlanError(
            f"provider mismatch: operation {operation_kind} uses {provider}, "
            f"plan uses {plan.provider.name}"
        )
    validate_fingerprint(plan)
    _validate_phase_steps("apply", plan.apply.steps, plan.provider.name)
    _validate_phase_steps("verify", plan.verify.steps, plan.provider.name)
    _validate_phase_steps("rollback", plan.rollback.steps, plan.provider.name)
    _validate_exact_phase_steps(plan)
    if plan.safety.requires_rollback and not plan.rollback.supported:
        raise PlanError("rollback.supported conflicts with safety.requiresRollback")
    _validate_operation_spec(plan)


def validate_evidence(evidence: OperationEvidence) -> None:
    operation_kind = evidence.metadata.operation_kind
    if operation_kind not in IMPLEMENTED_OPERATION_KINDS:
        raise PlanError(f"operation is not implemented: {operation_kind}")
    provider = _PROVIDER_BY_OPERATION[operation_kind]
    if evidence.provider.name != provider:
        raise PlanError(
            f"provider mismatch: operation {operation_kind} uses {provider}, "
            f"evidence uses {evidence.provider.name}"
        )
    if not evidence.metadata.plan_id:
        raise PlanError("evidence metadata.planId is missing")
    for step in evidence.steps:
        if not step.id:
            raise PlanError("evidence step id is missing")
    if evidence.rollback.supported and not evidence.created_resources:
        raise PlanError("rollback-capable evidence must include createdResources")
    if not evidence.plan.snapshot:
        raise PlanError("evidence plan snapshot is missing")
    try:
        plan = OperationPlan.model_validate(
            evidence.plan.snapshot,
            by_alias=True,
            by_name=False,
        )
    except ValidationError as exc:
        raise PlanError(f"invalid evidence plan snapshot: {exc}") from exc
    if evidence.plan.fingerprint != plan.metadata.fingerprint:
        raise PlanError("evidence plan fingerprint does not match snapshot")
    validate_plan(plan)
    if plan.metadata.plan_id != evidence.metadata.plan_id:
        raise PlanError("evidence plan snapshot belongs to a different plan")
    if plan.metadata.operation_kind != evidence.metadata.operation_kind:
        raise PlanError("evidence operation kind does not match plan snapshot")
    if plan.metadata.target != evidence.metadata.target:
        raise PlanError("evidence target does not match plan snapshot")


def _validate_phase_steps(phase: str, steps: list[Any], provider_name: str) -> None:
    for step in steps:
        if not step.id:
            raise PlanError(f"{phase} step id is missing")
        if not step.action:
            raise PlanError(f"{phase} step action is missing")
        if step.provider != provider_name:
            raise PlanError(
                f"{phase} step {step.id} provider mismatch: {step.provider} != {provider_name}"
            )


def _validate_exact_phase_steps(plan: OperationPlan) -> None:
    expected = _PHASE_STEPS_BY_OPERATION[plan.metadata.operation_kind]
    actual = {
        "apply": tuple((step.id, step.action) for step in plan.apply.steps),
        "verify": tuple((step.id, step.action) for step in plan.verify.steps),
        "rollback": tuple((step.id, step.action) for step in plan.rollback.steps),
    }
    for phase in ("apply", "verify", "rollback"):
        if actual[phase] != expected[phase]:
            raise PlanError(
                f"{phase} steps do not match the implemented "
                f"{plan.metadata.operation_kind} contract"
            )


def _validate_operation_spec(plan: OperationPlan) -> None:
    if plan.metadata.operation_kind == "proxmox.vm-create":
        _require_spec_keys(
            plan.spec,
            "policy",
            "vmid",
            "name",
            "templateVmid",
            "disk",
            "network",
            "guest",
        )
        _require_spec_keys(plan.spec["disk"], "device", "sizeGb", "storage")
        _require_spec_keys(plan.spec["network"], "bridge", "vlan", "ip", "gateway")
        return
    if plan.metadata.operation_kind == "proxmox.vm-template-create":
        _require_spec_keys(
            plan.spec,
            "policy",
            "vmid",
            "name",
            "image",
            "resources",
            "network",
            "guest",
        )
        _require_spec_keys(plan.spec["image"], "url", "checksum", "transfer")
        _require_spec_keys(
            plan.spec["resources"], "memoryMb", "cores", "diskGb", "storage", "diskDevice"
        )
        _require_spec_keys(plan.spec["network"], "bridge", "vlan")
        transfer = plan.spec["image"]["transfer"]
        if transfer.get("mode") != "shared-path":
            raise PlanError("template image transfer mode must be shared-path")
        if not transfer.get("runnerPath"):
            raise PlanError("template image transfer requires image.transfer.runnerPath")
        if not transfer.get("nodePath"):
            raise PlanError("template image transfer requires image.transfer.nodePath")
        return
    raise PlanError(
        f"operation-specific validation is not implemented for {plan.metadata.operation_kind}"
    )


def _require_spec_keys(data: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise PlanError(f"spec is missing required key(s): {', '.join(missing)}")
