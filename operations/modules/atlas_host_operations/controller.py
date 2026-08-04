"""Planning and orchestration for the managed host lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import uuid4

from pydantic import ValidationError

from atlas_host_operations.artifacts import (
    file_digest,
    git_state,
    load_host_spec,
    read_plan,
    read_yaml,
    safe_directory,
    safe_file,
    set_fingerprint,
    validate_fingerprint,
    validate_source_bindings,
    write_json,
)
from atlas_host_operations.configurators import (
    AnsibleHostConfigurator,
    HostConfigurator,
)
from atlas_host_operations.errors import (
    AdapterError,
    HostOperationError,
    InputError,
    PlanError,
    RegistryAuthenticationError,
    RegistryConflictError,
    RegistryError,
    RegistryUnavailableError,
    SafetyError,
    UnknownProviderResult,
)
from atlas_host_operations.lifecycle import (
    PROVISIONING_PHASES,
    OperationStatus,
    ProvisioningPhase,
    ResourceLifecycle,
    StepStatus,
)
from atlas_host_operations.models import (
    GitSourceReference,
    HostOperationEvidence,
    HostOperationPlan,
    HostPlanConfiguration,
    HostPlanMetadata,
    HostPlanProvider,
    HostPlanResource,
    HostPlanSources,
    HostStatus,
    ProviderEvidence,
    RegistryAuthority,
    RegistryOperation,
    RegistryStep,
    SourceReference,
    VerificationResult,
)
from atlas_host_operations.providers import (
    HostContext,
    HostProvider,
    ProxmoxHostProvider,
)
from atlas_host_operations.readiness import HostReadinessChecker, ReadinessChecker
from atlas_host_operations.registry import (
    HTTPRegistryClient,
    RegistryClient,
    load_registry_client,
    provider_binding_matches,
)
from atlas_host_operations.subprocesses import CommandRunner, SubprocessRunner

MAX_PLAN_AGE = timedelta(minutes=30)
FUTURE_TOLERANCE = timedelta(minutes=1)
EXECUTION_PHASES = PROVISIONING_PHASES[1:]
LOCK_RENEW_INTERVAL_SECONDS = 120.0
_T = TypeVar("_T")


@dataclass(frozen=True)
class PhaseExecution:
    evidence: HostOperationEvidence
    exit_code: int


class PhaseExecutor(Protocol):
    def execute(
        self,
        phase: ProvisioningPhase,
        plan: HostOperationPlan,
        operation_id: str,
        *,
        resume: bool,
    ) -> PhaseExecution: ...


class _RegistryAuthorityLease:
    """Keep one Registry authority current while a blocking child action runs."""

    def __init__(
        self,
        registry: RegistryClient,
        operation_id: str,
        authority: RegistryAuthority,
        *,
        renew_interval_seconds: float = LOCK_RENEW_INTERVAL_SECONDS,
    ) -> None:
        self._registry = registry
        self._operation_id = operation_id
        self._authority = authority
        self._renew_interval_seconds = renew_interval_seconds
        self._state_lock = threading.Lock()

    @property
    def authority(self) -> RegistryAuthority:
        with self._state_lock:
            return self._authority

    def run(self, action: Callable[[], _T]) -> _T:
        stop = threading.Event()
        renewal_errors: list[RegistryError] = []

        def renew() -> None:
            while not stop.wait(self._renew_interval_seconds):
                try:
                    authority = self._registry.renew_locks(self._operation_id)
                except RegistryError as exc:
                    renewal_errors.append(exc)
                    return
                with self._state_lock:
                    self._authority = authority

        thread = threading.Thread(
            target=renew,
            name=f"hostctl-lock-{self._operation_id}",
            daemon=True,
        )
        thread.start()
        try:
            result = action()
        finally:
            stop.set()
            thread.join()
        if renewal_errors:
            raise renewal_errors[0]
        return result


class InlinePhaseExecutor:
    """Run phase functions in one process for provider-neutral contract tests."""

    def __init__(
        self,
        registry: RegistryClient,
        provider: HostProvider,
        configurator: HostConfigurator,
        readiness: ReadinessChecker,
    ) -> None:
        self._registry = registry
        self._provider = provider
        self._configurator = configurator
        self._readiness = readiness

    def execute(
        self,
        phase: ProvisioningPhase,
        plan: HostOperationPlan,
        operation_id: str,
        *,
        resume: bool,
    ) -> PhaseExecution:
        return execute_phase(
            phase,
            plan,
            operation_id,
            registry=self._registry,
            provider=self._provider,
            configurator=self._configurator,
            readiness=self._readiness,
            resume=resume,
        )


class SubprocessPhaseExecutor:
    """Invoke each phase through Atlas's private job execution contract."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        atlas_executable: str = "/opt/atlas/bin/atlas",
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._atlas_executable = atlas_executable

    def execute(
        self,
        phase: ProvisioningPhase,
        plan: HostOperationPlan,
        operation_id: str,
        *,
        resume: bool,
    ) -> PhaseExecution:
        job = _PHASE_JOBS[phase]
        argv = [
            self._atlas_executable,
            "job",
            "run",
            "operations",
            job,
            "--",
            "--plan",
            "-",
            "--operation",
            operation_id,
        ]
        if resume:
            argv.append("--resume")
        correlation_id = os.environ.get("ATLAS_OPERATION_ID") or operation_id
        result = self._runner.run(
            argv,
            input_text=json.dumps(plan.as_artifact()),
            env={"ATLAS_OPERATION_ID": correlation_id},
        )
        try:
            payload = json.loads(result.stdout)
            evidence = HostOperationEvidence.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            if result.return_code != 0:
                raise _phase_job_error(job, result.return_code, result.stderr) from exc
            raise AdapterError(f"phase job returned invalid evidence: {job}") from exc
        if (
            evidence.operation_id != operation_id
            or evidence.plan_id != plan.metadata.plan_id
            or evidence.resource_id != plan.resource.id
            or evidence.phase is not phase
        ):
            raise AdapterError(f"phase job returned mismatched evidence: {job}")
        successful = evidence.status in {StepStatus.SUCCEEDED, StepStatus.SKIPPED}
        if (result.return_code == 0) != successful:
            raise AdapterError(f"phase job returned an inconsistent outcome: {job}")
        return PhaseExecution(evidence=evidence, exit_code=result.return_code)


class HostController:
    def __init__(
        self,
        registry: RegistryClient,
        phase_executor: PhaseExecutor,
        *,
        provider: HostProvider | None = None,
        configurator: HostConfigurator | None = None,
        readiness: ReadinessChecker | None = None,
    ) -> None:
        self.registry = registry
        self.phase_executor = phase_executor
        self.provider = provider
        self.configurator = configurator
        self.readiness = readiness

    def apply(self, plan: HostOperationPlan, *, confirm: str) -> PhaseExecution:
        self._validate_mutation(plan, confirm=confirm, check_age=True)
        operation = self.registry.find_operation_by_idempotency_key(
            plan.metadata.idempotency_key
        )
        if operation is None:
            operation = self.registry.create_operation(plan)
        else:
            _require_operation_plan(operation, plan)
        if _operation_complete(operation):
            return _terminal_evidence(plan, operation)
        if operation.status in {
            OperationStatus.FAILED.value,
            OperationStatus.CANCELLED.value,
            OperationStatus.ROLLED_BACK.value,
            "failed",
            "cancelled",
        }:
            raise SafetyError(f"operation is terminal: {operation.status}")
        if _operation_needs_reconcile(operation):
            raise SafetyError("operation needs reconciliation; use hostctl resume")
        return self._run(plan, operation, resume=False)

    def resume(self, plan: HostOperationPlan, *, confirm: str) -> PhaseExecution:
        self._validate_mutation(plan, confirm=confirm, check_age=False)
        operation = self.registry.find_operation_by_idempotency_key(
            plan.metadata.idempotency_key
        )
        if operation is None:
            raise PlanError("operation does not exist; use hostctl apply")
        _require_operation_plan(operation, plan)
        if _operation_complete(operation):
            return _terminal_evidence(plan, operation)
        if operation.status in {
            OperationStatus.FAILED.value,
            OperationStatus.CANCELLED.value,
            OperationStatus.ROLLED_BACK.value,
        }:
            raise SafetyError(f"operation is terminal: {operation.status}")
        return self._run(plan, operation, resume=True)

    def status(self, target: str) -> HostStatus:
        plan, operation = self._resolve_plan_and_operation(
            target, require_operation=False
        )
        if operation is None:
            return HostStatus(
                operationId=None,
                planId=plan.metadata.plan_id,
                resourceId=plan.resource.id,
                operationStatus=OperationStatus.PLANNED.value,
                resourceLifecycle=plan.resource.lifecycle_before.value,
                currentPhase=ProvisioningPhase.VALIDATE.value,
                steps=[],
            )
        resource = self.registry.get_resource(plan.resource.id)
        lifecycle = (
            _host_lifecycle(resource.lifecycle_state)
            if resource is not None
            else ResourceLifecycle.ABSENT.value
        )
        current = next(
            (
                step.name
                for step in operation.steps
                if step.status not in {"succeeded", "skipped"}
            ),
            None,
        )
        return HostStatus(
            operationId=operation.id,
            planId=plan.metadata.plan_id,
            resourceId=plan.resource.id,
            operationStatus=_host_operation_status(operation),
            resourceLifecycle=lifecycle,
            currentPhase=current,
            steps=[
                step.model_dump(mode="json", by_alias=True) for step in operation.steps
            ],
        )

    def verify(self, target: str) -> VerificationResult:
        plan, operation = self._resolve_plan_and_operation(
            target, require_operation=True
        )
        validate_source_bindings(plan)
        provider = self.provider or provider_for_plan(plan)
        configurator = self.configurator or configurator_for_plan(plan)
        readiness = self.readiness or HostReadinessChecker()
        context = HostContext(plan)
        evidence = _provider_evidence(operation)
        provider_result = provider.verify(context, evidence)
        observation = provider.observe(context)
        readiness_result = readiness.wait(context, observation)
        configuration_result = configurator.verify(context)
        checks = [
            *provider_result.checks,
            *readiness_result.checks,
            *configuration_result.checks,
        ]
        status = (
            "failed" if any(check.status == "failed" for check in checks) else "passed"
        )
        return VerificationResult(status=status, checks=checks)

    def rollback(self, target: str, *, confirm: str) -> PhaseExecution:
        plan, operation = self._resolve_plan_and_operation(
            target, require_operation=True
        )
        self._validate_mutation(plan, confirm=confirm, check_age=False)
        resource = self.registry.get_resource(plan.resource.id)
        if (
            _operation_complete(operation)
            or (
                resource
                and _host_lifecycle(resource.lifecycle_state)
                == ResourceLifecycle.ACTIVE.value
            )
        ):
            raise SafetyError("active hosts require a separate HostRetire operation")
        steps = {step.name: step for step in operation.steps}
        authority = self.registry.acquire_locks(
            operation.id,
            [f"resource/{plan.resource.id}"],
        )
        now = datetime.now(UTC)
        configuration_started = next(
            (
                steps[phase.value]
                for phase in (
                    ProvisioningPhase.BOOTSTRAP,
                    ProvisioningPhase.CONVERGE,
                    ProvisioningPhase.CONFIGURATION_VERIFY,
                )
                if phase.value in steps
                and steps[phase.value].status not in {"planned", "pending", "skipped"}
            ),
            None,
        )
        if configuration_started is not None:
            evidence = _evidence(
                plan,
                operation.id,
                ProvisioningPhase(configuration_started.name),
                StepStatus.NEEDS_RECONCILE,
                now,
                message="configuration has started; provider resource was retained",
            )
            _mark_needs_reconcile(
                plan,
                operation.id,
                evidence,
                self.registry,
                authority,
            )
            return PhaseExecution(evidence, 6)
        allocation = steps.get(ProvisioningPhase.ALLOCATE.value)
        if allocation is None or allocation.status in {"planned", "pending", "skipped"}:
            updated = self.registry.cancel_operation(operation.id, authority)
            self.registry.release_locks(operation.id)
            evidence = _evidence(
                plan,
                updated.id,
                ProvisioningPhase.RESERVE,
                StepStatus.ROLLED_BACK,
                now,
                message="operation cancelled before provider allocation",
            )
            return PhaseExecution(evidence, 0)
        if allocation.status != StepStatus.SUCCEEDED.value:
            evidence = _evidence(
                plan,
                operation.id,
                ProvisioningPhase.ALLOCATE,
                StepStatus.NEEDS_RECONCILE,
                now,
                attempt=_next_attempt(allocation.evidence),
                message=(
                    "provider allocation was attempted without a confirmed result; "
                    "provider resource was retained"
                ),
            )
            _mark_needs_reconcile(
                plan,
                operation.id,
                evidence,
                self.registry,
                authority,
            )
            return PhaseExecution(evidence, 6)
        provider = self.provider or provider_for_plan(plan)
        provider_evidence = _provider_evidence(operation)
        if resource is not None and resource.binding is not None and not (
            provider_binding_matches(resource.binding, provider_evidence)
        ):
            evidence = _evidence(
                plan,
                operation.id,
                ProvisioningPhase.BIND,
                StepStatus.NEEDS_RECONCILE,
                now,
                message="Registry contains a different provider Binding; provider was retained",
            )
            _mark_needs_reconcile(
                plan,
                operation.id,
                evidence,
                self.registry,
                authority,
            )
            return PhaseExecution(evidence, 6)
        lease = _RegistryAuthorityLease(self.registry, operation.id, authority)
        try:
            result = lease.run(
                lambda: provider.rollback(
                    HostContext(plan),
                    provider_evidence,
                    lease.authority,
                )
            )
        except (UnknownProviderResult, RegistryError, AdapterError) as exc:
            return _phase_failure(
                exc,
                plan,
                operation.id,
                ProvisioningPhase.ALLOCATE,
                now,
                _next_attempt(allocation.evidence if allocation else {}),
                self.registry,
                lease.authority,
                previous_evidence=allocation.evidence if allocation else None,
            )
        if result.status == "succeeded" and resource and resource.binding is not None:
            self.registry.remove_provider_binding(
                operation.id,
                plan.resource.id,
                lease.authority,
            )
        status = (
            StepStatus.ROLLED_BACK
            if result.status == "succeeded"
            else StepStatus.FAILED
        )
        evidence = _evidence(
            plan,
            operation.id,
            ProvisioningPhase.ALLOCATE,
            status,
            now,
            message=result.message,
            details=result.details,
        )
        if result.status == "succeeded":
            self.registry.record_step(operation.id, evidence, lease.authority)
            self.registry.cancel_operation(operation.id, lease.authority)
            self.registry.release_locks(operation.id)
            return PhaseExecution(evidence, 0)
        _mark_needs_reconcile(
            plan,
            operation.id,
            evidence,
            self.registry,
            lease.authority,
        )
        return PhaseExecution(evidence, 1)

    def _run(
        self,
        plan: HostOperationPlan,
        operation: RegistryOperation,
        *,
        resume: bool,
    ) -> PhaseExecution:
        self._record_plan_validation(plan, operation)
        last: PhaseExecution | None = None
        for phase in EXECUTION_PHASES:
            print(f"phase: {phase.value}", file=sys.stderr)
            last = self.phase_executor.execute(
                phase,
                plan,
                operation.id,
                resume=resume,
            )
            if last.exit_code != 0:
                return last
        if last is None:
            raise PlanError("host operation has no executable phases")
        return last

    def _record_plan_validation(
        self,
        plan: HostOperationPlan,
        operation: RegistryOperation,
    ) -> None:
        validate_step = next(
            (
                step
                for step in operation.steps
                if step.name == ProvisioningPhase.VALIDATE.value
            ),
            None,
        )
        if validate_step is not None and validate_step.status == "succeeded":
            return
        authority = self.registry.acquire_locks(
            operation.id,
            [f"resource/{plan.resource.id}"],
        )
        self.registry.start_operation(operation.id, authority)
        now = datetime.now(UTC)
        if validate_step is None or validate_step.status != "running":
            running = _evidence(
                plan,
                operation.id,
                ProvisioningPhase.VALIDATE,
                StepStatus.RUNNING,
                now,
                attempt=_next_attempt(validate_step.evidence if validate_step else {}),
            )
            self.registry.record_step(operation.id, running, authority)
        evidence = _evidence(
            plan,
            operation.id,
            ProvisioningPhase.VALIDATE,
            StepStatus.SUCCEEDED,
            now,
            attempt=_next_attempt(validate_step.evidence if validate_step else {}),
            message="plan and source bindings validated",
        )
        self.registry.record_step(operation.id, evidence, authority)

    @staticmethod
    def _validate_mutation(
        plan: HostOperationPlan,
        *,
        confirm: str,
        check_age: bool,
    ) -> None:
        validate_fingerprint(plan)
        validate_source_bindings(plan)
        if confirm != plan.metadata.plan_id:
            raise SafetyError("--confirm must exactly match the plan ID")
        if check_age:
            now = datetime.now(UTC)
            if plan.metadata.created_at > now + FUTURE_TOLERANCE:
                raise PlanError("plan creation time is in the future")
            if now - plan.metadata.created_at > MAX_PLAN_AGE:
                raise PlanError("plan is stale; create a new plan")

    def _resolve_plan_and_operation(
        self,
        target: str,
        *,
        require_operation: bool,
    ) -> tuple[HostOperationPlan, RegistryOperation | None]:
        candidate = Path(target)
        if candidate.is_file() and not candidate.is_symlink():
            plan = read_plan(target)
            operation = self.registry.find_operation_by_idempotency_key(
                plan.metadata.idempotency_key
            )
        else:
            operation = self.registry.get_operation(target)
            if operation is None:
                operation = self.registry.find_operation_for_resource(target)
            if operation is None:
                raise PlanError(f"operation or Resource not found: {target}")
            plan = _operation_host_plan(operation)
        if require_operation and operation is None:
            raise PlanError("no Global Registry operation exists for this plan")
        return plan, operation


def build_host_plan(
    host_spec_path: str | Path,
    *,
    registry: RegistryClient | None = None,
    provider: HostProvider | None = None,
    configurator: HostConfigurator | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    new_id: Callable[[], str] = lambda: str(uuid4()),
) -> HostOperationPlan:
    spec, spec_path = load_host_spec(host_spec_path)
    base = spec_path.parent
    registry_profile = safe_file(spec.registry.profile, base=base)
    provider_definition = safe_file(spec.provider.definition, base=base)
    provider_input = safe_file(spec.provider.input, base=base)
    project = safe_directory(spec.configuration.project_root, base=base)
    commit, dirty = git_state(project)
    _validate_inventory_target(project, spec.resource.site, spec.configuration.target)
    selected_registry = registry or HTTPRegistryClient.from_profile(registry_profile)
    resource = selected_registry.get_resource(spec.resource.id)
    if resource is None:
        raise InputError(
            "Resource identity must be reserved in Global Registry before planning"
        )
    if resource.key != spec.resource.id or resource.name != spec.resource.name:
        raise InputError("Resource identity collides with the current Registry record")
    if resource.kind != "compute":
        raise InputError("managed host Resource kind must be compute")
    if resource.lifecycle_state != "absent" or resource.binding is not None:
        raise InputError("Resource is not an unbound absent compute identity")
    spec_digest = file_digest(spec_path)
    registry_profile_digest = file_digest(registry_profile)
    provider_definition_digest = file_digest(provider_definition)
    provider_input_digest = file_digest(provider_input)
    idempotency_key = _host_create_idempotency_key(
        resource_name=spec.resource.name,
        source_digests={
            "hostSpec": spec_digest,
            "registryProfile": registry_profile_digest,
            "providerDefinition": provider_definition_digest,
            "providerInput": provider_input_digest,
        },
        provisioning_commit=commit,
        provisioning_dirty=dirty,
        registry_revision=resource.revision,
    )
    selected_provider = provider or _provider_for_adapter(spec.provider.adapter)
    initial = HostOperationPlan(
        apiVersion="atlas.host-operation/v1",
        kind="HostOperationPlan",
        metadata=HostPlanMetadata(
            planId=f"plan-{new_id()}",
            operationKind="HostCreate",
            createdAt=now(),
            target=spec.resource.name,
            site=spec.resource.site,
            risk="high",
            idempotencyKey=idempotency_key,
        ),
        resource=HostPlanResource(
            id=spec.resource.id,
            name=spec.resource.name,
            lifecycleBefore=ResourceLifecycle.ABSENT,
            lifecycleAfter=ResourceLifecycle.ACTIVE,
            registryRevision=resource.revision,
        ),
        sources=HostPlanSources(
            hostSpec=SourceReference(path=str(spec_path), digest=spec_digest),
            registryProfile=SourceReference(
                path=str(registry_profile),
                digest=registry_profile_digest,
            ),
            providerDefinition=SourceReference(
                path=str(provider_definition),
                digest=provider_definition_digest,
            ),
            providerInput=SourceReference(
                path=str(provider_input),
                digest=provider_input_digest,
            ),
            provisioningProject=GitSourceReference(
                path=str(project),
                gitCommit=commit,
                gitDirty=dirty,
            ),
        ),
        provider=HostPlanProvider(
            adapter=spec.provider.adapter,
            resourceType=selected_provider.resource_type,
        ),
        configuration=HostPlanConfiguration(
            adapter=spec.configuration.adapter,
            target=spec.configuration.target,
            bootstrapPlaybook=spec.configuration.bootstrap_playbook,
            convergePlaybook=spec.configuration.converge_playbook,
        ),
        readiness=spec.readiness,
        phases=list(PROVISIONING_PHASES),
    )
    selected_configurator = configurator or configurator_for_plan(initial)
    if selected_provider.name != spec.provider.adapter:
        raise InputError("provider adapter name does not match the host specification")
    if selected_configurator.name != spec.configuration.adapter:
        raise InputError(
            "configuration adapter name does not match the host specification"
        )
    context = HostContext(initial)
    checks = [
        *selected_provider.validate(context),
        *selected_configurator.validate(context),
    ]
    failures = [check for check in checks if check.status == "failed"]
    if failures:
        messages = "; ".join(check.message or check.name for check in failures)
        raise AdapterError(f"host planning checks failed: {messages}")
    data = initial.as_artifact()
    data["provider"]["plan"] = selected_provider.planning_artifact()
    return set_fingerprint(HostOperationPlan.model_validate(data))


def execute_phase(
    phase: ProvisioningPhase,
    plan: HostOperationPlan,
    operation_id: str,
    *,
    registry: RegistryClient,
    provider: HostProvider,
    configurator: HostConfigurator,
    readiness: ReadinessChecker,
    resume: bool,
) -> PhaseExecution:
    if phase not in EXECUTION_PHASES:
        raise PlanError(f"phase is not executable: {phase.value}")
    validate_fingerprint(plan)
    validate_source_bindings(plan)
    operation = registry.get_operation(operation_id)
    if operation is None:
        raise PlanError(f"operation not found: {operation_id}")
    _require_operation_plan(operation, plan)
    authority = registry.renew_locks(operation_id)
    lease = _RegistryAuthorityLease(registry, operation_id, authority)
    existing = next(
        (step for step in operation.steps if step.name == phase.value), None
    )
    if existing is None:
        raise RegistryConflictError(f"operation step is missing: {phase.value}")
    attempt = _next_attempt(existing.evidence if existing else {})
    context = HostContext(plan)
    started = datetime.now(UTC)
    if existing.status == "succeeded":
        try:
            result = _revalidate_completed_phase(
                phase,
                context,
                operation,
                registry,
                provider,
                configurator,
                readiness,
                lease.authority,
            )
            if phase is ProvisioningPhase.ACTIVATE:
                if _host_operation_status(operation) != "running":
                    registry.start_operation(operation_id, lease.authority)
                registry.complete_operation(operation_id, lease.authority)
                registry.release_locks(operation_id)
            return PhaseExecution(result, 0)
        except (UnknownProviderResult, RegistryError, AdapterError) as exc:
            return _phase_failure(
                exc,
                plan,
                operation_id,
                phase,
                started,
                attempt,
                registry,
                lease.authority,
                previous_evidence=existing.evidence,
            )
    attempt = _ensure_step_running(
        plan,
        operation_id,
        existing,
        registry,
        lease.authority,
        started,
    )
    if (
        resume
        and phase is ProvisioningPhase.ALLOCATE
        and existing.status
        in {
            "blocked",
            "running",
            StepStatus.NEEDS_RECONCILE.value,
        }
    ):
        try:
            observation = lease.run(lambda: provider.observe(context))
            if observation.exists:
                recovered = observation.provider_evidence
                if recovered is None:
                    raise UnknownProviderResult(
                        "provider Resource exists without recoverable ownership evidence"
                    )
                registry.update_resource_lifecycle(
                    operation_id,
                    plan.resource.id,
                    "allocated",
                    lease.authority,
                )
                evidence = _evidence(
                    plan,
                    operation_id,
                    phase,
                    StepStatus.SUCCEEDED,
                    started,
                    attempt=attempt,
                    message="allocation recovered from live provider state",
                    details={
                        "providerEvidence": recovered.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                        "observation": observation.model_dump(
                            mode="json",
                            by_alias=True,
                        ),
                    },
                )
                registry.record_step(operation_id, evidence, lease.authority)
                return PhaseExecution(evidence, 0)
            if not observation.absence_confirmed:
                raise UnknownProviderResult(
                    "provider Resource absence is not confirmed; reconcile is required"
                )
        except (UnknownProviderResult, RegistryError, AdapterError) as exc:
            return _phase_failure(
                exc,
                plan,
                operation_id,
                phase,
                started,
                attempt,
                registry,
                lease.authority,
                previous_evidence=existing.evidence,
            )
    try:
        details = _run_phase(
            phase,
            context,
            operation_id,
            registry,
            provider,
            configurator,
            readiness,
            lease,
        )
        evidence = _evidence(
            plan,
            operation_id,
            phase,
            StepStatus.SUCCEEDED,
            started,
            attempt=attempt,
            details=details,
        )
        registry.record_step(operation_id, evidence, lease.authority)
        if phase is ProvisioningPhase.ACTIVATE:
            registry.complete_operation(operation_id, lease.authority)
            registry.release_locks(operation_id)
        return PhaseExecution(evidence, 0)
    except (UnknownProviderResult, RegistryError, AdapterError) as exc:
        return _phase_failure(
            exc,
            plan,
            operation_id,
            phase,
            started,
            attempt,
            registry,
            lease.authority,
            previous_evidence=existing.evidence if existing else None,
        )


def _phase_failure(
    error: HostOperationError,
    plan: HostOperationPlan,
    operation_id: str,
    phase: ProvisioningPhase,
    started: datetime,
    attempt: int,
    registry: RegistryClient,
    authority: RegistryAuthority,
    *,
    previous_evidence: dict[str, object] | None = None,
) -> PhaseExecution:
    needs_reconcile = isinstance(error, (UnknownProviderResult, AdapterError))
    evidence = _evidence(
        plan,
        operation_id,
        phase,
        StepStatus.NEEDS_RECONCILE if needs_reconcile else StepStatus.FAILED,
        started,
        attempt=attempt,
        message=str(error),
        details=(
            {"previousEvidence": previous_evidence}
            if previous_evidence is not None
            else None
        ),
    )
    if needs_reconcile:
        _mark_needs_reconcile(
            plan,
            operation_id,
            evidence,
            registry,
            authority,
        )
    if isinstance(error, UnknownProviderResult):
        exit_code = 6
    elif isinstance(error, AdapterError):
        exit_code = 4
    elif isinstance(error, RegistryAuthenticationError):
        exit_code = 4
    else:
        exit_code = 5
    return PhaseExecution(evidence, exit_code)


def _ensure_step_running(
    plan: HostOperationPlan,
    operation_id: str,
    step: RegistryStep,
    registry: RegistryClient,
    authority: RegistryAuthority,
    started: datetime,
) -> int:
    attempt = _next_attempt(step.evidence)
    if step.status == "running":
        return attempt
    if step.status not in {
        "planned",
        StepStatus.PENDING.value,
        "blocked",
        StepStatus.NEEDS_RECONCILE.value,
    }:
        raise RegistryConflictError(
            f"operation step cannot be resumed: {step.name} ({step.status})"
        )
    running = _evidence(
        plan,
        operation_id,
        ProvisioningPhase(step.name),
        StepStatus.RUNNING,
        started,
        attempt=attempt,
        details={"previousEvidence": step.evidence} if step.evidence else None,
    )
    registry.record_step(operation_id, running, authority)
    return attempt


def _mark_needs_reconcile(
    plan: HostOperationPlan,
    operation_id: str,
    trigger: HostOperationEvidence,
    registry: RegistryClient,
    authority: RegistryAuthority,
) -> None:
    operation = registry.get_operation(operation_id)
    if operation is None:
        raise RegistryConflictError(f"operation not found: {operation_id}")
    writable = {
        "planned",
        StepStatus.PENDING.value,
        "running",
        "blocked",
        StepStatus.NEEDS_RECONCILE.value,
    }
    preferred = next(
        (step for step in operation.steps if step.name == trigger.phase.value),
        None,
    )
    candidate = preferred if preferred and preferred.status in writable else None
    if candidate is None:
        candidate = next(
            (step for step in operation.steps if step.status in writable),
            None,
        )
    if candidate is None:
        if (
            _host_operation_status(operation)
            == OperationStatus.NEEDS_RECONCILE.value
        ):
            return
        raise RegistryConflictError(
            "operation has no writable step for reconciliation evidence"
        )
    started = datetime.now(UTC)
    attempt = _ensure_step_running(
        plan,
        operation_id,
        candidate,
        registry,
        authority,
        started,
    )
    evidence = trigger
    if candidate.name != trigger.phase.value:
        evidence = _evidence(
            plan,
            operation_id,
            ProvisioningPhase(candidate.name),
            StepStatus.NEEDS_RECONCILE,
            started,
            attempt=attempt,
            message=trigger.message,
            details={"triggerEvidence": trigger.as_artifact()},
        )
    registry.mark_needs_reconcile(operation_id, evidence, authority)


def _run_phase(
    phase: ProvisioningPhase,
    context: HostContext,
    operation_id: str,
    registry: RegistryClient,
    provider: HostProvider,
    configurator: HostConfigurator,
    readiness: ReadinessChecker,
    authority: RegistryAuthority | _RegistryAuthorityLease,
) -> dict[str, object]:
    if phase not in EXECUTION_PHASES:
        raise PlanError(f"phase is not executable: {phase.value}")
    lease = (
        authority
        if isinstance(authority, _RegistryAuthorityLease)
        else _RegistryAuthorityLease(registry, operation_id, authority)
    )
    if phase is ProvisioningPhase.RESERVE:
        resource = registry.reserve_resource(
            operation_id,
            context.plan.resource,
            lease.authority,
        )
        registry.start_operation(operation_id, lease.authority)
        return {"resourceRevision": resource.revision}
    if phase is ProvisioningPhase.ALLOCATE:
        evidence = lease.run(
            lambda: provider.allocate(context, lease.authority)
        )
        registry.update_resource_lifecycle(
            operation_id,
            context.plan.resource.id,
            "allocated",
            lease.authority,
        )
        return {"providerEvidence": evidence.model_dump(mode="json", by_alias=True)}
    operation = registry.get_operation(operation_id)
    if operation is None:
        raise RegistryConflictError(f"operation not found: {operation_id}")
    provider_evidence = _provider_evidence(operation)
    if phase is ProvisioningPhase.PROVIDER_VERIFY:
        result = lease.run(lambda: provider.verify(context, provider_evidence))
        _require_passed(result, "provider verification")
        return {"verification": result.model_dump(mode="json", by_alias=True)}
    if phase is ProvisioningPhase.BIND:
        registry.bind_provider(
            operation_id,
            context.plan.resource.id,
            provider_evidence,
            lease.authority,
        )
        return {"providerResourceId": provider_evidence.resource_id}
    if phase is ProvisioningPhase.WAIT_READY:
        result = lease.run(
            lambda: readiness.wait(context, provider.observe(context))
        )
        _require_passed(result, "host readiness")
        return {"verification": result.model_dump(mode="json", by_alias=True)}
    if phase is ProvisioningPhase.BOOTSTRAP:
        result = lease.run(lambda: configurator.bootstrap(context))
        _require_step_succeeded(result.status, result.message, "bootstrap")
        registry.update_resource_lifecycle(
            operation_id,
            context.plan.resource.id,
            "bootstrapped",
            lease.authority,
        )
        return dict(result.details)
    if phase is ProvisioningPhase.CONVERGE:
        result = lease.run(lambda: configurator.converge(context))
        _require_step_succeeded(result.status, result.message, "converge")
        registry.update_resource_lifecycle(
            operation_id,
            context.plan.resource.id,
            "configured",
            lease.authority,
        )
        return dict(result.details)
    if phase is ProvisioningPhase.CONFIGURATION_VERIFY:
        result = lease.run(lambda: configurator.verify(context))
        _require_passed(result, "configuration verification")
        return {"verification": result.model_dump(mode="json", by_alias=True)}
    if phase is ProvisioningPhase.ACTIVATE:
        resource = registry.update_resource_lifecycle(
            operation_id,
            context.plan.resource.id,
            "ready",
            lease.authority,
        )
        return {"resourceRevision": resource.revision}
    raise AssertionError(
        f"unhandled executable phase: {phase.value}"
    )  # pragma: no cover


def _revalidate_completed_phase(
    phase: ProvisioningPhase,
    context: HostContext,
    operation: RegistryOperation,
    registry: RegistryClient,
    provider: HostProvider,
    configurator: HostConfigurator,
    readiness: ReadinessChecker,
    authority: RegistryAuthority,
) -> HostOperationEvidence:
    if phase is ProvisioningPhase.RESERVE:
        registry.reserve_resource(operation.id, context.plan.resource, authority)
        registry.start_operation(operation.id, authority)
    elif phase is ProvisioningPhase.ALLOCATE:
        observation = provider.observe(context)
        if not observation.exists:
            raise UnknownProviderResult(
                "completed allocation is not present at provider"
            )
    elif phase is ProvisioningPhase.PROVIDER_VERIFY:
        _require_passed(
            provider.verify(context, _provider_evidence(operation)),
            "provider verification",
        )
    elif phase is ProvisioningPhase.BIND:
        resource = registry.get_resource(context.plan.resource.id)
        evidence = _provider_evidence(operation)
        if (
            resource is None
            or resource.binding is None
            or not provider_binding_matches(resource.binding, evidence)
        ):
            raise RegistryConflictError(
                "completed provider Binding does not match the allocation evidence"
            )
    elif phase is ProvisioningPhase.WAIT_READY:
        _require_passed(
            readiness.wait(context, provider.observe(context)),
            "host readiness",
        )
    elif phase is ProvisioningPhase.BOOTSTRAP:
        _require_passed(
            configurator.verify(_bootstrap_verification_context(context)),
            "bootstrap verification",
        )
    elif phase in {
        ProvisioningPhase.CONVERGE,
        ProvisioningPhase.CONFIGURATION_VERIFY,
    }:
        _require_passed(configurator.verify(context), "configuration verification")
    else:
        # execute_phase rejects VALIDATE before this internal helper is called, so
        # ACTIVATE is the only remaining executable phase.
        resource = registry.get_resource(context.plan.resource.id)
        if resource is None or _host_lifecycle(resource.lifecycle_state) != "active":
            raise RegistryConflictError("completed activation is not present")
    now = datetime.now(UTC)
    return _evidence(
        context.plan,
        operation.id,
        phase,
        StepStatus.SKIPPED,
        now,
        message="completed phase live state was revalidated",
    )


def provider_for_plan(plan: HostOperationPlan) -> HostProvider:
    provider = _provider_for_adapter(plan.provider.adapter)
    if provider.resource_type != plan.provider.resource_type:
        raise AdapterError("provider Resource type does not match the host plan")
    return provider


def _provider_for_adapter(adapter: str) -> HostProvider:
    if adapter == "proxmox":
        return ProxmoxHostProvider()
    raise AdapterError(f"unsupported provider adapter: {adapter}")


def _bootstrap_verification_context(context: HostContext) -> HostContext:
    configuration = context.plan.configuration.model_copy(
        update={
            "converge_playbook": context.plan.configuration.bootstrap_playbook,
        }
    )
    return HostContext(
        context.plan.model_copy(update={"configuration": configuration})
    )


def configurator_for_plan(plan: HostOperationPlan) -> HostConfigurator:
    if plan.configuration.adapter == "ansible":
        return AnsibleHostConfigurator()
    raise AdapterError(
        f"unsupported configuration adapter: {plan.configuration.adapter}"
    )


def phase_job_main(
    phase: ProvisioningPhase,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=f"host-{phase.value}")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = read_plan(args.plan)
        result = execute_phase(
            phase,
            plan,
            args.operation,
            registry=load_registry_client(plan),
            provider=provider_for_plan(plan),
            configurator=configurator_for_plan(plan),
            readiness=HostReadinessChecker(),
            resume=args.resume,
        )
        write_json(result.evidence.as_artifact())
        return result.exit_code
    except HostOperationError as exc:
        print(str(exc), file=sys.stderr)
        return _exception_exit_code(exc)


def host_registry_reserve_main(argv: list[str] | None = None) -> int:
    """Run the registry reservation phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.RESERVE, argv)


def host_provider_allocate_main(argv: list[str] | None = None) -> int:
    """Run the provider allocation phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.ALLOCATE, argv)


def host_provider_verify_main(argv: list[str] | None = None) -> int:
    """Run the provider verification phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.PROVIDER_VERIFY, argv)


def host_registry_bind_main(argv: list[str] | None = None) -> int:
    """Run the registry binding phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.BIND, argv)


def host_wait_ready_main(argv: list[str] | None = None) -> int:
    """Run the readiness phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.WAIT_READY, argv)


def host_config_bootstrap_main(argv: list[str] | None = None) -> int:
    """Run the configuration bootstrap phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.BOOTSTRAP, argv)


def host_config_converge_main(argv: list[str] | None = None) -> int:
    """Run the configuration converge phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.CONVERGE, argv)


def host_config_verify_main(argv: list[str] | None = None) -> int:
    """Run the configuration verification phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.CONFIGURATION_VERIFY, argv)


def host_registry_activate_main(argv: list[str] | None = None) -> int:
    """Run the registry activation phase as a manifest job."""
    return phase_job_main(ProvisioningPhase.ACTIVATE, argv)


def reconcile_job_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="host-operation-reconcile")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args(argv)
    try:
        plan = read_plan(args.plan)
        registry = load_registry_client(plan)
        provider = provider_for_plan(plan)
        operation = registry.get_operation(args.operation)
        if operation is None:
            raise PlanError(f"operation not found: {args.operation}")
        _require_operation_plan(operation, plan)
        validate_source_bindings(plan)
        authority = registry.renew_locks(args.operation)
        lease = _RegistryAuthorityLease(registry, args.operation, authority)
        registry.start_operation(args.operation, authority)
        allocation = next(
            (
                step
                for step in operation.steps
                if step.name == ProvisioningPhase.ALLOCATE.value
            ),
            None,
        )
        if allocation is None:
            raise RegistryConflictError("operation allocation step is missing")
        now = datetime.now(UTC)
        attempt = _ensure_step_running(
            plan,
            args.operation,
            allocation,
            registry,
            authority,
            now,
        )
        observation = lease.run(lambda: provider.observe(HostContext(plan)))
        authority = lease.authority
        if observation.exists and observation.provider_evidence is not None:
            registry.update_resource_lifecycle(
                args.operation,
                plan.resource.id,
                "allocated",
                authority,
            )
            status = StepStatus.SUCCEEDED
            exit_code = 0
            message = "provider allocation recovered from live state"
        elif observation.absence_confirmed:
            status = StepStatus.FAILED
            exit_code = 1
            message = "provider Resource absence confirmed"
        else:
            status = StepStatus.NEEDS_RECONCILE
            exit_code = 6
            message = "provider state remains uncertain"
        details: dict[str, object] = {
            "observation": observation.model_dump(mode="json", by_alias=True)
        }
        if observation.provider_evidence is not None:
            details["providerEvidence"] = observation.provider_evidence.model_dump(
                mode="json",
                by_alias=True,
            )
        evidence = _evidence(
            plan,
            args.operation,
            ProvisioningPhase.ALLOCATE,
            status,
            now,
            attempt=attempt,
            message=message,
            details=details,
        )
        if status is StepStatus.SUCCEEDED:
            registry.record_step(args.operation, evidence, authority)
        elif status is StepStatus.FAILED:
            registry.record_step(args.operation, evidence, authority)
            registry.fail_operation(args.operation, message, authority)
        else:
            registry.mark_needs_reconcile(args.operation, evidence, authority)
        write_json(evidence.as_artifact())
        return exit_code
    except HostOperationError as exc:
        print(str(exc), file=sys.stderr)
        return _exception_exit_code(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hostctl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("host_spec")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("plan", nargs="?", default="-")
    apply_parser.add_argument("--confirm", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("target")
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("target")
    resume_parser.add_argument("--confirm", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("target")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("target")
    rollback_parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_host_plan(args.host_spec)
            write_json(plan.as_artifact())
            return 0
        plan = read_plan(args.plan) if args.command == "apply" else None
        if plan is not None:
            registry = load_registry_client(plan)
        elif Path(args.target).is_file() and not Path(args.target).is_symlink():
            target_plan = read_plan(args.target)
            registry = load_registry_client(target_plan)
        else:
            profile = os.environ.get("ATLAS_REGISTRY_PROFILE")
            if profile is None:
                raise InputError(
                    "ATLAS_REGISTRY_PROFILE is required when target is not a plan file"
                )
            registry = HTTPRegistryClient.from_profile(profile)
        controller = HostController(
            registry,
            SubprocessPhaseExecutor(
                atlas_executable=os.environ.get(
                    "ATLAS_EXECUTABLE",
                    str(
                        Path(os.environ.get("ATLAS_HOME", "/opt/atlas"))
                        / "bin/atlas"
                    ),
                )
            ),
        )
        if args.command == "apply":
            result = controller.apply(plan, confirm=args.confirm)
            write_json(result.evidence.as_artifact())
            return result.exit_code
        if args.command == "status":
            write_json(controller.status(args.target).as_artifact())
            return 0
        if args.command == "resume":
            resolved_plan, _operation = controller._resolve_plan_and_operation(
                args.target,
                require_operation=True,
            )
            result = controller.resume(resolved_plan, confirm=args.confirm)
            write_json(result.evidence.as_artifact())
            return result.exit_code
        if args.command == "verify":
            result = controller.verify(args.target)
            write_json(result.model_dump(mode="json", by_alias=True))
            return 0 if result.status == "passed" else 1
        if args.command == "rollback":
            result = controller.rollback(args.target, confirm=args.confirm)
            write_json(result.evidence.as_artifact())
            return result.exit_code
        raise InputError(f"unsupported command: {args.command}")  # pragma: no cover
    except HostOperationError as exc:
        print(str(exc), file=sys.stderr)
        return _exception_exit_code(exc)


def _validate_inventory_target(project: Path, site: str, target: str) -> None:
    inventory = safe_file(project / "inventories" / site / "hosts.yml")
    data = read_yaml(inventory)

    def contains(value: object, *, under_hosts: bool = False) -> bool:
        if isinstance(value, dict):
            if under_hosts and target in value:
                return True
            return any(
                contains(child, under_hosts=under_hosts or key == "hosts")
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(contains(child, under_hosts=under_hosts) for child in value)
        return under_hosts and value == target

    if not contains(data):
        raise InputError(f"configuration target is absent from inventory: {target}")


def _host_create_idempotency_key(
    *,
    resource_name: str,
    source_digests: dict[str, str],
    provisioning_commit: str,
    provisioning_dirty: bool,
    registry_revision: int,
) -> str:
    intent = json.dumps(
        {
            "resourceName": resource_name,
            "sourceDigests": source_digests,
            "provisioningCommit": provisioning_commit,
            "provisioningDirty": provisioning_dirty,
            "registryRevision": registry_revision,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(intent.encode("utf-8")).hexdigest()
    return f"host-create:{resource_name}:sha256:{digest}"


def _provider_evidence(operation: RegistryOperation) -> ProviderEvidence:
    allocate = next(
        (
            step
            for step in operation.steps
            if step.name == ProvisioningPhase.ALLOCATE.value
        ),
        None,
    )
    raw = allocate.evidence if allocate is not None else {}
    provider = None
    while isinstance(raw, dict):
        details = raw.get("details")
        if not isinstance(details, dict):
            break
        provider = details.get("providerEvidence")
        if isinstance(provider, dict):
            break
        raw = details.get("previousEvidence")
    if not isinstance(provider, dict):
        raise PlanError("provider allocation evidence is missing")
    try:
        return ProviderEvidence.model_validate(provider)
    except ValidationError as exc:
        raise PlanError("provider allocation evidence is invalid") from exc


def _operation_host_plan(operation: RegistryOperation) -> HostOperationPlan:
    intent = operation.plan.get("intent")
    raw = intent.get("hostPlan") if isinstance(intent, dict) else None
    if not isinstance(raw, dict):
        raise PlanError("Registry operation does not contain a host plan")
    try:
        plan = HostOperationPlan.model_validate(raw)
    except ValidationError as exc:
        raise PlanError("Registry operation contains an invalid host plan") from exc
    validate_fingerprint(plan)
    return plan


def _require_operation_plan(
    operation: RegistryOperation,
    plan: HostOperationPlan,
) -> None:
    recorded = _operation_host_plan(operation)
    if (
        recorded.metadata.plan_id != plan.metadata.plan_id
        or recorded.metadata.fingerprint != plan.metadata.fingerprint
    ):
        raise RegistryConflictError("idempotency key belongs to a different plan")


def _operation_complete(operation: RegistryOperation) -> bool:
    return operation.status in {
        OperationStatus.COMPLETED.value,
        "succeeded",
    }


def _operation_needs_reconcile(operation: RegistryOperation) -> bool:
    return operation.status in {
        OperationStatus.NEEDS_RECONCILE.value,
        "blocked",
    } or any(
        step.status in {StepStatus.NEEDS_RECONCILE.value, "blocked"}
        for step in operation.steps
    )


def _terminal_evidence(
    plan: HostOperationPlan,
    operation: RegistryOperation,
) -> PhaseExecution:
    activate = next(
        (
            step
            for step in operation.steps
            if step.name == ProvisioningPhase.ACTIVATE.value
        ),
        None,
    )
    if activate and isinstance(activate.evidence, dict) and activate.evidence:
        try:
            return PhaseExecution(
                HostOperationEvidence.model_validate(activate.evidence),
                0,
            )
        except ValidationError:
            pass
    now = datetime.now(UTC)
    return PhaseExecution(
        _evidence(
            plan,
            operation.id,
            ProvisioningPhase.ACTIVATE,
            StepStatus.SUCCEEDED,
            now,
            message="operation was already complete",
        ),
        0,
    )


def _host_operation_status(operation: RegistryOperation) -> str:
    if _operation_needs_reconcile(operation):
        return OperationStatus.NEEDS_RECONCILE.value
    return {
        "succeeded": OperationStatus.COMPLETED.value,
        "blocked": OperationStatus.NEEDS_RECONCILE.value,
    }.get(operation.status, operation.status)


def _host_lifecycle(state: str) -> str:
    if state in {"ready", ResourceLifecycle.ACTIVE.value}:
        return ResourceLifecycle.ACTIVE.value
    if state == ResourceLifecycle.ABSENT.value:
        return ResourceLifecycle.ABSENT.value
    if state in {"retired", ResourceLifecycle.RETIRED.value}:
        return ResourceLifecycle.RETIRED.value
    return ResourceLifecycle.PROVISIONING.value


def _evidence(
    plan: HostOperationPlan,
    operation_id: str,
    phase: ProvisioningPhase,
    status: StepStatus,
    started_at: datetime,
    *,
    attempt: int = 1,
    message: str = "",
    details: dict[str, object] | None = None,
) -> HostOperationEvidence:
    return HostOperationEvidence(
        apiVersion="atlas.host-operation/v1",
        kind="HostOperationEvidence",
        operationId=operation_id,
        planId=plan.metadata.plan_id,
        resourceId=plan.resource.id,
        phase=phase,
        status=status,
        startedAt=started_at,
        finishedAt=datetime.now(UTC),
        attempt=attempt,
        message=message,
        details=details or {},
    )


def _next_attempt(evidence: dict[str, object]) -> int:
    attempt = evidence.get("attempt") if isinstance(evidence, dict) else None
    return attempt + 1 if isinstance(attempt, int) and attempt >= 1 else 1


def _require_passed(result: VerificationResult, label: str) -> None:
    if result.status != "passed":
        messages = "; ".join(
            check.message or check.name
            for check in result.checks
            if check.status == "failed"
        )
        raise AdapterError(f"{label} failed: {messages or result.status}")


def _require_step_succeeded(status: str, message: str, label: str) -> None:
    if status != "succeeded":
        raise AdapterError(f"{label} failed: {message or status}")


def _exception_exit_code(exc: HostOperationError) -> int:
    if isinstance(exc, SafetyError):
        return 3
    if isinstance(exc, (InputError, PlanError)):
        return 2
    if isinstance(exc, RegistryConflictError):
        return 5
    if isinstance(exc, UnknownProviderResult):
        return 6
    if isinstance(exc, (AdapterError, RegistryAuthenticationError)):
        return 4
    if isinstance(exc, RegistryError):
        return 5
    return 1


def _phase_job_error(job: str, return_code: int, stderr: str) -> HostOperationError:
    diagnostic = stderr.strip() or f"exit status {return_code}"
    message = f"phase job failed without evidence ({job}): {diagnostic}"
    if return_code == 124:
        if job == _PHASE_JOBS[ProvisioningPhase.ALLOCATE]:
            return UnknownProviderResult(message)
        if job in {
            _PHASE_JOBS[ProvisioningPhase.RESERVE],
            _PHASE_JOBS[ProvisioningPhase.BIND],
            _PHASE_JOBS[ProvisioningPhase.ACTIVATE],
        }:
            return RegistryUnavailableError(message)
        return AdapterError(message)
    if return_code == 2:
        return PlanError(message)
    if return_code == 3:
        return SafetyError(message)
    if return_code == 4:
        return AdapterError(message)
    if return_code == 5:
        return RegistryConflictError(message)
    if return_code == 6:
        return UnknownProviderResult(message)
    return HostOperationError(message)


_PHASE_JOBS = {
    ProvisioningPhase.RESERVE: "host-registry-reserve",
    ProvisioningPhase.ALLOCATE: "host-provider-allocate",
    ProvisioningPhase.PROVIDER_VERIFY: "host-provider-verify",
    ProvisioningPhase.BIND: "host-registry-bind",
    ProvisioningPhase.WAIT_READY: "host-wait-ready",
    ProvisioningPhase.BOOTSTRAP: "host-config-bootstrap",
    ProvisioningPhase.CONVERGE: "host-config-converge",
    ProvisioningPhase.CONFIGURATION_VERIFY: "host-config-verify",
    ProvisioningPhase.ACTIVATE: "host-registry-activate",
}
