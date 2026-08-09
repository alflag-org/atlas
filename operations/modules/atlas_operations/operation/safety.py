from __future__ import annotations

from datetime import UTC, datetime

from atlas_operations.operation.config import SafetyConfig
from atlas_operations.operation.errors import SafetyError
from atlas_operations.operation.evidence import OperationEvidence
from atlas_operations.operation.fingerprint import validate_fingerprint
from atlas_operations.operation.plan import OperationPlan

IMPLEMENTED_OPERATION_KINDS = frozenset(
    {
        "proxmox.vm-create",
        "proxmox.vm-template-create",
    }
)


class SafetyGate:
    def __init__(
        self,
        config: SafetyConfig,
        *,
        now: datetime | None = None,
    ) -> None:
        self.config = config
        self.now = now

    def validate_apply(
        self,
        plan: OperationPlan,
        *,
        confirm: str | None,
        preflight_passed: bool,
    ) -> None:
        self._validate_common(plan)
        self._validate_plan_age(plan)
        if self.config.require_confirm and confirm != plan.metadata.plan_id:
            raise SafetyError("apply refused: --confirm must equal plan id")
        if plan.provider.mode != "live":
            raise SafetyError("apply refused: provider mode must be live")
        if plan.safety.allowed_only_if_preflight_passes and not preflight_passed:
            raise SafetyError("apply refused: preflight did not pass")

    def validate_rollback(
        self,
        plan: OperationPlan,
        evidence: OperationEvidence,
        *,
        confirm: str | None,
    ) -> None:
        self._validate_common(plan)
        if self.config.require_confirm and confirm != plan.metadata.plan_id:
            raise SafetyError("rollback refused: --confirm must equal plan id")
        if not self.config.allow_rollback_delete:
            raise SafetyError("rollback refused: rollback delete is disabled by config")
        policy = plan.spec.get("policy")
        if not isinstance(policy, dict) or not policy.get("rollbackDeleteAllowed"):
            raise SafetyError("rollback refused: operation input does not allow delete")
        if not evidence.plan.snapshot:
            raise SafetyError("rollback refused: evidence does not include a plan snapshot")
        if evidence.plan.fingerprint != plan.metadata.fingerprint:
            raise SafetyError("rollback refused: evidence fingerprint does not match plan")
        if not plan.rollback.supported:
            raise SafetyError("rollback refused: plan does not support rollback")
        if evidence.metadata.plan_id != plan.metadata.plan_id:
            raise SafetyError("rollback refused: evidence belongs to a different plan")
        if evidence.metadata.operation_kind != plan.metadata.operation_kind:
            raise SafetyError("rollback refused: evidence operation kind does not match plan")
        if evidence.metadata.target != plan.metadata.target:
            raise SafetyError("rollback refused: evidence target does not match plan")
        if evidence.provider.name != plan.provider.name:
            raise SafetyError("rollback refused: evidence provider does not match plan")
        if evidence.provider.node != plan.provider.node:
            raise SafetyError("rollback refused: evidence provider node does not match plan")
        if evidence.provider.vmid != int(plan.spec["vmid"]):
            raise SafetyError("rollback refused: evidence VMID does not match plan")
        if not evidence.rollback.supported:
            raise SafetyError("rollback refused: evidence does not allow rollback")
        if not _has_created_resource(evidence, int(plan.spec["vmid"])):
            raise SafetyError("rollback refused: evidence does not show a created VM")

    def _validate_common(self, plan: OperationPlan) -> None:
        if plan.metadata.operation_kind not in IMPLEMENTED_OPERATION_KINDS:
            raise SafetyError(
                f"operation is not implemented: {plan.metadata.operation_kind}"
            )
        validate_fingerprint(plan)

    def _validate_plan_age(self, plan: OperationPlan) -> None:
        max_age = min(self.config.max_plan_age_seconds, plan.safety.max_plan_age_seconds)
        age = (self._now() - plan.metadata.created_at).total_seconds()
        if age < 0:
            raise SafetyError("plan was created in the future")
        if age > max_age:
            raise SafetyError(f"plan is older than max_plan_age_seconds ({max_age})")

    def _now(self) -> datetime:
        if self.now:
            return self.now
        return datetime.now(UTC)


def _has_created_resource(evidence: OperationEvidence, vmid: int) -> bool:
    return any(
        resource.provider == evidence.provider.name
        and resource.type in {"proxmox.qemu", "proxmox.qemu-template"}
        and resource.vmid == vmid
        for resource in evidence.created_resources
    )
