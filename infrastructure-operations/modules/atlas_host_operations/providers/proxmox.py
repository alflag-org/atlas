"""Proxmox host adapter delegated to private reviewed-operation jobs."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from atlas_host_operations.errors import AdapterError, UnknownProviderResult
from atlas_host_operations.models import (
    CheckResult,
    ProviderEvidence,
    ProviderObservation,
    RegistryAuthority,
    RollbackResult,
    VerificationResult,
)
from atlas_host_operations.providers.base import HostContext
from atlas_host_operations.subprocesses import (
    ChildResult,
    CommandRunner,
    SubprocessRunner,
    job_argv,
)

INFRASTRUCTURE_RELEASE = "infrastructure-operations"


class ProxmoxHostProvider:
    name = "proxmox"
    resource_type = "proxmox.qemu"

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()
        self._plan: dict[str, Any] = {}

    def validate(self, context: HostContext) -> list[CheckResult]:
        result = self._runner.run(
            job_argv(
                INFRASTRUCTURE_RELEASE,
                "vm-create-plan",
                [
                    context.plan.sources.provider_definition.path,
                    context.plan.sources.provider_input.path,
                ],
            ),
            timeout_seconds=300,
        )
        if result.return_code != 0:
            return [
                CheckResult(
                    name="provider.proxmox.plan",
                    status="failed",
                    message=_diagnostic(result),
                )
            ]
        self._plan = _json_object(result, "vm-create-plan")
        identity = _plan_identity(self._plan)
        metadata = self._plan.get("metadata")
        spec = self._plan.get("spec")
        network = spec.get("network") if isinstance(spec, dict) else None
        guest = spec.get("guest") if isinstance(spec, dict) else None
        passed = (
            identity is not None
            and identity[0] == context.plan.resource.name
            and isinstance(metadata, dict)
            and metadata.get("site") == context.plan.metadata.site
            and isinstance(spec, dict)
            and spec.get("name") == context.plan.resource.name
            and isinstance(network, dict)
            and network.get("ip") == context.plan.readiness.address
            and isinstance(guest, dict)
            and guest.get("sshPort") == context.plan.readiness.ssh_port
            and (
                not context.plan.readiness.require_guest_agent
                or guest.get("qemuAgent") is True
            )
        )
        return [
            CheckResult(
                name="provider.proxmox.plan",
                status="passed" if passed else "failed",
                message=""
                if passed
                else "provider plan target does not match host target",
            )
        ]

    def planning_artifact(self) -> dict[str, object]:
        return dict(self._plan)

    def allocate(
        self,
        context: HostContext,
        authority: RegistryAuthority,
    ) -> ProviderEvidence:
        child_plan = context.plan.provider.plan
        metadata = child_plan.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("planId"), str
        ):
            raise AdapterError("Proxmox child plan is missing its plan ID")
        result = self._runner.run(
            job_argv(
                INFRASTRUCTURE_RELEASE,
                "vm-create-apply",
                [
                    context.plan.sources.provider_definition.path,
                    "-",
                    "--confirm",
                    metadata["planId"],
                ],
            ),
            input_text=json.dumps(child_plan),
            timeout_seconds=1800,
        )
        if result.timed_out:
            raise UnknownProviderResult(
                "vm-create-apply timed out; reconcile is required"
            )
        evidence = _json_object_or_none(result)
        if result.return_code != 0:
            if evidence and evidence.get("createdResources"):
                raise UnknownProviderResult(
                    "vm-create-apply left provider evidence; reconcile is required"
                )
            raise AdapterError(f"vm-create-apply failed: {_diagnostic(result)}")
        if evidence is None:
            raise UnknownProviderResult(
                "vm-create-apply succeeded without valid evidence; reconcile is required"
            )
        resources = evidence.get("createdResources")
        if (
            not isinstance(resources, list)
            or len(resources) != 1
            or not isinstance(resources[0], dict)
        ):
            raise UnknownProviderResult(
                "vm-create-apply did not return exactly one created resource; "
                "reconcile is required"
            )
        resource = resources[0]
        resource_id = resource.get("id")
        resource_name = resource.get("name")
        resource_type = resource.get("type")
        if not all(
            isinstance(value, str) and value
            for value in (resource_id, resource_name, resource_type)
        ):
            raise UnknownProviderResult(
                "vm-create-apply returned an invalid resource identity; reconcile is required"
            )
        identity = _plan_identity(child_plan)
        if identity is None:
            raise UnknownProviderResult(
                "vm-create-apply used a plan without a resource identity; "
                "reconcile is required"
            )
        target, node, vmid = identity
        if (
            resource_name != target
            or resource_id != f"qemu/{vmid}"
            or resource_type != self.resource_type
            or resource.get("node") != node
            or resource.get("vmid") != vmid
        ):
            raise UnknownProviderResult(
                "vm-create-apply evidence does not match the child plan; "
                "reconcile is required"
            )
        return ProviderEvidence(
            provider=self.name,
            resourceType=resource_type,
            resourceId=resource_id,
            resourceName=resource_name,
            locator={
                key: resource[key]
                for key in ("node", "vmid")
                if resource.get(key) is not None
            },
            ownershipMarker=bool(resource.get("ownershipMarkerWritten")),
            details={
                "childEvidence": evidence,
                "childPlan": child_plan,
                "fencingToken": authority.fencing_token,
            },
        )

    def observe(self, context: HostContext) -> ProviderObservation:
        _result, payload = self._verify_child(context, _provider_artifact(context))
        exists = _check_passed(payload, "proxmox.vm.exists")
        running = _check_passed(payload, "proxmox.vm.running")
        guest_agent_ready = _check_passed(payload, "proxmox.guest-agent")
        address_ready = _check_passed(payload, "proxmox.guest-agent.ip")
        ownership_marker = _check_passed(payload, "proxmox.ownership-marker")
        recovered = (
            _recovered_provider_evidence(context)
            if exists and ownership_marker
            else None
        )
        return ProviderObservation(
            exists=exists,
            running=running,
            guestAgentReady=guest_agent_ready,
            addresses=[context.plan.readiness.address] if address_ready else [],
            absenceConfirmed=False,
            providerEvidence=recovered,
            details=payload,
        )

    def verify(
        self,
        context: HostContext,
        evidence: ProviderEvidence,
    ) -> VerificationResult:
        artifact = evidence.details.get("childEvidence")
        if not isinstance(artifact, dict):
            raise AdapterError("Proxmox evidence is missing its child artifact")
        _result, payload = self._verify_child(context, artifact)
        status = payload.get("status")
        raw_checks = payload["checks"]
        try:
            checks = [CheckResult.model_validate(check) for check in raw_checks]
        except ValidationError as exc:
            raise AdapterError("vm-create-verify returned invalid checks") from exc
        return VerificationResult(status=status, checks=checks, details=payload)

    def rollback(
        self,
        context: HostContext,
        evidence: ProviderEvidence,
        authority: RegistryAuthority,
    ) -> RollbackResult:
        if not evidence.ownership_marker:
            return RollbackResult(
                status="failed", message="ownership marker is missing"
            )
        if evidence.details.get("recoveredFromLiveState") is True:
            return RollbackResult(
                status="failed",
                message="original vm-create evidence is unavailable; provider was retained",
            )
        artifact = evidence.details.get("childEvidence")
        child_plan = evidence.details.get("childPlan")
        metadata = child_plan.get("metadata") if isinstance(child_plan, dict) else None
        if not isinstance(artifact, dict) or not isinstance(metadata, dict):
            raise AdapterError("Proxmox rollback evidence is incomplete")
        plan_id = metadata.get("planId")
        if not isinstance(plan_id, str):
            raise AdapterError("Proxmox rollback plan ID is missing")
        result = self._runner.run(
            job_argv(
                INFRASTRUCTURE_RELEASE,
                "vm-create-rollback",
                [
                    context.plan.sources.provider_definition.path,
                    "-",
                    "--confirm",
                    plan_id,
                ],
            ),
            input_text=json.dumps(artifact),
            timeout_seconds=1800,
        )
        if result.timed_out:
            raise UnknownProviderResult(
                "vm-create-rollback timed out; reconcile is required"
            )
        payload = _json_object_or_none(result)
        if result.return_code not in (0, 1) or payload is None:
            raise AdapterError(f"vm-create-rollback failed: {_diagnostic(result)}")
        rollback = payload.get("rollback")
        succeeded = isinstance(rollback, dict) and rollback.get("result") == "success"
        return RollbackResult(
            status="succeeded" if succeeded else "failed",
            details={"childEvidence": payload, "fencingToken": authority.fencing_token},
        )

    def _verify_child(
        self,
        context: HostContext,
        artifact: dict[str, Any],
    ) -> tuple[ChildResult, dict[str, Any]]:
        result = self._runner.run(
            job_argv(
                INFRASTRUCTURE_RELEASE,
                "vm-create-verify",
                [context.plan.sources.provider_definition.path, "-"],
            ),
            input_text=json.dumps(artifact),
            timeout_seconds=300,
        )
        if result.timed_out:
            raise AdapterError("vm-create-verify timed out")
        payload = _json_object_or_none(result)
        if payload is None:
            raise AdapterError("vm-create-verify returned invalid JSON")
        if result.return_code not in (0, 1):
            raise AdapterError(f"vm-create-verify failed: {_diagnostic(result)}")
        if payload.get("status") not in {"passed", "failed", "warning"}:
            raise AdapterError("vm-create-verify returned an invalid status")
        raw_checks = payload.get("checks")
        if not isinstance(raw_checks, list) or not all(
            isinstance(check, dict) for check in raw_checks
        ):
            raise AdapterError("vm-create-verify returned invalid checks")
        return result, payload


def _provider_artifact(context: HostContext) -> dict[str, Any]:
    return context.plan.provider.plan


def _plan_identity(plan: dict[str, Any]) -> tuple[str, str, int] | None:
    metadata = plan.get("metadata")
    provider = plan.get("provider")
    spec = plan.get("spec")
    target = metadata.get("target") if isinstance(metadata, dict) else None
    node = provider.get("node") if isinstance(provider, dict) else None
    vmid = spec.get("vmid") if isinstance(spec, dict) else None
    if (
        not isinstance(target, str)
        or not isinstance(node, str)
        or not isinstance(vmid, int)
    ):
        return None
    return target, node, vmid


def _recovered_provider_evidence(context: HostContext) -> ProviderEvidence:
    identity = _plan_identity(context.plan.provider.plan)
    if identity is None:
        raise AdapterError("Proxmox child plan is missing its resource identity")
    target, node, vmid = identity
    child_plan = context.plan.provider.plan
    return ProviderEvidence(
        provider="proxmox",
        resourceType="proxmox.qemu",
        resourceId=f"qemu/{vmid}",
        resourceName=target,
        locator={"node": node, "vmid": vmid},
        ownershipMarker=True,
        details={
            "childEvidence": child_plan,
            "childPlan": child_plan,
            "recoveredFromLiveState": True,
        },
    )


def _check_passed(payload: dict[str, Any], name: str) -> bool:
    checks = payload["checks"]
    return any(
        isinstance(check, dict)
        and check.get("name") == name
        and check.get("status") == "passed"
        for check in checks
    )


def _json_object(result: ChildResult, command: str) -> dict[str, Any]:
    payload = _json_object_or_none(result)
    if payload is None:
        raise AdapterError(f"{command} returned invalid JSON")
    return payload


def _json_object_or_none(result: ChildResult) -> dict[str, Any] | None:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _diagnostic(result: ChildResult) -> str:
    return result.stderr.strip() or f"exit status {result.return_code}"
