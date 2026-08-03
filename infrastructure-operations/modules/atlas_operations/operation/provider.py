from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from atlas_operations.operation.plan import CheckResult, OperationPlan, OperationStep


@dataclass
class StepResult:
    id: str
    status: str
    task_id: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyResult:
    status: str
    checks: list[CheckResult] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


class ProviderClient(Protocol):
    name: str

    def preflight(self, plan: OperationPlan) -> VerifyResult:
        ...

    def apply_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        ...

    def verify(self, plan: OperationPlan) -> VerifyResult:
        ...

    def rollback_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        ...
