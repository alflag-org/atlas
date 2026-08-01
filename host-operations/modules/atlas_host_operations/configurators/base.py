"""Host configuration contract and deterministic fake."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from atlas_host_operations.errors import AdapterError
from atlas_host_operations.models import CheckResult, StepResult, VerificationResult
from atlas_host_operations.providers.base import HostContext


class HostConfigurator(Protocol):
    name: str

    def validate(self, context: HostContext) -> list[CheckResult]: ...

    def bootstrap(self, context: HostContext) -> StepResult: ...

    def converge(self, context: HostContext) -> StepResult: ...

    def verify(self, context: HostContext) -> VerificationResult: ...


@dataclass
class FakeHostConfigurator:
    name: str = "fake-configurator"
    fail_on: str | None = None
    calls: list[str] = field(default_factory=list)
    bootstrapped: bool = False
    converged: bool = False

    def validate(self, context: HostContext) -> list[CheckResult]:
        self.calls.append("validate")
        self._fail("validate")
        return [CheckResult(name="configuration.fake", status="passed")]

    def bootstrap(self, context: HostContext) -> StepResult:
        self.calls.append("bootstrap")
        self._fail("bootstrap")
        self.bootstrapped = True
        return StepResult(status="succeeded")

    def converge(self, context: HostContext) -> StepResult:
        self.calls.append("converge")
        self._fail("converge")
        if not self.bootstrapped:
            return StepResult(status="failed", message="host is not bootstrapped")
        self.converged = True
        return StepResult(status="succeeded")

    def verify(self, context: HostContext) -> VerificationResult:
        self.calls.append("verify")
        self._fail("verify")
        bootstrap_check = (
            context.plan.configuration.converge_playbook
            == context.plan.configuration.bootstrap_playbook
        )
        configured = self.bootstrapped if bootstrap_check else self.converged
        return VerificationResult(
            status="passed" if configured else "failed",
            checks=[
                CheckResult(
                    name="configuration.fake.converged",
                    status="passed" if configured else "failed",
                )
            ],
        )

    def _fail(self, method: str) -> None:
        if self.fail_on == method:
            raise AdapterError(f"fake configurator {method} failed")
