"""Provider-neutral host provider contract and deterministic fake."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from atlas_host_operations.errors import AdapterError, UnknownProviderResult
from atlas_host_operations.models import (
    CheckResult,
    HostOperationPlan,
    ProviderEvidence,
    ProviderObservation,
    RegistryAuthority,
    RollbackResult,
    VerificationResult,
)


@dataclass(frozen=True)
class HostContext:
    plan: HostOperationPlan


class HostProvider(Protocol):
    name: str
    resource_type: str

    def validate(self, context: HostContext) -> list[CheckResult]: ...

    def planning_artifact(self) -> dict[str, object]: ...

    def allocate(
        self,
        context: HostContext,
        authority: RegistryAuthority,
    ) -> ProviderEvidence: ...

    def observe(self, context: HostContext) -> ProviderObservation: ...

    def verify(
        self,
        context: HostContext,
        evidence: ProviderEvidence,
    ) -> VerificationResult: ...

    def rollback(
        self,
        context: HostContext,
        evidence: ProviderEvidence,
        authority: RegistryAuthority,
    ) -> RollbackResult: ...


@dataclass
class FakeCloudProvider:
    """Provider-neutral stateful fake used by lifecycle contract tests."""

    name: str = "fake-cloud"
    resource_type: str = "compute"
    fail_on: str | None = None
    unknown_allocate: bool = False
    calls: list[str] = field(default_factory=list)
    exists: bool = False
    running: bool = False
    ownership_marker: bool = False
    _plan: dict[str, object] = field(default_factory=dict)

    def validate(self, context: HostContext) -> list[CheckResult]:
        self.calls.append("validate")
        self._fail("validate")
        self._plan = {
            "kind": "FakeCloudHostPlan",
            "target": context.plan.resource.name,
        }
        return [CheckResult(name="provider.fake", status="passed")]

    def planning_artifact(self) -> dict[str, object]:
        return dict(self._plan)

    def allocate(
        self,
        context: HostContext,
        authority: RegistryAuthority,
    ) -> ProviderEvidence:
        self.calls.append("allocate")
        if self.unknown_allocate:
            raise UnknownProviderResult("fake provider allocation result is unknown")
        self._fail("allocate")
        self.exists = True
        self.running = True
        self.ownership_marker = True
        return self._evidence(context, fencing_token=authority.fencing_token)

    def _evidence(
        self,
        context: HostContext,
        *,
        fencing_token: int | None = None,
    ) -> ProviderEvidence:
        details = {} if fencing_token is None else {"fencingToken": fencing_token}
        return ProviderEvidence(
            provider=self.name,
            resourceType="compute",
            resourceId=f"fake/{context.plan.resource.id}",
            resourceName=context.plan.resource.name,
            locator={"region": "test"},
            ownershipMarker=self.ownership_marker,
            details=details,
        )

    def observe(self, context: HostContext) -> ProviderObservation:
        self.calls.append("observe")
        self._fail("observe")
        return ProviderObservation(
            exists=self.exists,
            running=self.running,
            guestAgentReady=self.running,
            addresses=[context.plan.readiness.address] if self.running else [],
            absenceConfirmed=not self.exists,
            providerEvidence=(
                self._evidence(context)
                if self.exists and self.ownership_marker
                else None
            ),
        )

    def verify(
        self,
        context: HostContext,
        evidence: ProviderEvidence,
    ) -> VerificationResult:
        self.calls.append("verify")
        self._fail("verify")
        passed = (
            self.exists
            and self.running
            and self.ownership_marker
            and evidence.resource_name == context.plan.resource.name
        )
        return VerificationResult(
            status="passed" if passed else "failed",
            checks=[
                CheckResult(
                    name="provider.fake.live-state",
                    status="passed" if passed else "failed",
                )
            ],
        )

    def rollback(
        self,
        context: HostContext,
        evidence: ProviderEvidence,
        authority: RegistryAuthority,
    ) -> RollbackResult:
        self.calls.append("rollback")
        self._fail("rollback")
        if not evidence.ownership_marker or not self.ownership_marker:
            return RollbackResult(status="failed", message="ownership marker mismatch")
        self.exists = False
        self.running = False
        return RollbackResult(
            status="succeeded",
            details={"fencingToken": authority.fencing_token},
        )

    def _fail(self, method: str) -> None:
        if self.fail_on == method:
            raise AdapterError(f"fake provider {method} failed")
