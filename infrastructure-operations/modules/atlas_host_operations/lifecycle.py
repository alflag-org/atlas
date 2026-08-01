"""Provider-neutral host lifecycle states and transition validation."""

from __future__ import annotations

from enum import Enum, StrEnum

from atlas_host_operations.errors import PlanError


class ResourceLifecycle(StrEnum):
    ABSENT = "absent"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    RETIRING = "retiring"
    RETIRED = "retired"


class OperationStatus(StrEnum):
    PLANNED = "planned"
    LOCKED = "locked"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_RECONCILE = "needs-reconcile"
    ROLLING_BACK = "rolling-back"
    ROLLED_BACK = "rolled-back"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_RECONCILE = "needs-reconcile"
    ROLLED_BACK = "rolled-back"


class ProvisioningPhase(StrEnum):
    VALIDATE = "validate"
    RESERVE = "reserve"
    ALLOCATE = "allocate"
    PROVIDER_VERIFY = "provider-verify"
    BIND = "bind"
    WAIT_READY = "wait-ready"
    BOOTSTRAP = "bootstrap"
    CONVERGE = "converge"
    CONFIGURATION_VERIFY = "configuration-verify"
    ACTIVATE = "activate"


PROVISIONING_PHASES: tuple[ProvisioningPhase, ...] = tuple(ProvisioningPhase)

_RESOURCE_TRANSITIONS: dict[ResourceLifecycle, frozenset[ResourceLifecycle]] = {
    ResourceLifecycle.ABSENT: frozenset({ResourceLifecycle.PROVISIONING}),
    ResourceLifecycle.PROVISIONING: frozenset({ResourceLifecycle.ACTIVE}),
    ResourceLifecycle.ACTIVE: frozenset(
        {ResourceLifecycle.MAINTENANCE, ResourceLifecycle.RETIRING}
    ),
    ResourceLifecycle.MAINTENANCE: frozenset(
        {ResourceLifecycle.ACTIVE, ResourceLifecycle.RETIRING}
    ),
    ResourceLifecycle.RETIRING: frozenset({ResourceLifecycle.RETIRED}),
    ResourceLifecycle.RETIRED: frozenset(),
}

_OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PLANNED: frozenset(
        {OperationStatus.LOCKED, OperationStatus.CANCELLED}
    ),
    OperationStatus.LOCKED: frozenset(
        {OperationStatus.RUNNING, OperationStatus.CANCELLED}
    ),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.VERIFYING,
            OperationStatus.FAILED,
            OperationStatus.NEEDS_RECONCILE,
            OperationStatus.ROLLING_BACK,
        }
    ),
    OperationStatus.VERIFYING: frozenset(
        {
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.NEEDS_RECONCILE,
            OperationStatus.ROLLING_BACK,
        }
    ),
    OperationStatus.NEEDS_RECONCILE: frozenset(
        {
            OperationStatus.RUNNING,
            OperationStatus.FAILED,
            OperationStatus.ROLLING_BACK,
        }
    ),
    OperationStatus.ROLLING_BACK: frozenset(
        {
            OperationStatus.ROLLED_BACK,
            OperationStatus.FAILED,
            OperationStatus.NEEDS_RECONCILE,
        }
    ),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.ROLLED_BACK: frozenset(),
    OperationStatus.CANCELLED: frozenset(),
}

_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.RUNNING, StepStatus.SKIPPED}),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.NEEDS_RECONCILE,
        }
    ),
    StepStatus.FAILED: frozenset({StepStatus.RUNNING}),
    StepStatus.NEEDS_RECONCILE: frozenset({StepStatus.RUNNING, StepStatus.FAILED}),
    StepStatus.SUCCEEDED: frozenset({StepStatus.ROLLED_BACK}),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.ROLLED_BACK: frozenset(),
}


def validate_resource_transition(
    current: ResourceLifecycle,
    target: ResourceLifecycle,
) -> None:
    _validate_transition("resource", current, target, _RESOURCE_TRANSITIONS)


def validate_operation_transition(
    current: OperationStatus,
    target: OperationStatus,
) -> None:
    _validate_transition("operation", current, target, _OPERATION_TRANSITIONS)


def validate_step_transition(current: StepStatus, target: StepStatus) -> None:
    _validate_transition("step", current, target, _STEP_TRANSITIONS)


def phase_position(phase: ProvisioningPhase) -> int:
    return PROVISIONING_PHASES.index(phase)


def _validate_transition(
    label: str,
    current: Enum,
    target: Enum,
    transitions: dict[Enum, frozenset[Enum]],
) -> None:
    if target not in transitions[current]:
        raise PlanError(
            f"invalid {label} transition: {current.value} -> {target.value}"
        )
