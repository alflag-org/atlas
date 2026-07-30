from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from atlas_operations.operation.plan import CheckResult, OperationPlan, OperationStep


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    live_operations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderQuery:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderState:
    provider: str
    data: dict[str, Any] = field(default_factory=dict)


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

    def capabilities(self) -> ProviderCapabilities:
        ...

    def read_state(self, query: ProviderQuery) -> ProviderState:
        ...

    def preflight(self, plan: OperationPlan) -> VerifyResult:
        ...

    def apply_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        ...

    def verify(self, plan: OperationPlan) -> VerifyResult:
        ...

    def rollback_step(self, step: OperationStep, plan: OperationPlan) -> StepResult:
        ...
