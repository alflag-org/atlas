from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from atlas_operations.operation.config import (
    ProviderDefinition,
    SafetyConfig,
    VmTemplateCreateInput,
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

_IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 60


def build_vm_template_create_plan(
    provider_definition: ProviderDefinition,
    operation_input: VmTemplateCreateInput,
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
    preflight = _merge_preflight(_local_image_preflight(plan), provider.preflight(plan))
    plan = _with_preflight(plan, preflight)
    return set_fingerprint(plan)


def apply_vm_template_create(
    safety: SafetyConfig,
    *,
    plan: OperationPlan,
    provider: ProviderClient,
    confirm: str | None,
    progress: Callable[[str], None] | None = None,
) -> OperationEvidence:
    validate_fingerprint(plan)
    preflight = _merge_preflight(_local_image_preflight(plan), provider.preflight(plan))
    SafetyGate(safety).validate_apply(
        plan,
        confirm=confirm,
        preflight_passed=preflight.status == "passed",
    )

    image_paths: tuple[Path, str] | None = None
    steps: list[EvidenceStep] = []
    created_resources: list[EvidenceResource] = []
    result = "success"
    started = datetime.now(UTC)
    for step in plan.apply.steps:
        if progress:
            progress(f"apply: {step.id}")
        step_started = datetime.now(UTC)
        try:
            if step.action == "download-image":
                image_paths = _prepare_shared_image(plan)
                runner_path, node_path = image_paths
                step_result = _local_step_result(
                    step,
                    {"runnerPath": str(runner_path), "nodePath": node_path},
                )
            elif step.action == "verify-image-checksum":
                if image_paths is None:
                    raise ProviderError("image has not been prepared")
                runner_path, _node_path = image_paths
                _verify_image_checksum(runner_path, str(plan.spec["image"]["checksum"]))
                step_result = _local_step_result(
                    step,
                    {"checksum": plan.spec["image"]["checksum"]},
                )
            else:
                if step.action == "template-import-disk":
                    if image_paths is None:
                        raise ProviderError("image has not been prepared")
                    _runner_path, node_path = image_paths
                    step = _step_with_params(step, {"imagePath": node_path})
                step_result = provider.apply_step(step, plan)
        except Exception as exc:
            step_result = _local_step_result(step, {}, status="failed", message=str(exc))
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
        if step_result.status == "success" and step.id == "create-vm":
            created_resources.append(_template_resource(plan, created_by_step=step.id))
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
            supported=plan.rollback.supported and _has_template_resource(created_resources)
        ),
    )
    return evidence


def verify_vm_template_create(
    *,
    plan: OperationPlan,
    provider: ProviderClient,
) -> VerifyResult:
    validate_fingerprint(plan)
    return provider.verify(plan)


def rollback_vm_template_create(
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


def _initial_plan(
    provider_definition: ProviderDefinition,
    operation_input: VmTemplateCreateInput,
    *,
    input_path: str,
    provider_path: str,
) -> OperationPlan:
    spec = operation_input.to_plan_spec()
    plan_id = _new_id("plan")
    return OperationPlan(
        apiVersion="atlas.operation/v1",
        kind="OperationPlan",
        metadata=PlanMetadata(
            planId=plan_id,
            createdAt=datetime.now(UTC),
            operationKind="proxmox.vm-template-create",
            target=operation_input.target,
            site=operation_input.site,
            risk="medium",
            idempotencyKey=(
                f"proxmox.vm-template-create:{operation_input.target}:{operation_input.vmid}"
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
            node=operation_input.node,
        ),
        spec=spec,
        preflight=PreflightResult(status="failed"),
        changes=[
            {
                "action": "create-template",
                "provider": "proxmox",
                "resource": f"qemu/{operation_input.vmid}",
                "name": operation_input.name,
            }
        ],
        apply=OperationPhase(steps=_apply_steps()),
        verify=OperationPhase(steps=_verify_steps()),
        rollback=RollbackPhase(supported=True, steps=_rollback_steps()),
    )


def _merge_preflight(local: VerifyResult, provider: VerifyResult) -> VerifyResult:
    checks = [*local.checks, *provider.checks]
    status = "failed" if any(check.status == "failed" for check in checks) else "passed"
    return VerifyResult(status=status, checks=checks)


def _local_image_preflight(plan: OperationPlan) -> VerifyResult:
    policy = plan.spec["policy"]
    transfer = plan.spec["image"].get("transfer") or {}
    runner_path = transfer.get("runnerPath")
    node_path = transfer.get("nodePath")
    checks = [
        _check(
            "input.create-allowed",
            bool(policy["createAllowed"]),
            str(policy["createAllowed"]),
        ),
        _check(
            "rollback.delete-allowed",
            bool(policy["rollbackDeleteAllowed"]),
            str(policy["rollbackDeleteAllowed"]),
        ),
        _check(
            "template.image.transfer-mode", transfer.get("mode") == "shared-path", "shared-path"
        ),
        _check("template.image.runner-path", bool(runner_path), str(runner_path or "")),
        _check("template.image.node-path", bool(node_path), str(node_path or "")),
    ]
    if runner_path:
        checks.extend(_runner_path_checks(Path(str(runner_path))))
    status = "failed" if any(check.status == "failed" for check in checks) else "passed"
    return VerifyResult(status=status, checks=checks)


def _runner_path_checks(path: Path) -> list[CheckResult]:
    checks: list[CheckResult] = []
    if path.is_symlink():
        return [
            _check("template.image.runner-path.file", False, f"symlink rejected: {path}")
        ]
    if path.exists():
        checks.append(
            _check(
                "template.image.runner-path.file",
                path.is_file() and not path.is_symlink(),
                str(path),
            )
        )
        checks.append(
            _check("template.image.runner-path.readable", os.access(path, os.R_OK), str(path))
        )
        checks.append(
            _check(
                "template.image.runner-path.parent-writable",
                os.access(path.parent, os.W_OK),
                str(path.parent),
            )
        )
        return checks

    parent = _nearest_existing_parent(path)
    checks.append(
        _check(
            "template.image.runner-path.creatable",
            parent is not None and parent.is_dir() and os.access(parent, os.W_OK | os.X_OK),
            str(parent or path.parent),
        )
    )
    return checks


def _nearest_existing_parent(path: Path) -> Path | None:
    for candidate in [path.parent, *path.parents]:
        if candidate.exists():
            return None if candidate.is_symlink() else candidate
    return None


def _prepare_shared_image(plan: OperationPlan) -> tuple[Path, str]:
    transfer = plan.spec["image"].get("transfer") or {}
    if (
        transfer.get("mode") != "shared-path"
        or not transfer.get("runnerPath")
        or not transfer.get("nodePath")
    ):
        raise ProviderError(
            "template image import requires transfer.runnerPath and transfer.nodePath"
        )
    checksum = str(plan.spec["image"]["checksum"])
    image_path = Path(str(transfer["runnerPath"]))
    node_path = str(transfer["nodePath"])
    if image_path.is_symlink():
        raise ProviderError(f"template image path is unsafe: {image_path}")
    if image_path.exists():
        if not image_path.is_file() or image_path.is_symlink():
            raise ProviderError(f"template image path is unsafe: {image_path}")
        _verify_image_checksum(image_path, checksum)
        return image_path, node_path

    image_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = image_path.with_name(f".{image_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        source_url = str(plan.spec["image"]["url"])
        with urllib.request.urlopen(
            source_url,
            timeout=_IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
        ) as response:
            final_url = str(response.geturl())
            parsed = urlparse(final_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ProviderError(f"template image redirect is unsafe: {final_url}")
            with temporary.open("xb") as destination:
                shutil.copyfileobj(response, destination)
        _verify_image_checksum(temporary, checksum)
        os.replace(temporary, image_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return image_path, node_path


def _verify_image_checksum(image_path: Path, checksum: str) -> None:
    actual = _sha256_file(image_path)
    if actual != checksum:
        raise ProviderError(f"image checksum mismatch: expected {checksum}, got {actual}")


def _with_preflight(plan: OperationPlan, preflight: VerifyResult) -> OperationPlan:
    data = plan.as_artifact(exclude_none=True)
    data["preflight"] = {
        "status": preflight.status,
        "checks": [check.model_dump(mode="json", by_alias=True) for check in preflight.checks],
    }
    data["metadata"].pop("fingerprint", None)
    return OperationPlan.model_validate(data)


def _apply_steps() -> list[OperationStep]:
    return [
        OperationStep(
            id="download-image",
            description="Download cloud image",
            provider="proxmox",
            action="download-image",
        ),
        OperationStep(
            id="verify-image-checksum",
            description="Verify image checksum",
            provider="proxmox",
            action="verify-image-checksum",
        ),
        OperationStep(
            id="create-vm",
            description="Create temporary VM",
            provider="proxmox",
            action="template-create-vm",
        ),
        OperationStep(
            id="import-disk",
            description="Import template disk",
            provider="proxmox",
            action="template-import-disk",
        ),
        OperationStep(
            id="attach-disk",
            description="Attach imported disk",
            provider="proxmox",
            action="template-attach-disk",
        ),
        OperationStep(
            id="add-cloud-init",
            description="Add cloud-init drive",
            provider="proxmox",
            action="template-add-cloud-init",
        ),
        OperationStep(
            id="configure-boot",
            description="Configure boot order",
            provider="proxmox",
            action="template-configure-boot",
        ),
        OperationStep(
            id="configure-serial",
            description="Configure serial console",
            provider="proxmox",
            action="template-configure-serial",
        ),
        OperationStep(
            id="enable-agent",
            description="Enable qemu guest agent",
            provider="proxmox",
            action="template-enable-agent",
        ),
        OperationStep(
            id="set-tags", description="Set template tags", provider="proxmox", action="set-tags"
        ),
        OperationStep(
            id="set-ownership",
            description="Write Atlas ownership marker",
            provider="proxmox",
            action="set-ownership",
        ),
        OperationStep(
            id="convert-template",
            description="Convert VM to template",
            provider="proxmox",
            action="template-convert",
        ),
    ]


def _verify_steps() -> list[OperationStep]:
    return [
        OperationStep(
            id="verify-template",
            description="Verify template flag and metadata",
            provider="proxmox",
            action="verify-template",
        ),
        OperationStep(
            id="verify-cloud-init",
            description="Verify cloud-init drive",
            provider="proxmox",
            action="verify-cloud-init",
        ),
        OperationStep(
            id="verify-agent",
            description="Verify guest agent setting",
            provider="proxmox",
            action="verify-agent",
        ),
    ]


def _rollback_steps() -> list[OperationStep]:
    return [
        OperationStep(
            id="verify-created-template",
            description="Verify template matches plan evidence",
            provider="proxmox",
            action="verify-created-template",
        ),
        OperationStep(
            id="delete-template",
            description="Delete template VM with purge",
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


def _template_resource(plan: OperationPlan, *, created_by_step: str) -> EvidenceResource:
    return EvidenceResource(
        provider=plan.provider.name,
        type="proxmox.qemu-template",
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
    if step.action != "verify-created-template":
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


def _has_template_resource(resources: list[EvidenceResource]) -> bool:
    return any(
        resource.type == "proxmox.qemu-template" and resource.vmid is not None
        for resource in resources
    )


def _evidence_verify(result: VerifyResult) -> EvidenceVerify:
    return EvidenceVerify(status=result.status, checks=result.checks)


def _check(name: str, passed: bool, message: str) -> CheckResult:
    return CheckResult(name=name, status="passed" if passed else "failed", message=message)


def _local_step_result(
    step: OperationStep,
    details: dict[str, str],
    *,
    status: str = "success",
    message: str = "",
):
    from atlas_operations.operation.provider import StepResult

    return StepResult(id=step.id, status=status, message=message, details=details)


def _evidence_plan(plan: OperationPlan) -> EvidencePlanSnapshot:
    fingerprint = plan.metadata.fingerprint
    if not fingerprint:
        raise ProviderError("plan fingerprint is missing")
    return EvidencePlanSnapshot(
        fingerprint=fingerprint, snapshot=plan.as_artifact(exclude_none=True)
    )


def _step_with_params(step: OperationStep, params: dict[str, str]) -> OperationStep:
    data = step.model_dump(mode="json", by_alias=True)
    data["params"] = {**data.get("params", {}), **params}
    return OperationStep.model_validate(data)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"
